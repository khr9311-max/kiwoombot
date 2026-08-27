"""
기술적 지표. pandas 만 사용하므로 TA-Lib 같은 네이티브 빌드 의존성이 없다.
모든 함수는 마지막 값이 '확정된 봉' 기준이라고 가정한다(미완성 봉은 호출자가 제외).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss  # avg_loss==0 -> inf -> RSI 100
    out = 100.0 - (100.0 / (1.0 + rs))
    # 상승분·하락분이 모두 0인 완전 횡보 구간은 중립 50 으로 둔다.
    flat = (avg_gain == 0.0) & (avg_loss == 0.0)
    return out.mask(flat, 50.0)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder ATR."""
    return true_range(high, low, close).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def bollinger(series: pd.Series, period: int = 20, mult: float = 2.0) -> pd.DataFrame:
    mid = sma(series, period)
    sd = series.rolling(period, min_periods=period).std(ddof=0)
    return pd.DataFrame({"bb_mid": mid, "bb_upper": mid + mult * sd, "bb_lower": mid - mult * sd})


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(series, fast) - ema(series, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def frac_diff(series: pd.Series, d: float = 0.4, threshold: float = 1e-4) -> pd.Series:
    """
    분수 차분 (López de Prado). 정상성을 확보하면서 기억(memory)을 최대한 보존한다.
    메타 라벨링 피처로 사용한다.
    """
    weights = [1.0]
    k = 1
    while True:
        w = -weights[-1] * (d - k + 1) / k
        if abs(w) < threshold:
            break
        weights.append(w)
        k += 1
        if k > len(series):
            break
    w_arr = np.array(weights[::-1])
    width = len(w_arr)
    if width > len(series):
        return pd.Series(np.nan, index=series.index)
    values = series.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    for i in range(width - 1, len(values)):
        window = values[i - width + 1 : i + 1]
        if np.isnan(window).any():
            continue
        out[i] = float(np.dot(w_arr, window))
    return pd.Series(out, index=series.index)


def enrich(df: pd.DataFrame, *, rsi_period: int, ma_fast: int, ma_slow: int, atr_period: int) -> pd.DataFrame:
    """
    OHLCV DataFrame(컬럼: open/high/low/close/volume, index=시각) 에 지표 컬럼을 붙인다.
    """
    if df.empty:
        return df
    out = df.copy()
    close = out["close"].astype(float)
    out["ma_fast"] = sma(close, ma_fast)
    out["ma_slow"] = sma(close, ma_slow)
    out["rsi"] = rsi(close, rsi_period)
    out["atr"] = atr(out["high"].astype(float), out["low"].astype(float), close, atr_period)
    out = out.join(bollinger(close, ma_slow))
    out["vol_ma"] = sma(out["volume"].astype(float), ma_slow)
    return out
