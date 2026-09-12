"""000660 리서치용 데이터 저장소.

운영 중인 trading_bot/data/trading.db 와 별도 파일(hynix.db)을 쓴다.
수집이 라이브 매매 DB 를 건드리지 않게 하려는 것이고, 리서치 단계에서
스키마를 자주 바꾸게 되므로 격리해 두는 편이 안전하다.

테이블
  kr_daily    국내 일봉 OHLCV+거래대금 (ka10081)
  kr_flow     투자자별 순매수 금액/수량 (ka10060)
  kr_foreign  외국인 보유주식수·지분율 추이 (ka10008)
  ext_daily   외부 일봉 (yfinance: SKHY/SOX/MU/NVDA/USDKRW/선물)
  collect_log 수집 이력 (무엇을 언제 어디까지 받았는가)
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import DB_PATH

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS kr_daily (
    code    TEXT NOT NULL,
    dt      TEXT NOT NULL,          -- YYYYMMDD
    open    REAL, high REAL, low REAL, close REAL,
    volume  INTEGER,                -- 주
    value   INTEGER,                -- 원 (ka10081 trde_prica * 1e6)
    PRIMARY KEY (code, dt)
);

CREATE TABLE IF NOT EXISTS kr_flow (
    code        TEXT NOT NULL,
    dt          TEXT NOT NULL,
    close       REAL,
    volume      INTEGER,            -- 주 (ka10060 acc_trde_prica — 이름과 달리 거래량)
    frgnr_amt   INTEGER,            -- 이하 _amt 는 순매수 백만원
    orgn_amt    INTEGER,
    ind_amt     INTEGER,
    fnnc_invt_amt INTEGER,          -- 금융투자
    invtrt_amt  INTEGER,            -- 투신
    penfnd_amt  INTEGER,            -- 연기금
    etc_corp_amt INTEGER,           -- 기타법인
    frgnr_qty   INTEGER,            -- 이하 _qty 는 순매수 주식수
    orgn_qty    INTEGER,
    ind_qty     INTEGER,
    PRIMARY KEY (code, dt)
);

CREATE TABLE IF NOT EXISTS kr_foreign (
    code         TEXT NOT NULL,
    dt           TEXT NOT NULL,
    close        REAL,
    volume       INTEGER,
    chg_qty      INTEGER,           -- 외국인 보유 변동수량
    poss_stkcnt  INTEGER,           -- 외국인 보유주식수
    wght         REAL,              -- 외국인 지분율 %
    limit_exh_rt REAL,              -- 한도소진율 %
    PRIMARY KEY (code, dt)
);

CREATE TABLE IF NOT EXISTS kr_min (
    code    TEXT NOT NULL,
    tic     INTEGER NOT NULL,       -- 분 단위 (1/5/10/...)
    ts      TEXT NOT NULL,          -- YYYYMMDDHHMMSS (봉 시작시각)
    open    REAL, high REAL, low REAL, close REAL,
    volume  INTEGER,                -- 해당 봉 거래량(주)
    PRIMARY KEY (code, tic, ts)
);
CREATE INDEX IF NOT EXISTS idx_kr_min_day ON kr_min(code, tic, substr(ts,1,8));

CREATE TABLE IF NOT EXISTS ext_daily (
    ticker  TEXT NOT NULL,
    dt      TEXT NOT NULL,
    open    REAL, high REAL, low REAL, close REAL,
    volume  INTEGER,
    PRIMARY KEY (ticker, dt)
);

CREATE TABLE IF NOT EXISTS collect_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    source  TEXT NOT NULL,
    target  TEXT,
    rows    INTEGER,
    first_dt TEXT,
    last_dt  TEXT,
    note    TEXT
);
"""


def init(path: Path | None = None) -> None:
    p = path or DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with connect(p) as con:
        con.executescript(SCHEMA)
    log.info("hynix.db 준비 완료: %s", p)


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(path or DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def upsert(table: str, cols: list[str], rows: Iterable[dict[str, Any]]) -> int:
    """PK 충돌 시 덮어쓴다. 같은 날짜를 다시 받아도 안전하게 재수집할 수 있어야 한다."""
    rows = list(rows)
    if not rows:
        return 0
    ph = ",".join("?" * len(cols))
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({ph})"
    with connect() as con:
        con.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
    return len(rows)


def log_run(source: str, target: str, rows: int, first_dt: str = "", last_dt: str = "",
            note: str = "") -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO collect_log (ts, source, target, rows, first_dt, last_dt, note)"
            " VALUES (?,?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), source, target, rows,
             first_dt, last_dt, note),
        )


