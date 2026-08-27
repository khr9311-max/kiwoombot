"""
SQLite 매매 일지.

테이블
  signals    1차 시그널 전량 + 피처 벡터(JSON). 메타 모델 학습 데이터의 원천.
  orders     발주한 모든 주문 (DRY-RUN 포함)
  fills      체결 내역
  trades     진입~청산이 짝지어진 완결 매매 (성과 집계용)
  bars       청산 이후 라벨링에 필요한 1분봉 스냅샷
  daily      일별 자산/손익 스냅샷

signals 에 피처를 남겨두면, 나중에 bars 로 삼중 장벽 라벨을 붙여
LightGBM 메타 모델을 바로 학습할 수 있다.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

from ..config import settings as cfg

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    trade_date    TEXT    NOT NULL,
    code          TEXT    NOT NULL,
    name          TEXT,
    action        TEXT    NOT NULL,
    score         REAL,
    price         REAL,
    factors       TEXT,
    features      TEXT,
    meta_prob     REAL,
    meta_approved INTEGER,
    executed      INTEGER DEFAULT 0,
    reason        TEXT,
    label         INTEGER,
    label_reason  TEXT,
    label_return  REAL
);
CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(trade_date, code);

CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    order_no    TEXT,
    code        TEXT NOT NULL,
    name        TEXT,
    side        TEXT NOT NULL,
    order_type  TEXT,
    qty         INTEGER,
    price       REAL,
    status      TEXT,
    reason      TEXT,
    dry_run     INTEGER DEFAULT 0,
    signal_id   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_orders_no ON orders(order_no);

CREATE TABLE IF NOT EXISTS fills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    order_no    TEXT,
    code        TEXT NOT NULL,
    name        TEXT,
    side        TEXT NOT NULL,
    qty         INTEGER,
    price       REAL,
    amount      REAL,
    fee         REAL DEFAULT 0,
    tax         REAL DEFAULT 0,
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_fills_date ON fills(trade_date, code);

CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date   TEXT NOT NULL,
    code         TEXT NOT NULL,
    name         TEXT,
    entry_ts     TEXT,
    exit_ts      TEXT,
    qty          INTEGER,
    entry_price  REAL,
    exit_price   REAL,
    pnl          REAL,
    pnl_pct      REAL,
    exit_reason  TEXT,
    signal_id    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(trade_date);

CREATE TABLE IF NOT EXISTS bars (
    trade_date  TEXT NOT NULL,
    code        TEXT NOT NULL,
    ts          TEXT NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      INTEGER,
    PRIMARY KEY (code, ts)
);
CREATE INDEX IF NOT EXISTS idx_bars_date ON bars(trade_date, code);

CREATE TABLE IF NOT EXISTS daily (
    trade_date    TEXT PRIMARY KEY,
    start_equity  REAL,
    end_equity    REAL,
    pnl           REAL,
    pnl_pct       REAL,
    trades        INTEGER,
    wins          INTEGER,
    kill_switch   INTEGER DEFAULT 0,
    note          TEXT
);
"""


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Database:
    """스레드마다 별도 커넥션을 쓰는 얇은 SQLite 래퍼."""

    def __init__(self, path: Path = cfg.DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
        log.info("매매 DB 준비 완료: %s", self.path)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------ 기록
    def log_signal(self, sig, *, name: str = "", meta_prob: float | None = None,
                   meta_approved: bool | None = None, executed: bool = False) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO signals
                   (ts, trade_date, code, name, action, score, price, factors, features,
                    meta_prob, meta_approved, executed, reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    sig.time.isoformat(timespec="seconds"),
                    _today(),
                    sig.code,
                    name,
                    sig.action,
                    sig.score,
                    sig.price,
                    json.dumps(sig.factors, ensure_ascii=False),
                    json.dumps(sig.features, ensure_ascii=False),
                    meta_prob,
                    None if meta_approved is None else int(meta_approved),
                    int(executed),
                    sig.reason,
                ),
            )
            return int(cur.lastrowid)

    def mark_signal_executed(self, signal_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE signals SET executed = 1 WHERE id = ?", (signal_id,))

    def log_order(self, *, order_no: str, code: str, name: str, side: str, order_type: str,
                  qty: int, price: float, status: str, reason: str = "", dry_run: bool = False,
                  signal_id: int | None = None) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO orders
                   (ts, trade_date, order_no, code, name, side, order_type, qty, price,
                    status, reason, dry_run, signal_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_now(), _today(), order_no, code, name, side, order_type, qty, price,
                 status, reason, int(dry_run), signal_id),
            )
            return int(cur.lastrowid)

    def update_order_status(self, order_no: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE orders SET status = ? WHERE order_no = ?", (status, order_no))

    def log_fill(self, *, order_no: str, code: str, name: str, side: str, qty: int,
                 price: float, fee: float = 0.0, tax: float = 0.0, reason: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO fills
                   (ts, trade_date, order_no, code, name, side, qty, price, amount, fee, tax, reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_now(), _today(), order_no, code, name, side, qty, price, qty * price, fee, tax, reason),
            )

    def log_trade(self, *, code: str, name: str, entry_ts: str, exit_ts: str, qty: int,
                  entry_price: float, exit_price: float, exit_reason: str,
                  signal_id: int | None = None) -> None:
        pnl = (exit_price - entry_price) * qty
        pnl_pct = (exit_price / entry_price - 1.0) if entry_price else 0.0
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO trades
                   (trade_date, code, name, entry_ts, exit_ts, qty, entry_price, exit_price,
                    pnl, pnl_pct, exit_reason, signal_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_today(), code, name, entry_ts, exit_ts, qty, entry_price, exit_price,
                 pnl, pnl_pct, exit_reason, signal_id),
            )

    def save_bars(self, code: str, rows: list[dict[str, Any]]) -> None:
        """라벨링용 1분봉 스냅샷 저장(중복은 덮어쓰기)."""
        if not rows:
            return
        payload = [
            (
                _today(), code, r["time"].isoformat(timespec="seconds"),
                r["open"], r["high"], r["low"], r["close"], int(r.get("volume", 0)),
            )
            for r in rows
        ]
        with self.connect() as conn:
            conn.executemany(
                """INSERT INTO bars (trade_date, code, ts, open, high, low, close, volume)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(code, ts) DO UPDATE SET
                     close=excluded.close, high=excluded.high,
                     low=excluded.low, volume=excluded.volume""",
                payload,
            )

    def save_daily(self, *, start_equity: float, end_equity: float, trades: int,
                   wins: int, kill_switch: bool, note: str = "") -> None:
        pnl = end_equity - start_equity
        pnl_pct = (end_equity / start_equity - 1.0) if start_equity else 0.0
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO daily
                   (trade_date, start_equity, end_equity, pnl, pnl_pct, trades, wins, kill_switch, note)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(trade_date) DO UPDATE SET
                     end_equity=excluded.end_equity, pnl=excluded.pnl, pnl_pct=excluded.pnl_pct,
                     trades=excluded.trades, wins=excluded.wins,
                     kill_switch=excluded.kill_switch, note=excluded.note""",
                (_today(), start_equity, end_equity, pnl, pnl_pct, trades, wins, int(kill_switch), note),
            )

    # ------------------------------------------------------------ 조회
    def today_stats(self) -> dict:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n,
                          COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
                          COALESCE(SUM(pnl), 0) AS realized
                   FROM trades WHERE trade_date = ?""",
                (_today(),),
            ).fetchone()
        return {"trades": row["n"], "wins": row["wins"], "realized": row["realized"]}

    def rolling_stats(self, days: int = 30) -> dict:
        """켈리 사이징에 넣을 최근 승률/손익비."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT pnl_pct FROM trades
                   WHERE trade_date >= date('now', ?) AND pnl_pct IS NOT NULL""",
                (f"-{days} day",),
            ).fetchall()
        vals = [r["pnl_pct"] for r in rows]
        if len(vals) < 20:
            return {"n": len(vals), "win_rate": None, "payoff": None}
        wins = [v for v in vals if v > 0]
        losses = [-v for v in vals if v < 0]
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        return {
            "n": len(vals),
            "win_rate": len(wins) / len(vals),
            "payoff": (avg_win / avg_loss) if avg_loss > 0 else None,
        }

    def unlabeled_signals(self, action: str = "BUY") -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT id, ts, trade_date, code, price, features
                   FROM signals WHERE action = ? AND label IS NULL ORDER BY ts""",
                (action,),
            ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["features"] = json.loads(item["features"] or "{}")
            except ValueError:
                item["features"] = {}
            out.append(item)
        return out

    def set_label(self, signal_id: int, label: int, reason: str, ret: float) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE signals SET label = ?, label_reason = ?, label_return = ? WHERE id = ?",
                (label, reason, ret, signal_id),
            )

    def labeled_signals(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, code, ts, features, label FROM signals WHERE label IS NOT NULL"
            ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["features"] = json.loads(item["features"] or "{}")
            except ValueError:
                item["features"] = {}
            out.append(item)
        return out

    def bars_after(self, code: str, ts: str) -> list[tuple[str, float]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT ts, close FROM bars WHERE code = ? AND ts > ? ORDER BY ts", (code, ts)
            ).fetchall()
        return [(r["ts"], r["close"]) for r in rows]
