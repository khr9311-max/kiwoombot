"""
[ML 확장 슬롯] 메타 라벨링 필터.

대시보드 문서의 2단계 아키텍처:
  1차 모델(strategy.SignalEngine)이 방향(Side=+1)과 진입 후보를 High-Recall 로 뽑고,
  2차 메타 모델(LightGBM)이 "그 진입이 성공할 확률" 만 이진 분류로 판정해
  P(y=1) >= META_PROB_THRESHOLD 일 때만 실제 주문을 낸다.

지금 당장은 학습 데이터가 없다. 그래서 기본값은 PassThroughFilter(항상 승인)이고,
봇은 돌아가는 내내 모든 1차 시그널과 그 피처 벡터를 DB(signals 테이블)에 남긴다.
2~4주 뒤 그 기록에 삼중 장벽 라벨을 붙이면(label_triple_barrier) 바로 학습이 가능하다.

  1) 데이터 축적:  META_FILTER_ENABLED=false 로 운용 (모의투자)
  2) 라벨링/학습:  python -m trading_bot.tools.train_meta
  3) 필터 가동:    META_FILTER_ENABLED=true
"""
from __future__ import annotations

import logging
import pickle
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import settings as cfg

log = logging.getLogger(__name__)

# 학습/추론에서 동일한 순서를 보장해야 하는 피처 목록
FEATURE_ORDER = [
    "rsi",
    "ma_gap",
    "atr_pct",
    "bb_pos",
    "vol_ratio",
    "ret_5",
    "ret_20",
    "turnover_ratio",
    "strength",
    "day_range_pos",
    "minute_of_day",
]


@dataclass
class MetaDecision:
    approved: bool
    probability: float
    reason: str


class BaseMetaFilter:
    def decide(self, features: dict[str, float]) -> MetaDecision:  # pragma: no cover - 인터페이스
        raise NotImplementedError


class PassThroughFilter(BaseMetaFilter):
    """메타 모델 없이 1차 시그널을 그대로 승인한다(기본값)."""

    def decide(self, features: dict[str, float]) -> MetaDecision:
        return MetaDecision(True, 1.0, "meta filter 미사용")


class LightGBMMetaFilter(BaseMetaFilter):
    """pickle 로 저장된 (model, feature_order) 를 읽어 확률을 판정한다."""

    def __init__(self, model_path: str | Path = cfg.META_MODEL_PATH,
                 threshold: float = cfg.META_PROB_THRESHOLD):
        self.threshold = threshold
        with Path(model_path).open("rb") as fh:
            bundle = pickle.load(fh)
        self.model = bundle["model"]
        self.features: list[str] = bundle.get("features", FEATURE_ORDER)
        log.info("메타 모델 로드: %s (피처 %d개, 임계 %.2f)", model_path, len(self.features), threshold)

    def decide(self, features: dict[str, float]) -> MetaDecision:
        row = pd.DataFrame([[float(features.get(f, 0.0)) for f in self.features]], columns=self.features)
        try:
            prob = float(self.model.predict_proba(row)[0][1])
        except Exception as exc:
            log.exception("메타 모델 추론 실패 — 보수적으로 거절: %s", exc)
            return MetaDecision(False, 0.0, f"추론 오류: {exc}")
        ok = prob >= self.threshold
        return MetaDecision(ok, prob, f"P(y=1)={prob:.3f} {'>=' if ok else '<'} {self.threshold:.2f}")


def build_filter() -> BaseMetaFilter:
    """설정에 따라 필터를 만든다. 모델이 없으면 조용히 PassThrough 로 폴백."""
    if not cfg.META_FILTER_ENABLED:
        return PassThroughFilter()
    path = Path(cfg.META_MODEL_PATH)
    if not path.exists():
        log.warning("META_FILTER_ENABLED=true 이지만 모델 파일이 없습니다: %s -> PassThrough", path)
        return PassThroughFilter()
    try:
        return LightGBMMetaFilter(path)
    except Exception as exc:
        log.error("메타 모델 로드 실패(%s) -> PassThrough", exc)
        return PassThroughFilter()


# ------------------------------------------------------------------ 라벨링
def label_triple_barrier(
    prices: pd.Series,
    entry_time,
    entry_price: float,
    atr: float,
    upper_mult: float = cfg.TB_UPPER_ATR_MULT,
    lower_mult: float = cfg.TB_LOWER_ATR_MULT,
    vertical_min: int = cfg.TB_VERTICAL_MIN,
) -> tuple[int, str, float]:
    """
    삼중 장벽법 (López de Prado).
      상단 장벽  entry + upper_mult * ATR   먼저 닿으면 y=1
      하단 장벽  entry - lower_mult * ATR   먼저 닿으면 y=0
      수직 장벽  entry_time + vertical_min분  만료 시 y=0

    prices: 진입 시각 이후의 1분봉 종가 시계열(index=시각).
    반환: (라벨, 사유, 실현수익률)
    """
    if atr <= 0 or entry_price <= 0 or prices.empty:
        return 0, "데이터 부족", 0.0

    window = prices[prices.index > entry_time]
    if vertical_min > 0:
        deadline = entry_time + timedelta(minutes=vertical_min)
        window = window[window.index <= deadline]
    if window.empty:
        return 0, "관측 구간 없음", 0.0

    upper = entry_price + upper_mult * atr
    lower = entry_price - lower_mult * atr

    for ts, px in window.items():
        if px >= upper:
            return 1, "상단 장벽", float(px / entry_price - 1.0)
        if px <= lower:
            return 0, "하단 장벽", float(px / entry_price - 1.0)

    final = float(window.iloc[-1])
    return 0, "수직 장벽(시간 만료)", float(final / entry_price - 1.0)


def features_to_matrix(rows: list[dict], feature_order: list[str] | None = None) -> tuple[pd.DataFrame, list[str]]:
    """DB 에서 읽은 피처 dict 리스트를 학습용 행렬로 만든다."""
    order = feature_order or FEATURE_ORDER
    data = [[float(r.get(f, np.nan)) for f in order] for r in rows]
    return pd.DataFrame(data, columns=order), order
