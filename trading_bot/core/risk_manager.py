"""
[3단계] 포지션 사이징 & 리스크 관리.

수익보다 원금 보존이 우선이다. 주문 직전 can_buy() 가 진입 가능 여부를 최종 판정하고,
check_exit() 가 보유 종목마다 청산 조건을 감시한다.

청산 규칙 (가이드 문서 3.3):
  손절     매수가 대비 STOP_LOSS_PCT (-2%)      -> 전량 시장가
  1차 익절 매수가 대비 TAKE_PROFIT_PCT (+3%)    -> 보유수량의 TAKE_PROFIT_RATIO (50%)
  트레일링 최고점 대비 TRAILING_STOP_PCT (-1.5%) -> 잔량 전량 (1차 익절 이후 활성)
  타임컷   진입 후 TIME_CUT_MIN 분간 ±1% 횡보    -> 전량 정리
  일괄청산 FLATTEN_TIME (15:15)                  -> 전량
  킬스위치 당일 누적 손실률 DAILY_LOSS_LIMIT_PCT (-3%) -> 전량 청산 후 당일 매매 중단

포지션 사이징 (SIZING_MODE):
  fixed_pct  주문가능금액 × POSITION_PCT (가이드 기본)
  atr_risk   자산 × RISK_PER_TRADE_PCT / (ATR_STOP_MULT × ATR)  — 변동성 역가중
  half_kelly 대시보드의 프랙셔널 켈리. f* = (p(b+1) - 1) / b, 실투입 = KELLY_FRACTION × f*
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..config import settings as cfg

log = logging.getLogger(__name__)


@dataclass
class Position:
    code: str
    name: str = ""
    qty: int = 0
    avg_price: float = 0.0
    entry_time: datetime = field(default_factory=datetime.now)
    peak_price: float = 0.0
    took_profit: bool = False          # 1차 분할 익절 완료 여부
    stop_price: float = 0.0            # ATR 기반 동적 손절가(0이면 % 손절 사용)
    pending_exit: str = ""             # 청산 주문 전송 후 체결 대기 중인 사유
    signal_id: int | None = None       # 매매일지-시그널 연결용

    def unrealized_pct(self, price: float) -> float:
        if self.avg_price <= 0:
            return 0.0
        return price / self.avg_price - 1.0

    def drawdown_from_peak(self, price: float) -> float:
        if self.peak_price <= 0:
            return 0.0
        return price / self.peak_price - 1.0


@dataclass
class ExitOrder:
    code: str
    qty: int
    reason: str
    urgent: bool = True   # True 면 시장가


class RiskManager:
    def __init__(self):
        self.positions: dict[str, Position] = {}
        self.day_start_equity: float = 0.0
        self.orderable_cash: float = 0.0
        self.cash: float = 0.0
        self.total_equity: float = 0.0
        self.kill_switch: bool = False
        self.kill_reason: str = ""
        self.halt_new_entry: bool = False
        self._last_exit_time: dict[str, datetime] = {}
        self._pending_buys: dict[str, int] = {}   # 체결 대기 중인 매수 (중복 진입 방지)

    # ------------------------------------------------------------ 계좌 동기화
    def sync(self, deposit: dict, balance: dict, *, mark_day_start: bool = False) -> None:
        """
        실제 증권사 잔고로 내부 상태를 맞춘다. 서버 재시작 복구의 핵심.
        보유 중인데 내부에 없는 종목은 Position 으로 복원한다(평균단가/수량만 신뢰).
        """
        self.orderable_cash = deposit.get("ord_alow_amt") or deposit.get("entr", 0.0)
        self.cash = deposit.get("entr", 0.0)
        self.total_equity = self.cash + balance.get("total_eval", 0.0)
        if mark_day_start or self.day_start_equity <= 0:
            self.day_start_equity = self.total_equity
            log.info("당일 기준 자산 설정: %s원", f"{self.total_equity:,.0f}")

        live: dict[str, Position] = {}
        for h in balance.get("holdings", []):
            code = h["code"]
            existing = self.positions.get(code)
            if existing:
                existing.qty = h["qty"]
                existing.avg_price = h["avg_price"] or existing.avg_price
                existing.name = h["name"] or existing.name
                existing.peak_price = max(existing.peak_price, h["cur_price"], existing.avg_price)
                live[code] = existing
            else:
                log.warning("내부 상태에 없는 보유 종목 복원: %s %s %d주", code, h["name"], h["qty"])
                live[code] = Position(
                    code=code,
                    name=h["name"],
                    qty=h["qty"],
                    avg_price=h["avg_price"],
                    peak_price=max(h["cur_price"], h["avg_price"]),
                )

        for code in set(self.positions) - set(live):
            log.info("포지션 종료 확인(잔고에 없음): %s", code)
            self._last_exit_time[code] = datetime.now()
        self.positions = live

    def mark_to_market(self, prices: dict[str, float]) -> float:
        """
        실시간 시세로 평가자산을 갱신한다. 잔고 조회(REST)는 30초에 한 번이지만
        킬스위치는 초 단위로 판정해야 하므로 그 사이는 이 값으로 감시한다.
        """
        if self.cash <= 0 and not self.positions:
            return self.total_equity
        holdings_value = sum(
            p.qty * (prices.get(p.code) or p.avg_price) for p in self.positions.values()
        )
        self.total_equity = self.cash + holdings_value
        return self.total_equity

    # ------------------------------------------------------------ 손익
    def daily_pnl_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return self.total_equity / self.day_start_equity - 1.0

    def check_kill_switch(self) -> bool:
        """당일 손실 한도 초과 시 True. 한 번 켜지면 그날은 다시 꺼지지 않는다."""
        if self.kill_switch:
            return True
        pnl = self.daily_pnl_pct()
        if self.day_start_equity > 0 and pnl <= cfg.DAILY_LOSS_LIMIT_PCT:
            self.kill_switch = True
            self.kill_reason = f"당일 손실률 {pnl:+.2%} <= 한도 {cfg.DAILY_LOSS_LIMIT_PCT:+.2%}"
            log.critical("킬스위치 발동: %s", self.kill_reason)
            return True
        return False

    def reset_day(self, equity: float) -> None:
        self.kill_switch = False
        self.kill_reason = ""
        self.halt_new_entry = False
        self.day_start_equity = equity
        self._last_exit_time.clear()
        self._pending_buys.clear()

    # ------------------------------------------------------------ 진입 판정
    def can_buy(self, code: str, price: float, now: datetime | None = None) -> tuple[bool, str]:
        now = now or datetime.now()
        if self.kill_switch:
            return False, f"킬스위치 활성({self.kill_reason})"
        if self.halt_new_entry:
            return False, "신규 진입 중단 시간대"
        if now.time() >= cfg.NO_NEW_ENTRY_AFTER:
            return False, f"{cfg.NO_NEW_ENTRY_AFTER:%H:%M} 이후 신규 진입 금지"
        if code in self.positions:
            return False, "이미 보유 중"
        if code in self._pending_buys:
            return False, "매수 주문 체결 대기 중"
        if len(self.positions) + len(self._pending_buys) >= cfg.MAX_POSITIONS:
            return False, f"최대 보유 종목 수 도달({cfg.MAX_POSITIONS})"

        last_exit = self._last_exit_time.get(code)
        if last_exit and (now - last_exit).total_seconds() < cfg.REENTRY_COOLDOWN_SEC:
            remain = cfg.REENTRY_COOLDOWN_SEC - (now - last_exit).total_seconds()
            return False, f"재진입 쿨다운 {remain:.0f}초 남음"

        if price <= 0:
            return False, "가격 없음"
        if self.orderable_cash < cfg.MIN_ORDER_AMOUNT:
            return False, f"주문가능금액 부족({self.orderable_cash:,.0f}원)"
        return True, "ok"

    # ------------------------------------------------------------ 사이징
    def calc_qty(self, price: float, atr: float = 0.0, win_rate: float | None = None,
                 payoff: float | None = None) -> tuple[int, float, str]:
        """(수량, 투입금액, 산출근거) 를 반환. 수량 0이면 진입하지 않는다."""
        if price <= 0:
            return 0, 0.0, "가격 없음"

        budget_base = min(self.orderable_cash, self.total_equity or self.orderable_cash)
        mode = cfg.SIZING_MODE

        if mode == "atr_risk" and atr > 0:
            risk_amount = (self.total_equity or budget_base) * cfg.RISK_PER_TRADE_PCT
            per_share_risk = cfg.ATR_STOP_MULT * atr
            qty = int(risk_amount // per_share_risk) if per_share_risk > 0 else 0
            note = f"atr_risk: risk={risk_amount:,.0f} / {per_share_risk:,.0f}주당"
            budget = qty * price
        elif mode == "half_kelly":
            p = cfg.KELLY_WIN_RATE if win_rate is None else win_rate
            b = cfg.KELLY_PAYOFF if payoff is None else payoff
            f_star = (p * (b + 1.0) - 1.0) / b if b > 0 else 0.0
            f_used = max(0.0, min(f_star * cfg.KELLY_FRACTION, cfg.KELLY_CAP))
            budget = budget_base * f_used
            qty = int(budget // price)
            note = f"half_kelly: p={p:.2f} b={b:.2f} f*={f_star:.3f} 적용={f_used:.3f}"
        else:
            budget = budget_base * cfg.POSITION_PCT
            qty = int(budget // price)
            note = f"fixed_pct: {cfg.POSITION_PCT:.0%} of {budget_base:,.0f}"

        # 공통 상·하한 적용
        max_qty_by_cap = int(cfg.MAX_ORDER_AMOUNT // price)
        max_qty_by_cash = int(self.orderable_cash * 0.98 // price)  # 수수료 여유
        qty = max(0, min(qty, max_qty_by_cap, max_qty_by_cash))
        amount = qty * price

        if amount < cfg.MIN_ORDER_AMOUNT:
            return 0, amount, f"{note} -> 최소주문금액 미달({amount:,.0f}원)"
        return qty, amount, note

    def reserve_slot(self, code: str, price: float, now: datetime | None = None) -> tuple[bool, str]:
        """
        진입 판정과 슬롯 선점을 하나의 동기 호출로 묶는다.

        can_buy() 로 확인한 뒤 실제 발주까지는 DB 기록·메타추론 등 await 지점이 여러 개
        끼어 있다. 그 사이에 같은 종목의 다음 봉이 확정되면 두 태스크가 같은 검사를
        통과해 중복 주문이 나간다(check-then-act 경쟁). 여기서 미리 자리를 잡아 둔다.
        진입을 포기할 때는 반드시 clear_pending_buy() 로 자리를 반납해야 한다.
        """
        ok, why = self.can_buy(code, price, now)
        if ok:
            self._pending_buys[code] = 0
        return ok, why

    # ------------------------------------------------------------ 포지션 등록
    def mark_pending_buy(self, code: str, qty: int) -> None:
        self._pending_buys[code] = qty

    def clear_pending_buy(self, code: str) -> None:
        self._pending_buys.pop(code, None)

    def open_position(self, code: str, qty: int, price: float, *, name: str = "",
                      atr: float = 0.0, signal_id: int | None = None,
                      now: datetime | None = None) -> Position:
        now = now or datetime.now()
        self.clear_pending_buy(code)
        pos = self.positions.get(code)
        if pos and pos.qty > 0:
            total = pos.qty + qty
            pos.avg_price = (pos.avg_price * pos.qty + price * qty) / total
            pos.qty = total
        else:
            pos = Position(code=code, name=name, qty=qty, avg_price=price,
                           entry_time=now, peak_price=price, signal_id=signal_id)
            self.positions[code] = pos
        if atr > 0:
            pos.stop_price = pos.avg_price - cfg.ATR_STOP_MULT * atr
        pos.peak_price = max(pos.peak_price, price)
        return pos

    def reduce_position(self, code: str, qty: int, now: datetime | None = None) -> Position | None:
        pos = self.positions.get(code)
        if not pos:
            return None
        pos.qty -= qty
        pos.pending_exit = ""
        if pos.qty <= 0:
            self.positions.pop(code, None)
            self._last_exit_time[code] = now or datetime.now()
            return None
        return pos

    def apply_fill_cash(self, side: str, qty: int, price: float, fee: float = 0.0, tax: float = 0.0) -> None:
        """
        체결 즉시 현금을 반영한다(다음 30초 대사에서 증권사 원장 값으로 재보정된다).

        cash/positions 는 체결 시점에 즉시 갱신되는데 cash 만 다음 sync() 까지 지연되면,
        1초 주기 킬스위치 판정(mark_to_market)이 매수 직후엔 자산을 과대계상하고
        매도 직후엔 매도금액이 통째로 증발한 것처럼 계산해 정상 익절에도 오발동한다.
        """
        gross = qty * price
        delta = -(gross + fee) if side == "BUY" else (gross - fee - tax)
        self.cash += delta
        self.orderable_cash += delta

    # ------------------------------------------------------------ 청산 판정
    def check_exit(self, pos: Position, price: float, now: datetime | None = None) -> ExitOrder | None:
        """보유 종목 하나에 대한 청산 판정. 우선순위: 손절 > 트레일링 > 익절 > 타임컷."""
        now = now or datetime.now()
        if pos.qty <= 0 or price <= 0 or pos.pending_exit:
            return None

        pos.peak_price = max(pos.peak_price, price)
        pnl = pos.unrealized_pct(price)

        # 1) 손절 — ATR 기반 동적 손절가가 있으면 그것과 % 손절 중 먼저 닿는 쪽
        if pos.stop_price > 0 and price <= pos.stop_price:
            return ExitOrder(pos.code, pos.qty, f"ATR손절 {price:,.0f} <= {pos.stop_price:,.0f} ({pnl:+.2%})")
        if pnl <= cfg.STOP_LOSS_PCT:
            return ExitOrder(pos.code, pos.qty, f"손절 {pnl:+.2%}")

        # 2) 트레일링 스탑 — 1차 익절 이후 잔량에 적용
        if pos.took_profit:
            dd = pos.drawdown_from_peak(price)
            if dd <= cfg.TRAILING_STOP_PCT:
                return ExitOrder(pos.code, pos.qty, f"트레일링스탑 고점대비 {dd:+.2%} (실현 {pnl:+.2%})")

        # 3) 1차 분할 익절
        if not pos.took_profit and pnl >= cfg.TAKE_PROFIT_PCT:
            qty = max(1, int(pos.qty * cfg.TAKE_PROFIT_RATIO))
            if qty >= pos.qty:  # 1주짜리 포지션이면 전량
                qty = pos.qty
            return ExitOrder(pos.code, qty, f"1차익절 {pnl:+.2%} ({qty}/{pos.qty}주)")

        # 4) 타임컷 — 일정 시간 이상 ±밴드 안에서 횡보
        if cfg.TIME_CUT_MIN > 0:
            held = now - pos.entry_time
            if held >= timedelta(minutes=cfg.TIME_CUT_MIN) and abs(pnl) <= cfg.TIME_CUT_BAND_PCT:
                return ExitOrder(pos.code, pos.qty,
                                 f"타임컷 {held.total_seconds()/60:.0f}분 횡보 ({pnl:+.2%})")
        return None

    def flatten_all(self, reason: str) -> list[ExitOrder]:
        """일괄 청산 목록."""
        return [ExitOrder(p.code, p.qty, reason) for p in self.positions.values()
                if p.qty > 0 and not p.pending_exit]

    # ------------------------------------------------------------ 요약
    def summary(self, prices: dict[str, float] | None = None) -> str:
        prices = prices or {}
        lines = [
            f"자산 {self.total_equity:,.0f}원 (당일 {self.daily_pnl_pct():+.2%}) "
            f"| 주문가능 {self.orderable_cash:,.0f}원 | 보유 {len(self.positions)}/{cfg.MAX_POSITIONS}"
        ]
        for p in self.positions.values():
            px = prices.get(p.code, p.avg_price)
            lines.append(
                f"  {p.code} {p.name} {p.qty}주 @{p.avg_price:,.0f} "
                f"-> {px:,.0f} ({p.unrealized_pct(px):+.2%})"
            )
        if self.kill_switch:
            lines.append(f"  [킬스위치] {self.kill_reason}")
        return "\n".join(lines)


def kelly_fraction(win_rate: float, payoff: float, fraction: float = cfg.KELLY_FRACTION,
                   cap: float = cfg.KELLY_CAP) -> float:
    """f* = (p(b+1) - 1) / b, 실제 투입은 fraction 배 (기본 하프 켈리)."""
    if payoff <= 0:
        return 0.0
    f_star = (win_rate * (payoff + 1.0) - 1.0) / payoff
    if math.isnan(f_star):
        return 0.0
    return max(0.0, min(f_star * fraction, cap))
