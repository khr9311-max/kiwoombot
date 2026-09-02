"""
[4단계] 주문 집행 및 사후 관리.

  - 진입은 ENTRY_ORDER_TYPE, 청산은 EXIT_ORDER_TYPE(시장가) 가 기본.
    모의투자는 최유리/최우선지정가를 거부(RC4026)하므로 지정가로 자동 대체한다.
  - 슬리피지 가드: 시그널 발생가 대비 SLIPPAGE_GUARD_PCT 이상 뛰었으면 진입 포기.
  - 미체결 감시: UNFILLED_TIMEOUT_SEC 초 내 미체결이면 kt10003 으로 취소하고,
    UNFILLED_MAX_CHASE 회까지 현재가로 재시도.
  - 체결 인지는 실시간 주문체결(00) 이 1순위, 주기적인 ka10075/kt00018 대사가 2순위.
  - DRY-RUN 에서는 실제 주문이 나가지 않으므로 현재가로 즉시 체결된 것처럼 시뮬레이션한다.

실시간 주문체결(00) 필드번호
  9203 주문번호  9001 종목코드  302 종목명  913 주문상태  900 주문수량
  901 주문가격   902 미체결수량 903 체결누계금액  904 원주문번호
  907 매도수구분(1:매도 2:매수)  908 주문/체결시간  909 체결번호
  910 체결가     911 체결량     938 당일매매수수료  939 당일매매세금
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..config import settings as cfg
from ..database.db import Database
from .kiwoom_client import KiwoomAPIError, KiwoomClient, norm_code, parse_price, parse_int
from .notifier import BaseNotifier, fmt_fill
from .risk_manager import ExitOrder, RiskManager

log = logging.getLogger(__name__)


@dataclass
class PendingOrder:
    order_no: str
    code: str
    name: str
    side: str            # "BUY" | "SELL"
    qty: int
    remain: int
    price: float
    order_type: str
    sent_at: datetime
    reason: str = ""
    signal_id: int | None = None
    chase_count: int = 0
    db_id: int | None = None
    cancelled: bool = False

    @property
    def age_sec(self) -> float:
        return (datetime.now() - self.sent_at).total_seconds()


@dataclass
class ExecStats:
    buy_orders: int = 0
    sell_orders: int = 0
    buy_fills: int = 0
    sell_fills: int = 0
    cancels: int = 0
    rejects: int = 0
    slippage_blocks: int = 0
    errors: list[str] = field(default_factory=list)


class OrderExecutor:
    def __init__(self, client: KiwoomClient, risk: RiskManager, db: Database, notifier: BaseNotifier):
        self.client = client
        self.risk = risk
        self.db = db
        self.notifier = notifier
        self.pending: dict[str, PendingOrder] = {}
        self.stats = ExecStats()
        self._lock = threading.RLock()
        # 청산 시 진입 정보를 남겨두었다가 체결 시 trades 테이블에 기록
        self._exit_context: dict[str, dict] = {}
        # 매매구분은 거부 응답을 보고 세션 중에 바꿀 수 있어야 하므로 인스턴스에 둔다.
        self.entry_order_type = cfg.ENTRY_ORDER_TYPE
        self.exit_order_type = cfg.EXIT_ORDER_TYPE
        # 거부된 종목의 재시도 금지 시각 / 연속 거부 카운터 / 신규 진입 중단 사유
        self._reject_until: dict[str, datetime] = {}
        self._consecutive_rejects = 0
        self.entry_halted_reason = ""

    # ------------------------------------------------------------ 매매구분/거부
    @staticmethod
    def _price_arg(order_type: str, price: float) -> int | str:
        """지정가 계열만 주문가를 싣는다. 시장가/최유리/최우선은 빈 값."""
        return int(price) if order_type in cfg.LIMIT_ORDER_TYPES and price > 0 else ""

    @staticmethod
    def _is_order_type_reject(msg: str) -> bool:
        """매매구분 자체가 환경에서 막힌 거부인지 (모의투자 RC4026 등)."""
        return "RC4026" in msg or ("최유리" in msg and "불가" in msg)

    def _fallback_order_type(self, order_type: str, msg: str) -> str:
        """거부 사유가 매매구분 때문이면 대체 구분을, 아니면 빈 문자열을 준다."""
        if not self._is_order_type_reject(msg):
            return ""
        alt = cfg.MOCK_UNSUPPORTED_ORDER_TYPES.get(order_type, "")
        return alt if alt and alt != order_type else ""

    def _record_error(self, msg: str) -> None:
        """일일 리포트용 오류 목록. 같은 문구가 수백 번 쌓이지 않도록 중복은 접는다."""
        if msg not in self.stats.errors:
            self.stats.errors.append(msg)
            del self.stats.errors[:-50]

    def _on_buy_reject(self, code: str, name: str, msg: str) -> None:
        """
        매수 거부 뒤처리. 같은 종목을 봉마다 다시 던져 거부가 폭주하는 것을 막는다.
          - 종목별 쿨다운
          - 연속 거부가 임계치를 넘으면 신규 진입 중단(청산 감시는 계속)
          - 알림은 임계치 전까지만, 중단 시 한 번 더
        """
        self.stats.rejects += 1
        self._record_error(f"{code} 매수거부: {msg}")
        self.risk.clear_pending_buy(code)

        cooldown = max(cfg.ORDER_REJECT_COOLDOWN_SEC, 0)
        self._reject_until[code] = datetime.now() + timedelta(seconds=cooldown)
        self._consecutive_rejects += 1
        log.error("매수 주문 거부 %s(%s): %s (연속 %d회, %d초 쿨다운)",
                  name, code, msg, self._consecutive_rejects, cooldown)

        if self._consecutive_rejects < cfg.MAX_CONSECUTIVE_REJECTS:
            self.notifier.error(f"매수 주문 실패 {name}({code}): {msg}")
        elif not self.entry_halted_reason:
            self.entry_halted_reason = msg
            self.notifier.error(
                f"매수 거부 {self._consecutive_rejects}회 연속 — 신규 진입을 중단합니다.\n"
                f"   마지막 사유: {msg}\n"
                f"   보유 종목 청산 감시는 계속됩니다. 설정 확인 후 재기동하세요."
            )

    def resume_entries(self) -> None:
        """설정을 고친 뒤 신규 진입을 다시 여는 수동 스위치."""
        self.entry_halted_reason = ""
        self._consecutive_rejects = 0
        self._reject_until.clear()

    # ------------------------------------------------------------ 진입
    def submit_buy(self, code: str, name: str, qty: int, signal_price: float,
                   current_price: float, *, signal_id: int | None = None,
                   reason: str = "") -> PendingOrder | None:
        if qty <= 0:
            return None

        if self.entry_halted_reason:
            log.info("신규 진입 중단 상태(%s) — %s 주문 생략", self.entry_halted_reason, code)
            self.risk.clear_pending_buy(code)
            return None

        until = self._reject_until.get(code)
        if until is not None and datetime.now() < until:
            log.info("주문거부 쿨다운 %s (~%s) — 진입 생략", code, until.strftime("%H:%M:%S"))
            self.risk.clear_pending_buy(code)
            return None

        # 슬리피지 가드 — 시그널 시점보다 이미 튄 종목은 쫓아가지 않는다.
        if signal_price > 0 and current_price > 0:
            drift = current_price / signal_price - 1.0
            if drift > cfg.SLIPPAGE_GUARD_PCT:
                self.stats.slippage_blocks += 1
                log.info("슬리피지 가드: %s 시그널가 %.0f -> 현재 %.0f (%+.2f%%) 진입 포기",
                         code, signal_price, current_price, drift * 100)
                return None

        order_type = self.entry_order_type

        def _buy(tp: str):
            return self.client.buy(code, qty, self._price_arg(tp, current_price), trade_type=tp)

        try:
            resp = _buy(order_type)
        except KiwoomAPIError as exc:
            # 매매구분이 환경에서 막힌 거부(RC4026)면 대체 구분으로 한 번 더 시도하고,
            # 이후 세션 내내 그 구분을 쓴다. 같은 거부를 매번 반복하지 않기 위함이다.
            alt = self._fallback_order_type(order_type, exc.msg)
            if not alt:
                self._on_buy_reject(code, name, exc.msg)
                return None
            log.warning("진입 매매구분 %s 거부(%s) -> %s 로 대체", order_type, exc.msg, alt)
            self.notifier.warn(f"진입 매매구분 {order_type} 거부 — {alt} 로 자동 전환합니다")
            self.entry_order_type = order_type = alt
            try:
                resp = _buy(order_type)
            except KiwoomAPIError as exc2:
                self._on_buy_reject(code, name, exc2.msg)
                return None

        # 정상 접수 — 연속 거부 카운터와 이 종목의 쿨다운을 푼다.
        self._consecutive_rejects = 0
        self._reject_until.pop(code, None)

        order_no = str(resp.get("ord_no", "")).strip()
        po = PendingOrder(
            order_no=order_no, code=code, name=name, side="BUY", qty=qty, remain=qty,
            price=current_price, order_type=order_type, sent_at=datetime.now(),
            reason=reason, signal_id=signal_id,
        )
        po.db_id = self.db.log_order(
            order_no=order_no, code=code, name=name, side="BUY", order_type=order_type,
            qty=qty, price=current_price, status="접수", reason=reason,
            dry_run=self.client.dry_run, signal_id=signal_id,
        )
        with self._lock:
            self.pending[order_no] = po
        self.stats.buy_orders += 1
        self.risk.mark_pending_buy(code, qty)
        log.info("매수 주문 접수 %s %s %d주 @%.0f (ord_no=%s)", code, name, qty, current_price, order_no)

        if self.client.dry_run:
            self._simulate_fill(po, current_price)
        return po

    # ------------------------------------------------------------ 청산
    def submit_exit(self, order: ExitOrder, price: float) -> PendingOrder | None:
        pos = self.risk.positions.get(order.code)
        if pos is None or pos.qty <= 0:
            return None
        qty = min(order.qty, pos.qty)
        if qty <= 0:
            return None

        order_type = self.exit_order_type if order.urgent else "0"

        def _sell(tp: str):
            return self.client.sell(order.code, qty, self._price_arg(tp, price), trade_type=tp)

        try:
            resp = _sell(order_type)
        except KiwoomAPIError as exc:
            # 청산은 반드시 나가야 한다. 매매구분 거부면 대체 구분으로 즉시 재전송.
            alt = self._fallback_order_type(order_type, exc.msg)
            if alt:
                log.warning("청산 매매구분 %s 거부(%s) -> %s 로 대체", order_type, exc.msg, alt)
                self.exit_order_type = order_type = alt
            try:
                if not alt:
                    raise exc
                resp = _sell(order_type)
            except KiwoomAPIError as exc2:
                self.stats.rejects += 1
                self._record_error(f"{order.code} 매도거부: {exc2.msg}")
                log.error("매도 주문 실패 %s: %s", order.code, exc2)
                self.notifier.error(f"🚨 매도 주문 실패 {pos.name}({order.code}) {qty}주: {exc2.msg}\n"
                                    f"   사유: {order.reason} — 수동 확인 필요")
                return None

        order_no = str(resp.get("ord_no", "")).strip()
        po = PendingOrder(
            order_no=order_no, code=order.code, name=pos.name, side="SELL", qty=qty, remain=qty,
            price=price, order_type=order_type, sent_at=datetime.now(), reason=order.reason,
            signal_id=pos.signal_id,
        )
        po.db_id = self.db.log_order(
            order_no=order_no, code=order.code, name=pos.name, side="SELL", order_type=order_type,
            qty=qty, price=price, status="접수", reason=order.reason,
            dry_run=self.client.dry_run, signal_id=pos.signal_id,
        )
        with self._lock:
            self.pending[order_no] = po
            self._exit_context[order_no] = {
                "entry_price": pos.avg_price,
                "entry_ts": pos.entry_time.isoformat(timespec="seconds"),
                "signal_id": pos.signal_id,
                "took_profit_partial": qty < pos.qty,
            }
        pos.pending_exit = order.reason
        self.stats.sell_orders += 1
        log.info("매도 주문 접수 %s %d주 (%s) ord_no=%s", order.code, qty, order.reason, order_no)

        if self.client.dry_run:
            self._simulate_fill(po, price)
        return po

    # ------------------------------------------------------------ 체결 처리
    def on_realtime_fill(self, values: dict[str, str]) -> None:
        """실시간 주문체결(00) 콜백. 체결량이 있을 때만 체결로 처리한다."""
        order_no = str(values.get("9203", "")).strip()
        code = norm_code(values.get("9001", ""))
        name = (values.get("302") or "").strip()
        status = (values.get("913") or "").strip()
        fill_qty = parse_int(values.get("911"))
        fill_price = parse_price(values.get("910"))
        remain = parse_int(values.get("902"))
        fee = parse_price(values.get("938"))
        tax = parse_price(values.get("939"))

        if not order_no or not code:
            return

        with self._lock:
            po = self.pending.get(order_no)

        # 매도/매수 구분은 우리가 주문을 낼 때 이미 확정한 po.side 를 최우선으로 쓴다.
        # 907(매도수구분)이 비거나 예상 밖 값으로 오면 매도 체결이 매수로 오분류되어
        # 유령 포지션이 생길 수 있다 — po 를 못 찾은 경우(추적 밖 주문)에만 907 로 판정한다.
        side = po.side if po is not None else ("SELL" if str(values.get("907", "")).strip() == "1" else "BUY")

        # 919 거부사유가 실려 오면 그 주문은 죽은 주문이다. 즉시 정리해야
        # 매수는 자리가 풀리고, 매도는 다시 시도할 수 있다.
        reject_reason = (values.get("919") or "").strip()
        if reject_reason:
            self.stats.rejects += 1
            self.stats.errors.append(f"{code} 주문거부: {reject_reason}")
            self.db.update_order_status(order_no, f"거부: {reject_reason}")
            log.error("주문 거부 %s %s(%s): %s", side, code, order_no, reject_reason)
            self._finalize_cancel(order_no)
            if side == "SELL":
                self.notifier.error(
                    f"매도 주문 거부 {name or code} — {reject_reason}\n"
                    f"   포지션이 남아 있습니다. 수동 확인이 필요합니다."
                )
            else:
                self._reject_until[code] = datetime.now() + timedelta(
                    seconds=max(cfg.ORDER_REJECT_COOLDOWN_SEC, 0))
                self.notifier.warn(f"매수 주문 거부 {name or code} — {reject_reason}")
            return

        if fill_qty <= 0 or fill_price <= 0:
            # 접수/확인/취소 통보
            if po is not None:
                po.remain = remain
            if status and "취소" in status:
                self._finalize_cancel(order_no)
            elif status:
                self.db.update_order_status(order_no, status)
            return

        self._apply_fill(order_no, code, name, side, fill_qty, fill_price, remain, fee, tax, po)

    def _simulate_fill(self, po: PendingOrder, price: float) -> None:
        """DRY-RUN 전용: 주문가에 전량 즉시 체결된 것으로 간주."""
        self._apply_fill(po.order_no, po.code, po.name, po.side, po.qty, price, 0, 0.0, 0.0, po)

    def _apply_fill(self, order_no: str, code: str, name: str, side: str, qty: int,
                    price: float, remain: int, fee: float, tax: float,
                    po: PendingOrder | None) -> None:
        reason = po.reason if po else ""
        self.db.log_fill(order_no=order_no, code=code, name=name, side=side, qty=qty,
                         price=price, fee=fee, tax=tax, reason=reason)
        # cash 는 다음 30초 대사까지 지연되지 않도록 체결 즉시 반영한다(킬스위치 오발동 방지).
        self.risk.apply_fill_cash(side, qty, price, fee, tax)

        if side == "BUY":
            self.stats.buy_fills += 1
            pos = self.risk.open_position(
                code, qty, price, name=name,
                signal_id=po.signal_id if po else None,
            )
            if po and po.signal_id:
                self.db.mark_signal_executed(po.signal_id)
            self.notifier.trade(fmt_fill("BUY", code, name or code, qty, price, reason))
            log.info("매수 체결 %s %d주 @%.0f -> 보유 %d주 평단 %.0f", code, qty, price, pos.qty, pos.avg_price)
        else:
            self.stats.sell_fills += 1
            with self._lock:
                ctx = self._exit_context.get(order_no, {})
            entry_price = ctx.get("entry_price", 0.0)
            pos_before = self.risk.positions.get(code)
            if pos_before and ctx.get("took_profit_partial"):
                pos_before.took_profit = True

            self.db.log_trade(
                code=code, name=name, entry_ts=ctx.get("entry_ts", ""),
                exit_ts=datetime.now().isoformat(timespec="seconds"), qty=qty,
                entry_price=entry_price, exit_price=price, exit_reason=reason,
                signal_id=ctx.get("signal_id"),
            )
            pnl_pct = (price / entry_price - 1.0) if entry_price else 0.0
            pnl = (price - entry_price) * qty if entry_price else 0.0
            self.risk.reduce_position(code, qty)
            self.notifier.trade(
                fmt_fill("SELL", code, name or code, qty, price,
                         f"{reason} | 손익 {pnl:+,.0f}원 ({pnl_pct:+.2%})")
            )
            log.info("매도 체결 %s %d주 @%.0f (%s) 손익 %+.0f", code, qty, price, reason, pnl)

        if po is not None:
            po.remain = remain if remain > 0 else max(po.remain - qty, 0)
            if po.remain <= 0:
                self.db.update_order_status(order_no, "체결완료")
                with self._lock:
                    self.pending.pop(order_no, None)
                    self._exit_context.pop(order_no, None)
                if side == "BUY":
                    self.risk.clear_pending_buy(code)
            else:
                self.db.update_order_status(order_no, "부분체결")

    def _finalize_cancel(self, order_no: str) -> None:
        with self._lock:
            po = self.pending.pop(order_no, None)
            self._exit_context.pop(order_no, None)
        if po is None:
            return
        po.cancelled = True
        self.db.update_order_status(order_no, "취소완료")
        if po.side == "BUY":
            self.risk.clear_pending_buy(po.code)
        else:
            pos = self.risk.positions.get(po.code)
            if pos:
                pos.pending_exit = ""

    # ------------------------------------------------------------ 미체결 감시
    def sweep_unfilled(self, price_of) -> None:
        """
        타임아웃된 미체결 주문을 취소하고, 매수는 설정 횟수만큼 현재가로 재시도한다.
        price_of: code -> 현재가 를 돌려주는 콜러블.
        """
        with self._lock:
            stale = [
                po for po in self.pending.values()
                if not po.cancelled and po.age_sec >= cfg.UNFILLED_TIMEOUT_SEC and po.remain > 0
            ]
        for po in stale:
            try:
                self.client.cancel(po.order_no, po.code, po.remain)
            except KiwoomAPIError as exc:
                # 이미 체결/취소된 주문이면 취소가 실패한다. 다음 대사에서 정리된다.
                log.warning("미체결 취소 실패 %s %s: %s", po.code, po.order_no, exc.msg)
                with self._lock:
                    self.pending.pop(po.order_no, None)
                continue

            self.stats.cancels += 1
            log.info("미체결 %.0f초 경과 -> 취소: %s %s %d주", po.age_sec, po.code, po.side, po.remain)
            self._finalize_cancel(po.order_no)

            if po.side == "SELL":
                # 청산은 반드시 나가야 한다. 시장가로 즉시 재전송.
                px = price_of(po.code) or po.price
                self.submit_exit(ExitOrder(po.code, po.remain, f"{po.reason}(재전송)"), px)
            elif po.chase_count < cfg.UNFILLED_MAX_CHASE:
                px = price_of(po.code)
                if not px:
                    continue
                ok, why = self.risk.can_buy(po.code, px)
                if not ok:
                    log.info("재시도 취소 %s: %s", po.code, why)
                    continue
                new_po = self.submit_buy(po.code, po.name, po.remain, po.price, px,
                                         signal_id=po.signal_id, reason=f"{po.reason}(재시도)")
                if new_po:
                    new_po.chase_count = po.chase_count + 1
            else:
                self.notifier.warn(f"{po.name}({po.code}) 매수 미체결 재시도 소진 — 진입 포기")

    # ------------------------------------------------------------ 대사
    def reconcile(self) -> None:
        """
        증권사의 미체결 원장과 내부 pending 을 맞춘다.
        실시간 통보를 놓쳤을 때(재접속 직후 등)의 안전망.
        """
        if self.client.dry_run:
            return
        try:
            live = {o["order_no"]: o for o in self.client.get_unfilled()}
        except KiwoomAPIError as exc:
            log.warning("미체결 대사 실패: %s", exc)
            return

        with self._lock:
            tracked = list(self.pending.items())

        for order_no, po in tracked:
            if order_no in live:
                po.remain = live[order_no]["remain_qty"]
                continue
            # 증권사 원장에 없다 = 전량 체결되었거나 취소됨.
            log.info("대사: 미체결 목록에 없는 주문 %s(%s %s) 정리", order_no, po.code, po.side)
            self.db.update_order_status(order_no, "대사종료")
            with self._lock:
                self.pending.pop(order_no, None)
                self._exit_context.pop(order_no, None)
            if po.side == "BUY":
                self.risk.clear_pending_buy(po.code)
            else:
                pos = self.risk.positions.get(po.code)
                if pos:
                    pos.pending_exit = ""

    def cancel_all(self) -> int:
        with self._lock:
            orders = list(self.pending.values())
        n = 0
        for po in orders:
            try:
                self.client.cancel(po.order_no, po.code, po.remain)
                self._finalize_cancel(po.order_no)
                n += 1
            except KiwoomAPIError as exc:
                log.warning("일괄 취소 실패 %s: %s", po.order_no, exc.msg)
        return n

    def has_pending(self, older_than_sec: float = 0.0) -> bool:
        with self._lock:
            return any(po.age_sec >= older_than_sec for po in self.pending.values())