def validate() -> list[str]:
    """수집 데이터의 자명한 위반을 잡는다. 빈 리스트면 정상.

    2026-09-12 에 ka10080 분봉이 하락봉 가격을 음수로 돌려주는 걸 모르고 parse_signed
    로 받아 43% 행이 음수가 된 적이 있다. 눈으로 발견했는데(일중 변동폭이 -15% 로
    나옴), 그런 건 눈이 아니라 여기서 걸려야 한다.
    """
    problems: list[str] = []
    with connect() as con:
        def q1(sql: str) -> int:
            return con.execute(sql).fetchone()[0] or 0

        for tbl, cols in (("kr_daily", ("open", "high", "low", "close")),
                          ("kr_min", ("open", "high", "low", "close")),
                          ("ext_daily", ("open", "high", "low", "close")),
                          ("kr_flow", ("close",)), ("kr_foreign", ("close",))):
            for c in cols:
                n = q1(f"SELECT COUNT(*) FROM {tbl} WHERE {c} < 0")
                if n:
                    problems.append(f"{tbl}.{c}: 음수 가격 {n}행 (부호 처리 확인)")

        for tbl in ("kr_daily", "kr_min", "ext_daily"):
            n = q1(f"SELECT COUNT(*) FROM {tbl} WHERE high < low")
            if n:
                problems.append(f"{tbl}: high < low 인 행 {n}개")
            # yfinance 의 환율(KRW=X)은 종가 스냅샷 시점이 고저 집계창과 달라
            # 종가가 고저를 0.1% 안쪽으로 벗어나는 행이 정상적으로 생긴다(449행 실측).
            # 부호 뒤집힘 같은 진짜 오류는 수십 % 단위로 벗어나므로 여유를 둔다.
            n = q1(f"SELECT COUNT(*) FROM {tbl} WHERE close > 0 AND high > 0 AND ("
                   f"close > high * 1.002 OR close < low * 0.998)")
            if n:
                problems.append(f"{tbl}: 종가가 고저 범위를 0.2% 넘게 벗어난 행 {n}개")

        # 일봉과 분봉 종가가 같은 날 크게 어긋나면 둘 중 하나가 틀린 것이다
        n = q1("""SELECT COUNT(*) FROM kr_daily d JOIN (
                    SELECT code, substr(ts,1,8) dt, close FROM kr_min m WHERE tic=5
                      AND ts = (SELECT MAX(ts) FROM kr_min x
                                WHERE x.code=m.code AND x.tic=m.tic
                                  AND substr(x.ts,1,8)=substr(m.ts,1,8))
                  ) m ON d.code=m.code AND d.dt=m.dt
                  WHERE d.close > 0 AND ABS(d.close/m.close - 1) > 0.005""")
        if n:
            problems.append(f"일봉 종가와 5분봉 마지막 종가가 0.5% 넘게 다른 날 {n}개")
    return problems


def summary() -> str:
    """수집 현황 한 눈에 보기."""
    out: list[str] = []
    with connect() as con:
        for tbl, key in (("kr_daily", "code"), ("kr_flow", "code"),
                         ("kr_foreign", "code"), ("ext_daily", "ticker")):
            q = (f"SELECT {key} k, COUNT(*) n, MIN(dt) a, MAX(dt) b "
                 f"FROM {tbl} GROUP BY {key} ORDER BY {key}")
            for r in con.execute(q):
                out.append(f"  {tbl:11} {r['k']:12} {r['n']:>6}행  {r['a']} ~ {r['b']}")
        q = ("SELECT code||' '||tic||'분' k, COUNT(*) n, COUNT(DISTINCT substr(ts,1,8)) d,"
             " MIN(ts) a, MAX(ts) b FROM kr_min GROUP BY code, tic ORDER BY code, tic")
        for r in con.execute(q):
            out.append(f"  {'kr_min':11} {r['k']:12} {r['n']:>6}행  "
                       f"{r['a'][:8]} ~ {r['b'][:8]} ({r['d']}거래일)")
    return "\n".join(out) or "  (비어 있음)"
