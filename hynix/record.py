"""실시간 체결(0B)·호가잔량(0D) 기록기. 매매는 하지 않는다.

왜 필요한가 — 장중 가설 중 과거 데이터로 검증할 수 있는 것은 2026-09-12 기준 전부
기각됐고(오프닝레인지, 갭 성격, 미국장 장중 전이, 외국인 수급, 5분 평균회귀), 남은
유일한 미검증 영역이 호가창이다. 그런데 호가 이력은 키움이 과거분을 제공하지 않는다.
지금부터 쌓아야 한 달 뒤에 검증할 수 있다.

trading_bot.core.kiwoom_ws 를 쓰지 않고 여기에 따로 구현한 이유: 그쪽은 라이브 봇이
쓰는 모듈이라 0D 지원을 끼워 넣으면 운영 중인 매매에 영향이 갈 수 있다.

0D 필드번호 (official_reference 실시간시세 예제 기준)
  21 호가시간 | 41~50 매도호가1~10 | 61~70 매도호가수량1~10
             | 51~60 매수호가1~10 | 71~80 매수호가수량1~10
  121 매도호가총잔량 | 125 매수호가총잔량 | 128 순매수잔량 | 129 매수비율
0B 필드번호
  20 체결시간 | 10 현재가 | 15 체결량 | 13 누적거래량 | 14 누적거래대금
  228 체결강도 | 27 최우선매도호가 | 28 최우선매수호가
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time as dtime
from typing import Any

import websockets

from trading_bot.config import settings as cfg
from trading_bot.core.kiwoom_client import KiwoomClient, parse_int, parse_price
from trading_bot.core.notifier import build_notifier

from . import store
from .config import TARGET, TARGET_NAME

log = logging.getLogger(__name__)

DEPTH = 5                      # 상위 몇 호가까지 남길 것인가
FLUSH_ROWS = 200               # 버퍼가 이만큼 차면 기록
FLUSH_SEC = 5.0                # 또는 이 간격마다 기록
QUOTE_MIN_INTERVAL_MS = 200    # 호가 스냅샷 최소 간격 (초당 5장 상한)

SCHEMA = """
CREATE TABLE IF NOT EXISTS rt_tick (
    code    TEXT NOT NULL,
    ts      TEXT NOT NULL,      -- YYYYMMDDHHMMSS (수신 시각 기준)
    hms     TEXT,               -- 키움이 준 체결시간 HHMMSS
    price   REAL,
    qty     INTEGER,            -- 체결량 (부호 = 매수/매도 구분)
    cum_qty INTEGER,
    cum_val INTEGER,
    strength REAL,              -- 체결강도
    ask1    REAL,
    bid1    REAL,
    PRIMARY KEY (code, ts, hms, cum_qty)
);
CREATE INDEX IF NOT EXISTS idx_rt_tick_day ON rt_tick(code, substr(ts,1,8));

