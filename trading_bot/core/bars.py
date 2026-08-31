"""
실시간 체결 틱(0B)을 1분봉으로 집계하는 버퍼.

ka10080 으로 과거 봉을 워밍업한 뒤, 장중에는 웹소켓 틱만으로 봉을 이어 붙인다.
이렇게 하면 종목 수 × 폴링 주기만큼 REST 를 두드릴 필요가 없어 초당 호출 제한을
근본적으로 회피할 수 있다.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd


@dataclass
class Bar:
    time: datetime  # 봉 시작 시각 (분 단위 절삭)
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    def as_dict(self) -> dict:
        return {
            "time": self.time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass
class Snapshot:
    """해당 종목의 최신 실시간 상태."""

    code: str
    price: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    prev_close: float = 0.0
    cum_volume: int = 0
    cum_turnover: float = 0.0   # 누적거래대금 (필드 14)
    strength: float = 0.0       # 체결강도 (필드 228)
    ask: float = 0.0
    bid: float = 0.0
    updated: datetime | None = None


def floor_minute(ts: datetime, minutes: int = 1) -> datetime:
    total = ts.hour * 60 + ts.minute
    total -= total % minutes
    return ts.replace(hour=total // 60, minute=total % 60, second=0, microsecond=0)


class BarSeries:
    """한 종목의 봉 시계열 + 현재 형성 중인 봉."""

    def __init__(self, code: str, maxlen: int = 400, interval_min: int = 1):
        self.code = code
        self.interval = interval_min
        self._bars: deque[Bar] = deque(maxlen=maxlen)
        self._current: Bar | None = None
        self._last_cum_volume: int = 0
        # 누적거래량 기준선이 잡혔는지. 잡히기 전에는 차분으로 체결량을 복원하지 않는다
        # (0 을 기준선으로 빼면 첫 봉에 하루치 거래량이 통째로 들어간다).
        self._cum_volume_seeded = False
        self.snapshot = Snapshot(code=code)

    # ------------------------------------------------------------ 워밍업
    def warmup(self, rows: list[dict]) -> None:
        """ka10080 결과(과거→현재 오름차순)를 확정봉으로 적재한다."""
        self._bars.clear()
        for r in rows:
            self._bars.append(
                Bar(
                    time=floor_minute(r["time"], self.interval),
                    open=r["open"],
                    high=r["high"],
                    low=r["low"],
                    close=r["close"],
                    volume=int(r.get("volume", 0)),
                )
            )
        if rows:
            last = rows[-1]
            self.snapshot.price = last["close"]
            # ka10080 응답 스펙에는 누적거래량(acc_trde_qty)이 없다. 있으면 쓰고,
            # 없으면 첫 실시간 틱에서 기준선을 잡는다.
            cum = int(last.get("cum_volume") or 0)
            if cum > 0:
                self._last_cum_volume = cum
                self._cum_volume_seeded = True
                self.snapshot.cum_volume = cum

    # ------------------------------------------------------------ 틱 반영
    def on_tick(
        self,
        ts: datetime,
        price: float,
        cum_volume: int,
        *,
        tick_volume: int = 0,
        cum_turnover: float = 0.0,
        strength: float = 0.0,
        open_: float = 0.0,
        high: float = 0.0,
        low: float = 0.0,
        ask: float = 0.0,
        bid: float = 0.0,
    ) -> Bar | None:
        """
        틱을 반영하고, 이 틱으로 '직전 봉이 확정'되었으면 그 확정봉을 반환한다.
        확정봉이 없으면 None.
        """
        if price <= 0:
            return None

        s = self.snapshot
        s.price = price
        s.updated = ts
        if cum_volume:
            s.cum_volume = cum_volume
        if cum_turnover:
            s.cum_turnover = cum_turnover
        if strength:
            s.strength = strength
        if open_:
            s.open = open_
        if high:
            s.high = high
        if low:
            s.low = low
        if ask:
            s.ask = ask
        if bid:
            s.bid = bid

        # 누적거래량 차이로 이번 틱의 체결량을 복원(필드 15 가 비어 오는 경우 대비).
        # 기준선이 아직 없으면 차분을 쓰지 않는다 — 하루치 누적이 한 봉에 몰린다.
        if not tick_volume and cum_volume and self._cum_volume_seeded:
            tick_volume = max(cum_volume - self._last_cum_volume, 0)
        if cum_volume:
            self._last_cum_volume = cum_volume
            self._cum_volume_seeded = True

        slot = floor_minute(ts, self.interval)
        closed: Bar | None = None

        if self._current is None:
            self._current = Bar(slot, price, price, price, price, tick_volume)
            return None

        if slot > self._current.time:
            closed = self._current
            self._bars.append(closed)
            self._current = Bar(slot, price, price, price, price, tick_volume)
            return closed

        cur = self._current
        cur.high = max(cur.high, price)
        cur.low = min(cur.low, price)
        cur.close = price
        cur.volume += tick_volume
        return None

    def force_close(self) -> Bar | None:
        """장 마감 등에서 형성 중인 봉을 강제 확정."""
        if self._current is None:
            return None
        closed, self._current = self._current, None
        self._bars.append(closed)
        return closed

    # ------------------------------------------------------------ 조회
    def __len__(self) -> int:
        return len(self._bars)

    @property
    def last_close(self) -> float:
        if self.snapshot.price:
            return self.snapshot.price
        if self._current:
            return self._current.close
        return self._bars[-1].close if self._bars else 0.0

    @property
    def last_bar_time(self) -> datetime | None:
        return self._bars[-1].time if self._bars else None

    def to_frame(self, include_current: bool = False, tail: int | None = None) -> pd.DataFrame:
        """
        확정봉을 DataFrame 으로. 봉은 항상 시각 오름차순으로만 append 되므로
        정렬·중복제거를 하지 않는다(시그널 경로의 핫패스라 비용에 민감하다).
        tail 을 주면 최근 N개만 만든다.
        """
        src = list(self._bars)[-tail:] if tail else list(self._bars)
        if include_current and self._current is not None:
            src.append(self._current)
        if not src:
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume"],
                index=pd.DatetimeIndex([], name="time"),
            )
        return pd.DataFrame(
            {
                "open": [b.open for b in src],
                "high": [b.high for b in src],
                "low": [b.low for b in src],
                "close": [b.close for b in src],
                "volume": [b.volume for b in src],
            },
            index=pd.DatetimeIndex([b.time for b in src], name="time"),
        )


class BarStore:
    """종목 -> BarSeries 컨테이너. 웹소켓 콜백(단일 이벤트루프)에서만 쓰지만
    스케줄러 스레드에서도 읽으므로 조회 경로만 락으로 보호한다."""

    def __init__(self, maxlen: int = 400, interval_min: int = 1):
        self._series: dict[str, BarSeries] = {}
        self._maxlen = maxlen
        self._interval = interval_min
        self._lock = threading.RLock()

    def get(self, code: str) -> BarSeries:
        with self._lock:
            if code not in self._series:
                self._series[code] = BarSeries(code, self._maxlen, self._interval)
            return self._series[code]

    def has(self, code: str) -> bool:
        with self._lock:
            return code in self._series

    def codes(self) -> list[str]:
        with self._lock:
            return list(self._series)

    def drop(self, code: str) -> None:
        with self._lock:
            self._series.pop(code, None)

    def keep_only(self, codes: list[str]) -> None:
        keep = set(codes)
        with self._lock:
            for c in list(self._series):
                if c not in keep:
                    del self._series[c]

    def stale_codes(self, now: datetime, max_age: timedelta) -> list[str]:
        """max_age 동안 틱이 없던 종목(시세 끊김 감지용)."""
        with self._lock:
            return [
                c
                for c, s in self._series.items()
                if s.snapshot.updated is None or now - s.snapshot.updated > max_age
            ]
