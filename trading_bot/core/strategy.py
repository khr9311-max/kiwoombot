"""
[2단계] 장중 실시간 시그널 엔진 — 다중 팩터 스코어링.

가이드 문서의 모델을 그대로 구현한다.
  Factor A  당일 거래대금 유입: 누적거래대금 >= 전일 거래대금 × 30%   -> +2점
  Factor B  단기 정배열: 1분봉 5선 > 20선 (직전 봉에서 골든크로스면 가산) -> +1점
  Factor C  모멘텀: RSI(14) 가 50 이상이며 우상향                        -> +1점
  Factor D  체결강도 110% 이상 (매수세 우위)                             -> +1점
  합산 4점 이상 -> BUY

여기서 나오는 것은 '1차 시그널(Primary Model)' 이다. 대시보드 문서의 메타 라벨링
아키텍처에서 이 시그널은 High-Recall 후보이고, 실제 발주 여부는 meta_filter 가
P(y=1) >= 임계치일 때만 승인한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from ..config import settings as cfg
from . import indicators
from .bars import BarSeries

log = logging.getLogger(__name__)


@dataclass
class Signal:
    code: str
    action: str                  # "BUY" | "NONE"
    score: float
    price: float
    time: datetime
    factors: dict[str, float] = field(default_factory=dict)
    features: dict[str, float] = field(default_factory=dict)
    reason: str = ""

    @property
    def is_buy(self) -> bool:
        return self.action == "BUY"


class SignalEngine:
    """종목별 봉 시계열을 받아 1차 시그널을 산출한다."""

    def __init__(self, prev_turnover: dict[str, float] | None = None):
        # 전일 거래대금(원). 장 전 스크리닝에서 채워 넣는다.
        self.prev_turnover: dict[str, float] = prev_turnover or {}
        # 봉이 바뀔 때까지 지표 계산 결과를 재사용한다(핫패스 캐시).
        self._cache: dict[str, tuple[datetime, pd.DataFrame]] = {}

    def set_prev_turnover(self, mapping: dict[str, float]) -> None:
        self.prev_turnover = dict(mapping)

    # ------------------------------------------------------------ 지표
    # ewm 기반 지표(RSI/ATR)가 충분히 수렴하도록 지표 주기의 배수만큼만 잘라 쓴다.
    _WINDOW_MULT = 6

    def _frame(self, series: BarSeries) -> pd.DataFrame | None:
        need = max(cfg.MA_SLOW, cfg.RSI_PERIOD, cfg.ATR_PERIOD) + 2
        if len(series) < need:
            return None

        last_ts = series.last_bar_time
        cached = self._cache.get(series.code)
        if cached is not None and cached[0] == last_ts:
            return cached[1]

        window = max(need, max(cfg.MA_SLOW, cfg.RSI_PERIOD, cfg.ATR_PERIOD) * self._WINDOW_MULT)
        df = indicators.enrich(
            series.to_frame(include_current=False, tail=window),
            rsi_period=cfg.RSI_PERIOD,
            ma_fast=cfg.MA_FAST,
            ma_slow=cfg.MA_SLOW,
            atr_period=cfg.ATR_PERIOD,
        )
        if last_ts is not None:
            self._cache[series.code] = (last_ts, df)
            if len(self._cache) > 200:
                self._cache.clear()
        return df

    # ------------------------------------------------------------ 시그널
    def evaluate(self, series: BarSeries) -> Signal:
        code = series.code
        now = series.snapshot.updated or datetime.now()
        price = series.last_close

        df = self._frame(series)
        if df is None or price <= 0:
            return Signal(code, "NONE", 0.0, price, now, reason="봉 데이터 부족")

        last = df.iloc[-1]
        prev = df.iloc[-2]
        snap = series.snapshot
        factors: dict[str, float] = {}

        # --- Factor A: 당일 거래대금 유입
        prev_val = self.prev_turnover.get(code, 0.0)
        turnover_ratio = (snap.cum_turnover / prev_val) if prev_val > 0 else 0.0
        factors["A_turnover"] = cfg.FACTOR_A_WEIGHT if turnover_ratio >= cfg.FACTOR_A_TURNOVER_RATIO else 0.0

        # --- Factor B: 단기 이평 정배열 (직전 봉 골든크로스면 신선한 신호)
        ma_fast, ma_slow = float(last["ma_fast"]), float(last["ma_slow"])
        aligned = ma_fast > ma_slow
        crossed_now = aligned and float(prev["ma_fast"]) <= float(prev["ma_slow"])
        factors["B_ma"] = cfg.FACTOR_B_MA_WEIGHT if aligned else 0.0
        factors["B_cross"] = cfg.FACTOR_B_CROSS_WEIGHT if crossed_now else 0.0

        # --- Factor C: RSI 50 이상 우상향
        rsi_now, rsi_prev = float(last["rsi"]), float(prev["rsi"])
        factors["C_rsi"] = cfg.FACTOR_C_WEIGHT if (rsi_now >= 50.0 and rsi_now > rsi_prev) else 0.0

        # --- Factor D: 체결강도
        factors["D_strength"] = cfg.FACTOR_D_WEIGHT if snap.strength >= cfg.FACTOR_D_STRENGTH else 0.0

        score = sum(factors.values())

        # 과열 차단: 볼린저 상단 돌파 + RSI 과매수 구간은 진입하지 않는다.
        bb_upper = float(last["bb_upper"]) if pd.notna(last["bb_upper"]) else float("inf")
        overbought = price > bb_upper and rsi_now >= 80.0
        if overbought:
            return Signal(code, "NONE", score, price, now, factors, self._features(df, snap, turnover_ratio),
                          reason="과열(BB상단+RSI80)")

        action = "BUY" if score >= cfg.SIGNAL_SCORE_THRESHOLD else "NONE"
        reason = f"score={score:.1f} " + " ".join(f"{k}={v:g}" for k, v in factors.items() if v)
        return Signal(code, action, score, price, now, factors,
                      self._features(df, snap, turnover_ratio), reason)

    # ------------------------------------------------------------ 피처
    @staticmethod
    def _features(df: pd.DataFrame, snap, turnover_ratio: float) -> dict[str, float]:
        """메타 모델(LightGBM) 입력 및 매매일지 기록용 피처 벡터."""
        last = df.iloc[-1]
        close = float(last["close"])
        atr_v = float(last["atr"]) if pd.notna(last["atr"]) else 0.0
        vol_ma = float(last["vol_ma"]) if pd.notna(last["vol_ma"]) and last["vol_ma"] else 1.0
        ret_5 = float(df["close"].pct_change(5).iloc[-1]) if len(df) > 5 else 0.0
        ret_20 = float(df["close"].pct_change(20).iloc[-1]) if len(df) > 20 else 0.0
        bb_mid = float(last["bb_mid"]) if pd.notna(last["bb_mid"]) and last["bb_mid"] else close

        return {
            "rsi": float(last["rsi"]) if pd.notna(last["rsi"]) else 50.0,
            "ma_gap": (float(last["ma_fast"]) / float(last["ma_slow"]) - 1.0)
            if pd.notna(last["ma_slow"]) and last["ma_slow"]
            else 0.0,
            "atr_pct": atr_v / close if close else 0.0,
            "bb_pos": (close / bb_mid - 1.0),
            "vol_ratio": float(last["volume"]) / vol_ma,
            "ret_5": 0.0 if pd.isna(ret_5) else ret_5,
            "ret_20": 0.0 if pd.isna(ret_20) else ret_20,
            "turnover_ratio": turnover_ratio,
            "strength": float(snap.strength),
            "day_range_pos": (
                (close - snap.low) / (snap.high - snap.low)
                if snap.high > snap.low > 0
                else 0.5
            ),
            "minute_of_day": float(df.index[-1].hour * 60 + df.index[-1].minute),
        }

    # ------------------------------------------------------------ 청산 보조
    def atr_of(self, series: BarSeries) -> float:
        df = self._frame(series)
        if df is None:
            return 0.0
        val = df["atr"].iloc[-1]
        return float(val) if pd.notna(val) else 0.0
