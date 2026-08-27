"""
매매 일지 조회 도구.

  python -m trading_bot.tools.report              오늘 요약
  python -m trading_bot.tools.report --days 30    최근 30일 성과
  python -m trading_bot.tools.report --trades     완결 매매 목록
  python -m trading_bot.tools.report --signals    시그널/메타필터 판정 이력
"""
from __future__ import annotations

import argparse
import sqlite3

import pandas as pd

from ..config import settings as cfg


def _read(sql: str, params: tuple = ()) -> pd.DataFrame:
    with sqlite3.connect(cfg.DB_PATH) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def summary(days: int) -> None:
    daily = _read(
        "SELECT * FROM daily ORDER BY trade_date DESC LIMIT ?", (days,)
    ).sort_values("trade_date")
    if daily.empty:
        print("일별 기록이 없습니다.")
    else:
        print(f"\n=== 최근 {len(daily)}영업일 ===")
        print(daily[["trade_date", "start_equity", "end_equity", "pnl", "pnl_pct",
                     "trades", "wins", "kill_switch"]].to_string(index=False))
        total = daily["pnl"].sum()
        cum = daily["end_equity"].iloc[-1] / daily["start_equity"].iloc[0] - 1
        peak = daily["end_equity"].cummax()
        mdd = ((daily["end_equity"] - peak) / peak).min()
        ret = daily["pnl_pct"]
        sharpe = (ret.mean() / ret.std() * (252 ** 0.5)) if ret.std() > 0 else float("nan")
        print(f"\n누적 손익 {total:+,.0f}원 / 수익률 {cum:+.2%} / MDD {mdd:.2%} / 일간 샤프 {sharpe:.2f}")

    trades = _read(
        "SELECT * FROM trades WHERE trade_date >= date('now', ?)", (f"-{days} day",)
    )
    if trades.empty:
        print("\n완결 매매가 없습니다.")
        return
    wins = trades[trades["pnl"] > 0]
    losses = trades[trades["pnl"] <= 0]
    avg_win = wins["pnl_pct"].mean() if len(wins) else 0.0
    avg_loss = losses["pnl_pct"].mean() if len(losses) else 0.0
    payoff = (avg_win / abs(avg_loss)) if avg_loss else float("nan")
    print(
        f"\n매매 {len(trades)}회 | 승률 {len(wins)/len(trades):.1%} | "
        f"평균수익 {avg_win:+.2%} / 평균손실 {avg_loss:+.2%} | 손익비 {payoff:.2f}"
    )
    print("\n청산 사유별:")
    reasons = trades.assign(kind=trades["exit_reason"].str.split().str[0])
    print(reasons.groupby("kind").agg(건수=("pnl", "size"), 손익합=("pnl", "sum"),
                                      평균수익률=("pnl_pct", "mean")).to_string())


def show_trades(days: int) -> None:
    df = _read(
        """SELECT trade_date, code, name, qty, entry_price, exit_price,
                  pnl, pnl_pct, exit_reason, entry_ts, exit_ts
           FROM trades WHERE trade_date >= date('now', ?) ORDER BY exit_ts DESC""",
        (f"-{days} day",),
    )
    print(df.to_string(index=False) if not df.empty else "완결 매매가 없습니다.")


def show_signals(days: int) -> None:
    df = _read(
        """SELECT ts, code, name, action, score, price, meta_prob, meta_approved,
                  executed, label, label_return, reason
           FROM signals WHERE trade_date >= date('now', ?) ORDER BY ts DESC LIMIT 200""",
        (f"-{days} day",),
    )
    if df.empty:
        print("시그널 기록이 없습니다.")
        return
    print(df.to_string(index=False))
    labeled = df[df["label"].notna()]
    if not labeled.empty:
        print(f"\n라벨링된 시그널 {len(labeled)}건 / 성공률 {labeled['label'].mean():.1%}")
    print(f"\n메타 필터 승인 {int(df['meta_approved'].fillna(0).sum())}건 / 전체 {len(df)}건, "
          f"실제 발주 {int(df['executed'].sum())}건")


def main() -> None:
    p = argparse.ArgumentParser(description="매매 일지 리포트")
    p.add_argument("--days", type=int, default=1)
    p.add_argument("--trades", action="store_true")
    p.add_argument("--signals", action="store_true")
    a = p.parse_args()

    if a.trades:
        show_trades(a.days)
    elif a.signals:
        show_signals(a.days)
    else:
        summary(a.days)


if __name__ == "__main__":
    main()
