"""
오프라인 리플레이 백테스터.

실매매와 '같은 코드'로 돌린다 — SignalEngine, RiskManager, BarSeries 를 그대로 쓰고
주문 집행만 시뮬레이터로 대체한다. 백테스트와 실매매의 로직이 갈라지는 것을 막기 위한
구조다(대시보드 문서가 NautilusTrader 를 권한 이유와 같은 취지).

데이터 소스 3가지
  --source api     키움 ka10080 으로 종목별 1분봉을 받아온다 (앱키 필요, 최근 며칠치)
  --source db      봇이 장중에 저장해 둔 bars 테이블을 재생한다
  --source synth   난수 경로를 생성한다 (네트워크·키 없이 로직 검증용)

실행 예)
  python -m trading_bot.tools.backtest --source synth --days 5 --symbols 8
  python -m trading_bot.tools.backtest --source api --codes 005930,000660
  python -m trading_bot.tools.backtest --source db --days 30
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from ..config import settings as cfg
from ..core.bars import BarSeries
from ..core.risk_manager import RiskManager
from ..core.strategy import SignalEngine

log = logging.getLogger("backtest")

# 국내주식 실거래 비용 근사: 매수 수수료, 매도 수수료 + 거래세
FEE_RATE = 0.00015
TAX_RATE = 0.0018


@dataclass
class SimTrade:
    code: str
    entry_ts: datetime
    exit_ts: datetime
    qty: int
    entry: float
    exit: float
    reason: str

    @property
    def gross(self) -> float:
        return (self.exit - self.entry) * self.qty

    @property
    def cost(self) -> float:
        return self.entry * self.qty * FEE_RATE + self.exit * self.qty * (FEE_RATE + TAX_RATE)

    @property
    def pnl(self) -> float:
        return self.gross - self.cost

    @property
    def pnl_pct(self) -> float:
        return self.pnl / (self.entry * self.qty) if self.entry * self.qty else 0.0


@dataclass
class SimResult:
    trades: list[SimTrade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    signals: int = 0
    blocked: int = 0
    start_equity: float = 0.0

    def report(self) -> str:
        if not self.trades:
            return f"매매 없음 (시그널 {self.signals}건, 리스크 차단 {self.blocked}건)"

        pnl = np.array([t.pnl for t in self.trades])
        pct = np.array([t.pnl_pct for t in self.trades])
        wins, losses = pct[pct > 0], pct[pct <= 0]
        end_equity = self.equity_curve[-1][1] if self.equity_curve else self.start_equity

        eq = pd.Series([e for _, e in self.equity_curve])
        mdd = ((eq - eq.cummax()) / eq.cummax()).min() if len(eq) > 1 else 0.0
        daily = pd.Series(pnl).groupby(
            pd.Series([t.exit_ts.date() for t in self.trades])
        ).sum() / self.start_equity
        sharpe = (daily.mean() / daily.std() * np.sqrt(252)) if len(daily) > 1 and daily.std() > 0 else float("nan")

        by_reason = {}
        for t in self.trades:
            key = t.reason.split()[0]
            agg = by_reason.setdefault(key, [0, 0.0])
            agg[0] += 1
            agg[1] += t.pnl

        lines = [
            "",
            "=" * 64,
            f"  기간 손익      {pnl.sum():+,.0f}원  ({end_equity/self.start_equity - 1:+.2%})",
            f"  시작/종료 자산 {self.start_equity:,.0f} -> {end_equity:,.0f}원",
            f"  매매 횟수      {len(self.trades)}회 (시그널 {self.signals} / 리스크차단 {self.blocked})",
            f"  승률           {len(wins)/len(pct):.1%}  ({len(wins)}승 {len(losses)}패)",
            f"  평균 수익/손실 {wins.mean() if len(wins) else 0:+.2%} / {losses.mean() if len(losses) else 0:+.2%}",
            f"  손익비         {abs(wins.mean()/losses.mean()) if len(wins) and len(losses) and losses.mean() else float('nan'):.2f}",
            f"  MDD            {mdd:.2%}",
            f"  일간 샤프      {sharpe:.2f}",
            f"  총 거래비용    {sum(t.cost for t in self.trades):,.0f}원",
            "-" * 64,
            "  청산 사유별:",
        ]
        for k, (n, s) in sorted(by_reason.items(), key=lambda kv: -kv[1][1]):
            lines.append(f"    {k:<12} {n:>4}회  {s:>+12,.0f}원")
        lines.append("=" * 64)
        return "\n".join(lines)


# ------------------------------------------------------------------ 데이터
def synth_bars(code: str, days: int, seed: int) -> pd.DataFrame:
    """랜덤워크 + 간헐적 추세 구간이 섞인 1분봉. 로직 검증용."""
    rng = np.random.default_rng(seed)
    rows = []
    price = float(rng.integers(20_000, 90_000))
    base_vol = float(rng.integers(3_000, 30_000))

    for d in range(days):
        day = datetime.combine(date.today() - timedelta(days=days - d), datetime.min.time())
        day = day.replace(hour=9)
        # 하루에 0~2번 추세 구간을 심어 시그널이 발생할 여지를 만든다
        trends = sorted(rng.choice(np.arange(30, 340), size=int(rng.integers(0, 3)), replace=False))
        drift_until, drift = -1, 0.0

        for m in range(381):  # 09:00 ~ 15:20
            ts = day + timedelta(minutes=m)
            if m in trends:
                drift_until = m + int(rng.integers(20, 60))
                drift = float(rng.normal(0, 1)) * 0.0006
            if m > drift_until:
                drift = 0.0
            ret = rng.normal(drift, 0.0016)
            open_ = price
            price = max(price * (1 + ret), 100.0)
            high = max(open_, price) * (1 + abs(rng.normal(0, 0.0007)))
            low = min(open_, price) * (1 - abs(rng.normal(0, 0.0007)))
            vol = int(base_vol * max(0.2, rng.lognormal(0, 0.6)) * (2.5 if drift else 1.0))
            rows.append({"time": ts, "open": open_, "high": high, "low": low,
                         "close": price, "volume": vol})

    return pd.DataFrame(rows).set_index("time")


def db_bars(days: int) -> dict[str, pd.DataFrame]:
    with sqlite3.connect(cfg.DB_PATH) as conn:
        df = pd.read_sql_query(
            "SELECT code, ts, open, high, low, close, volume FROM bars "
            "WHERE trade_date >= date('now', ?) ORDER BY code, ts",
            conn, params=(f"-{days} day",),
        )
    if df.empty:
        return {}
    df["ts"] = pd.to_datetime(df["ts"])
    return {c: g.set_index("ts")[["open", "high", "low", "close", "volume"]]
            for c, g in df.groupby("code")}


def api_bars(codes: list[str], count: int) -> dict[str, pd.DataFrame]:
    from ..core.kiwoom_client import KiwoomClient

    client = KiwoomClient()
    client.issue_token()
    out = {}
    for code in codes:
        rows = client.get_minute_chart(code, 1, count)
        if not rows:
            log.warning("%s: 분봉 없음", code)
            continue
        out[code] = pd.DataFrame(rows).set_index("time")[["open", "high", "low", "close", "volume"]]
        log.info("%s: %d봉 (%s ~ %s)", code, len(rows), rows[0]["time"], rows[-1]["time"])
    return out


# ------------------------------------------------------------------ 실행
def run(data: dict[str, pd.DataFrame], start_equity: float) -> SimResult:
    """모든 종목의 봉을 시각 순으로 병합해 하루씩 재생한다."""
    engine = SignalEngine(prev_turnover={})
    risk = RiskManager()
    risk.cash = start_equity
    risk.orderable_cash = start_equity
    risk.total_equity = start_equity
    risk.reset_day(start_equity)

    result = SimResult(start_equity=start_equity)
    series: dict[str, BarSeries] = {c: BarSeries(c, maxlen=400) for c in data}
    entry_meta: dict[str, tuple[datetime, float]] = {}

    # 전일 거래대금 근사: 종목별 하루 평균 거래대금
    prev_turnover = {}
    for code, df in data.items():
        by_day = (df["close"] * df["volume"]).groupby(df.index.date).sum()
        prev_turnover[code] = float(by_day.mean()) if len(by_day) else 0.0
    engine.set_prev_turnover(prev_turnover)

    merged = pd.concat(
        [df.assign(code=c) for c, df in data.items()]
    ).sort_index()

    cur_day: date | None = None
    day_turnover: dict[str, float] = {}

    for ts, row in merged.iterrows():
        code = row["code"]
        ts = ts.to_pydatetime()

        if cur_day != ts.date():
            # 전일 잔여 포지션은 일괄 청산 규칙대로 이미 정리되었어야 한다
            cur_day = ts.date()
            day_turnover = {}
            risk.reset_day(risk.total_equity)
            risk.cash = risk.orderable_cash

        s = series[code]
        day_turnover[code] = day_turnover.get(code, 0.0) + float(row["close"] * row["volume"])
        s.snapshot.cum_turnover = day_turnover[code]
        # 체결강도는 재생 데이터에 없다 -> 상승봉이면 매수 우위로 근사
        s.snapshot.strength = 115.0 if row["close"] >= row["open"] else 95.0
        s.snapshot.updated = ts

        closed = s.on_tick(
            ts=ts, price=float(row["close"]), cum_volume=0,
            tick_volume=int(row["volume"]),
            open_=float(row["open"]), high=float(row["high"]), low=float(row["low"]),
        )

        price = float(row["close"])

        # --- 청산 감시 (봉 고가/저가로 장중 스톱 접촉을 근사)
        pos = risk.positions.get(code)
        if pos:
            for probe in (float(row["low"]), float(row["high"]), price):
                order = risk.check_exit(pos, probe, now=ts)
                if order is None:
                    continue
                fill = probe
                e_ts, e_px = entry_meta.get(code, (ts, pos.avg_price))
                qty = min(order.qty, pos.qty)
                result.trades.append(SimTrade(code, e_ts, ts, qty, e_px, fill, order.reason))
                risk.cash += fill * qty * (1 - FEE_RATE - TAX_RATE)
                risk.orderable_cash = risk.cash
                if qty < pos.qty:
                    pos.took_profit = True
                remaining = risk.reduce_position(code, qty, now=ts)
                if remaining is None:
                    entry_meta.pop(code, None)
                pos = remaining
                if pos is None:
                    break

        # --- 15:15 일괄 청산
        if ts.time() >= cfg.FLATTEN_TIME:
            for c in list(risk.positions):
                p = risk.positions[c]
                px = series[c].last_close or p.avg_price
                e_ts, e_px = entry_meta.get(c, (ts, p.avg_price))
                result.trades.append(SimTrade(c, e_ts, ts, p.qty, e_px, px, "장마감 일괄청산"))
                risk.cash += px * p.qty * (1 - FEE_RATE - TAX_RATE)
                risk.orderable_cash = risk.cash
                risk.reduce_position(c, p.qty, now=ts)
                entry_meta.pop(c, None)

        # --- 진입 판정 (봉 확정 시점에만)
        if closed is not None:
            risk.mark_to_market({c: series[c].last_close for c in risk.positions})
            if risk.check_kill_switch():
                for c in list(risk.positions):
                    p = risk.positions[c]
                    px = series[c].last_close or p.avg_price
                    e_ts, e_px = entry_meta.get(c, (ts, p.avg_price))
                    result.trades.append(SimTrade(c, e_ts, ts, p.qty, e_px, px, "킬스위치 청산"))
                    risk.cash += px * p.qty * (1 - FEE_RATE - TAX_RATE)
                    risk.reduce_position(c, p.qty, now=ts)
                    entry_meta.pop(c, None)
            else:
                sig = engine.evaluate(s)
                if sig.is_buy:
                    result.signals += 1
                    ok, _ = risk.can_buy(code, price, now=ts)
                    if not ok:
                        result.blocked += 1
                    else:
                        qty, _amount, _note = risk.calc_qty(price, atr=engine.atr_of(s))
                        if qty > 0:
                            cost = price * qty * (1 + FEE_RATE)
                            risk.cash -= cost
                            risk.orderable_cash = risk.cash
                            risk.open_position(code, qty, price, now=ts)
                            entry_meta[code] = (ts, price)

            risk.mark_to_market({c: series[c].last_close for c in risk.positions})
            result.equity_curve.append((ts, risk.total_equity))

    return result


def main() -> None:
    p = argparse.ArgumentParser(description="오프라인 리플레이 백테스트")
    p.add_argument("--source", choices=["synth", "db", "api"], default="synth")
    p.add_argument("--days", type=int, default=5, help="synth/db: 재생 일수")
    p.add_argument("--symbols", type=int, default=8, help="synth: 생성할 종목 수")
    p.add_argument("--codes", type=str, default="005930,000660,035720", help="api: 종목코드")
    p.add_argument("--bars", type=int, default=900, help="api: 종목당 분봉 개수")
    p.add_argument("--equity", type=float, default=10_000_000, help="시작 자산")
    p.add_argument("--seed", type=int, default=42)
    a = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if a.source == "synth":
        data = {f"T{i:05d}": synth_bars(f"T{i:05d}", a.days, a.seed + i) for i in range(a.symbols)}
        log.info("합성 데이터 %d종목 × %d일", a.symbols, a.days)
    elif a.source == "db":
        data = db_bars(a.days)
        log.info("DB 재생 %d종목", len(data))
    else:
        data = api_bars([c.strip() for c in a.codes.split(",") if c.strip()], a.bars)

    if not data:
        log.error("재생할 데이터가 없습니다")
        raise SystemExit(1)

    result = run(data, a.equity)
    print(result.report())
    print(f"\n설정: 진입임계 {cfg.SIGNAL_SCORE_THRESHOLD} · 손절 {cfg.STOP_LOSS_PCT:+.1%} · "
          f"익절 {cfg.TAKE_PROFIT_PCT:+.1%} · 트레일링 {cfg.TRAILING_STOP_PCT:+.1%} · "
          f"타임컷 {cfg.TIME_CUT_MIN}분 · 사이징 {cfg.SIZING_MODE} {cfg.POSITION_PCT:.0%} · "
          f"최대보유 {cfg.MAX_POSITIONS}")


if __name__ == "__main__":
    main()