CREATE TABLE IF NOT EXISTS rt_quote (
    code      TEXT NOT NULL,
    ts        TEXT NOT NULL,
    hms       TEXT,
    ask1      REAL, bid1 REAL,
    ask_tot   INTEGER,          -- 매도호가총잔량
    bid_tot   INTEGER,          -- 매수호가총잔량
    net_bal   INTEGER,          -- 순매수잔량
    buy_ratio REAL,             -- 매수비율
    exp_px    REAL,             -- 예상체결가 (동시호가 구간에서만 유효)
    exp_qty   INTEGER,          -- 예상체결수량
    ask_px    TEXT,             -- 상위 DEPTH 호가 JSON 배열
    ask_qty   TEXT,
    bid_px    TEXT,
    bid_qty   TEXT,
    PRIMARY KEY (code, ts, hms)
);
CREATE INDEX IF NOT EXISTS idx_rt_quote_day ON rt_quote(code, substr(ts,1,8));
"""


class Recorder:
    """0B/0D 를 받아 hynix.db 에 적재한다. 재접속·핑 응답까지 자체 처리."""

    # 08:30 부터 받는 이유: 장전 동시호가 구간(08:30~09:00)에 0D 가 예상체결가(23)와
    # 예상체결수량(24)을 실어 보낸다. 09:00 갭이 만들어지는 과정 그 자체라 갭 분석에
    # 쓸 수 있는 유일한 사전 데이터다.
    def __init__(self, code: str = TARGET, start_at: dtime = dtime(8, 30),
                 stop_at: dtime = dtime(15, 40)):
        self.code = code
        self.start_at = start_at
        self.stop_at = stop_at
        self._client = KiwoomClient()
        self._ticks: list[tuple] = []
        self._quotes: list[tuple] = []
        self._last_quote_ms = 0.0
        self._stop = asyncio.Event()
        self.n_tick = self.n_quote = 0
        self.n_reconnect = 0
        self._notifier = build_notifier()

    # ------------------------------------------------------------ 적재
    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y%m%d%H%M%S")

    def _on_tick(self, v: dict[str, str]) -> None:
        self._ticks.append((
            self.code, self._now(), (v.get("20") or "").strip(),
            parse_price(v.get("10")), int(_signed(v.get("15"))),
            parse_int(v.get("13")), parse_int(v.get("14")),
            parse_price(v.get("228")), parse_price(v.get("27")), parse_price(v.get("28")),
        ))

    def _on_quote(self, v: dict[str, str]) -> None:
        now_ms = asyncio.get_running_loop().time() * 1000
        if now_ms - self._last_quote_ms < QUOTE_MIN_INTERVAL_MS:
            return
        self._last_quote_ms = now_ms
        ask_px = [parse_price(v.get(str(41 + i))) for i in range(DEPTH)]
        ask_qty = [parse_int(v.get(str(61 + i))) for i in range(DEPTH)]
        bid_px = [parse_price(v.get(str(51 + i))) for i in range(DEPTH)]
        bid_qty = [parse_int(v.get(str(71 + i))) for i in range(DEPTH)]
        self._quotes.append((
            self.code, self._now(), (v.get("21") or "").strip(),
            ask_px[0], bid_px[0],
            parse_int(v.get("121")), parse_int(v.get("125")),
            int(_signed(v.get("128"))), parse_price(v.get("129")),
            parse_price(v.get("23")), parse_int(v.get("24")),
            json.dumps(ask_px), json.dumps(ask_qty),
            json.dumps(bid_px), json.dumps(bid_qty),
        ))

    def _flush(self) -> None:
        if not self._ticks and not self._quotes:
            return
        with store.connect() as con:
            if self._ticks:
                con.executemany(
                    "INSERT OR IGNORE INTO rt_tick (code,ts,hms,price,qty,cum_qty,cum_val,"
                    "strength,ask1,bid1) VALUES (?,?,?,?,?,?,?,?,?,?)", self._ticks)
                self.n_tick += len(self._ticks)
            if self._quotes:
                con.executemany(
                    "INSERT OR IGNORE INTO rt_quote (code,ts,hms,ask1,bid1,ask_tot,bid_tot,"
                    "net_bal,buy_ratio,exp_px,exp_qty,ask_px,ask_qty,bid_px,bid_qty)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", self._quotes)
                self.n_quote += len(self._quotes)
        self._ticks.clear()
        self._quotes.clear()

    # ------------------------------------------------------------ 루프
    async def _flusher(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=FLUSH_SEC)
            except asyncio.TimeoutError:
                pass
            self._flush()

    async def _clock(self) -> None:
        """장 마감 후 스스로 멈춘다."""
        while not self._stop.is_set():
            if datetime.now().time() >= self.stop_at:
                log.info("%s 도달 -> 기록 종료", self.stop_at)
                self._stop.set()
                return
            await asyncio.sleep(20)

    async def _session(self) -> None:
        token = self._client.access_token
        async with websockets.connect(cfg.WS_HOST, ping_interval=None, max_size=2**22) as ws:
            await ws.send(json.dumps({"trnm": "LOGIN", "token": token}))
            raw = json.loads(await ws.recv())
            if raw.get("return_code") not in (0, None):
                raise RuntimeError(f"LOGIN 실패: {raw}")
            await ws.send(json.dumps({
                "trnm": "REG", "grp_no": "1", "refresh": "1",
                "data": [{"item": [self.code], "type": ["0B", "0D"]}],
            }))
            log.info("실시간 등록: %s 0B+0D", self.code)
            while not self._stop.is_set():
                msg = json.loads(await ws.recv())
                trnm = msg.get("trnm")
                if trnm == "PING":
                    await ws.send(json.dumps(msg))   # 받은 그대로 되돌려야 연결 유지
                    continue
                if trnm != "REAL":
                    continue
                for item in msg.get("data") or []:
                    v = item.get("values") or {}
                    if item.get("type") == "0B":
                        self._on_tick(v)
                    elif item.get("type") == "0D":
                        self._on_quote(v)
                if len(self._ticks) + len(self._quotes) >= FLUSH_ROWS:
                    self._flush()

    async def run(self) -> None:
        # 장 시간 밖에서는 키움이 접속 직후 연결을 닫는다(1000 Bye). 가드가 없으면
        # 재접속 루프만 하염없이 돈다.
        now = datetime.now()
        if now.weekday() >= 5 or not (self.start_at <= now.time() < self.stop_at):
            log.info("장 시간이 아니라 기록을 시작하지 않는다 (%s~%s, 평일)",
                     self.start_at, self.stop_at)
            return
        store.init()
        with store.connect() as con:
            con.executescript(SCHEMA)
            # CREATE TABLE IF NOT EXISTS 는 이미 있는 테이블에 새 컬럼을 붙여주지
            # 않는다. 스키마가 늘어난 채로 기존 DB 를 만나면 INSERT 가 통째로
            # 실패하므로, 빠진 컬럼만 채워 넣는다.
            have = {r[1] for r in con.execute("PRAGMA table_info(rt_quote)")}
            for col, typ in (("exp_px", "REAL"), ("exp_qty", "INTEGER")):
                if col not in have:
                    con.execute(f"ALTER TABLE rt_quote ADD COLUMN {col} {typ}")
                    log.info("rt_quote 에 %s 컬럼 추가", col)
        tasks = [asyncio.create_task(self._flusher()), asyncio.create_task(self._clock())]
        backoff = 1.0
        try:
            while not self._stop.is_set():
                try:
                    await self._session()
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.n_reconnect += 1
                    log.warning("WS 끊김(%s) -> %.0fs 후 재접속", exc, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
        finally:
            self._stop.set()
            for t in tasks:
                t.cancel()
            self._flush()
            log.info("기록 종료: 체결 %d건 / 호가 %d건", self.n_tick, self.n_quote)
            self._report()

    # ------------------------------------------------------------ 알림
    def _report(self) -> None:
        """하루치 수집 결과를 텔레그램으로 보낸다.

        무인으로 도는 수집기라 조용히 실패하면 한 달치 데이터를 날리고도 모른다.
        '오늘 몇 건 쌓였나' 한 줄이 그걸 막는 최소 장치다.
        """
        today = datetime.now().strftime("%Y%m%d")
        try:
            with store.connect() as con:
                q = ("SELECT COUNT(*) FROM {} WHERE code=? AND substr(ts,1,8)=?")
                ticks = con.execute(q.format("rt_tick"), (self.code, today)).fetchone()[0]
                quotes = con.execute(q.format("rt_quote"), (self.code, today)).fetchone()[0]
                exp = con.execute(
                    "SELECT COUNT(*) FROM rt_quote WHERE code=? AND substr(ts,1,8)=?"
                    " AND exp_px > 0", (self.code, today)).fetchone()[0]
        except Exception as exc:
            self._notifier.error(f"🔴 기록기 집계 실패 ({self.code}): {exc}")
            return

        head = f"{TARGET_NAME}({self.code}) 기록 종료"
        body = (f"체결 {ticks:,}건 / 호가 {quotes:,}건\n"
                f"동시호가 예상체결 {exp:,}건\n"
                f"재접속 {self.n_reconnect}회")

        if ticks == 0 and quotes == 0:
            # 가장 중요한 실패 모드. 다만 주말이 아닌 임시휴장일(명절·대체공휴일 등)에도
            # 똑같이 0건이 나온다 — 휴장일 달력이 없어 둘을 구분하지 못하니, 이 알림이
            # 떴을 때 휴장일이었는지 먼저 확인할 것.
            self._notifier.error(f"🔴 {head} — 수집 0건. 휴장일이 아니면 연결/구독 확인 필요\n{body}")
        elif quotes == 0 or self.n_reconnect >= 10:
            self._notifier.warn(f"🟡 {head} — 부분 이상\n{body}")
        else:
            self._notifier.info(f"🟢 {head}\n{body}")


def _signed(value: Any) -> float:
    """체결량·순매수잔량처럼 부호가 뜻을 가지는 필드용."""
    s = str(value or "").strip().replace(",", "")
    if not s or s in ("+", "-"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    await Recorder().run()


if __name__ == "__main__":
    asyncio.run(main())
