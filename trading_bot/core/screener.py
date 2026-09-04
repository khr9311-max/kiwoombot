"""
[1단계] 장 전 종목 스크리닝 — 전 종목(2,500+) 을 감시 유니버스(10~30) 로 압축.

키움 REST API 만으로 구성한다.
  - ka10099 (종목정보 리스트): 종목 마스터 + 거래 가능 여부 + 현재가/상장주식수
      state       : 종목상태 (관리종목 / 거래정지 / 투자유의 등)
      auditInfo   : 감사정보 (투자주의환기종목 등)
      orderWarning: 투자경고/위험 단계 ("0" 이 정상)
      upName      : 업종명, ETF 는 "" 로 온다
      lastPrice/listCount: 시가총액 계산용 (별도 API 불필요)
  - ka10081 (주식일봉차트조회): 종목별 일봉 (거래대금·이동평균·거래량 급증 판정)

예전에는 pykrx 로 KRX(data.krx.co.kr) 를 스크래핑했으나, KRX Data Marketplace
이용약관 제10조 제2호가 "자동화 수단을 통한 정보 수집"을 명시적으로 금지하고
있고 실제로 접속 IP 가 하루 차단당하는 사고가 있었다. 게다가 pykrx 는 실제
호출 여부와 무관하게 import 시점에 KRX 로그인을 시도한다. 그래서 일봉/시총
조회를 전부 키움 공식 API 로 옮겼다 — 속도는 더 느리지만(종목별 순차 호출)
약관 위반·차단 위험이 없다.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd

from ..config import settings as cfg
from .kiwoom_client import KiwoomAPIError, KiwoomClient, norm_code, parse_price

log = logging.getLogger(__name__)

# 종목명에 포함되면 제외 (스팩/리츠 신주인수권 등)
NAME_EXCLUDE_KEYWORDS = ("스팩", "제1호", "리츠", "인수권")
# state / auditInfo 에 이 단어가 있으면 제외
STATE_EXCLUDE_KEYWORDS = ("관리", "거래정지", "정지", "투자유의", "환기", "정리매매", "우선주")
MARKET_CODES = {"KOSPI": "0", "KOSDAQ": "10"}

UNIVERSE_PATH = cfg.DATA_DIR / "universe.json"


class ScreenerError(RuntimeError):
    """스크리닝을 진행할 수 없는 상태(데이터 소스 장애 등)."""


@dataclass
class Candidate:
    code: str
    name: str
    market: str
    close: float
    market_cap: float
    avg_value_20d: float
    ma60: float
    vol_surge: float
    score: float

    def as_row(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "market": self.market,
            "close": self.close,
            "market_cap": self.market_cap,
            "avg_value_20d": self.avg_value_20d,
            "ma60": self.ma60,
            "vol_surge": round(self.vol_surge, 2),
            "score": round(self.score, 4),
        }


# ------------------------------------------------------------------ 종목 마스터
def fetch_master(client: KiwoomClient, markets: tuple[str, ...] = cfg.UNIVERSE_MARKETS) -> pd.DataFrame:
    """ka10099 종목정보 리스트로 거래 가능한 보통주 마스터를 만든다."""
    rows: list[dict] = []
    for market in markets:
        mrkt_tp = MARKET_CODES.get(market.upper())
        if mrkt_tp is None:
            log.warning("알 수 없는 시장 구분: %s", market)
            continue
        data, _, _ = client.request("ka10099", "/api/dostk/stkinfo", {"mrkt_tp": mrkt_tp})
        for r in data.get("list") or []:
            rows.append(
                {
                    "code": norm_code(r.get("code", "")),
                    "name": (r.get("name") or "").strip(),
                    "market": market.upper(),
                    "state": (r.get("state") or "").strip(),
                    "audit": (r.get("auditInfo") or "").strip(),
                    "order_warning": (r.get("orderWarning") or "0").strip(),
                    "sector": (r.get("upName") or "").strip(),
                    "last_price": parse_price(r.get("lastPrice")),
                    "listing_shares": parse_price(r.get("listCount")),
                    "reg_day": (r.get("regDay") or "").strip(),
                }
            )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df[df["code"].str.len() == 6].drop_duplicates("code").reset_index(drop=True)


def filter_tradable(master: pd.DataFrame) -> pd.DataFrame:
    """관리종목·거래정지·투자경고·ETF/ETN·스팩 등을 제외한다."""
    if master.empty:
        return master
    df = master.copy()
    before = len(df)

    bad_state = df["state"].str.contains("|".join(STATE_EXCLUDE_KEYWORDS), na=False)
    bad_audit = df["audit"].str.contains("|".join(STATE_EXCLUDE_KEYWORDS), na=False)
    bad_name = df["name"].str.contains("|".join(NAME_EXCLUDE_KEYWORDS), na=False)
    # ka10099 는 ETF/ETN 의 업종명(upName)을 빈 문자열로 준다.
    is_etf = df["sector"].eq("")
    # 우선주는 코드 끝자리가 0 이 아니다 (보통주만 남긴다).
    is_preferred = ~df["code"].str.endswith("0")
    warned = df["order_warning"].ne("0")

    df = df[~(bad_state | bad_audit | bad_name | is_etf | is_preferred | warned)]
    log.info("거래 가능 종목 필터: %d -> %d", before, len(df))
    return df.reset_index(drop=True)


def is_trading_day(day: date | None = None) -> bool:
    """
    오늘이 개장일인지 확인한다(주말만 걸러낸다).
    공휴일은 걸러내지 않는다 — 휴장일에 스크리닝이 돌아도 시세가 없어
    아무 일도 일어나지 않으므로, 굳이 외부 휴장일 조회에 기대지 않는다.
    """
    day = day or date.today()
    return day.weekday() < 5


# ------------------------------------------------------------------ 일봉 패널
def build_panel(client: KiwoomClient, codes: list[str], lookback_days: int = 90) -> pd.DataFrame:
    """키움 ka10081 로 종목별 일봉을 받아 (date, code, close, volume, value) 패널을 만든다."""
    rows: list[dict] = []
    errors = 0
    last_error = ""
    total = len(codes)
    for i, code in enumerate(codes, 1):
        try:
            bars = client.get_daily_chart(code, count=lookback_days)
        except KiwoomAPIError as exc:
            errors += 1
            last_error = exc.msg
            continue
        finally:
            # ka10081 서버측 제한이 REST_RATE_PER_SEC(주문·계좌 조회용) 보다 훨씬
            # 엄격해서, 수천 종목을 그 속도로 두들기면 거의 매 호출이 429 로 막힌다.
            time.sleep(cfg.SCREEN_REQUEST_DELAY_SEC)
        for b in bars:
            rows.append(
                {"date": b["date"], "code": code, "close": b["close"], "volume": b["volume"], "value": b["value"]}
            )
        if i % 200 == 0:
            log.info("일봉 수집 진행: %d/%d (오류 %d건)", i, total, errors)

    if errors and not rows:
        raise ScreenerError(f"키움 일봉 조회가 {errors}건 모두 실패했습니다 (마지막 오류: {last_error})")
    if errors:
        log.warning("일봉 조회 일부 실패: %d/%d건 (마지막 오류: %s)", errors, total, last_error)

    return pd.DataFrame(rows, columns=["date", "code", "close", "volume", "value"])


# ------------------------------------------------------------------ 스크리닝
def screen(client: KiwoomClient, top_n: int = cfg.UNIVERSE_MAX) -> list[Candidate]:
    """장 전 유니버스 압축. 조건을 통과한 종목을 점수 내림차순으로 반환."""
    if cfg.FIXED_UNIVERSE:
        log.warning("FIXED_UNIVERSE 설정됨 — 스크리닝을 건너뜁니다: %s", ", ".join(cfg.FIXED_UNIVERSE))
        return [
            Candidate(c, c, "FIXED", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0) for c in cfg.FIXED_UNIVERSE[:top_n]
        ]

    master = filter_tradable(fetch_master(client))
    if master.empty:
        raise ScreenerError("종목 마스터가 비어 있습니다 (키움 ka10099 응답 확인)")

    panel = build_panel(client, master["code"].tolist(), lookback_days=max(cfg.MA_TREND_PERIOD + 20, 90))
    if panel.empty:
        raise ScreenerError("일봉 패널이 비어 있습니다 (키움 ka10081 응답 확인)")

    wide_close = panel.pivot(index="date", columns="code", values="close").sort_index()
    wide_vol = panel.pivot(index="date", columns="code", values="volume").sort_index()
    wide_val = panel.pivot(index="date", columns="code", values="value").sort_index()

    if len(wide_close) < cfg.MA_TREND_PERIOD:
        raise ScreenerError(
            f"일봉 이력 부족: {len(wide_close)}일 < {cfg.MA_TREND_PERIOD}일 "
            "(MA_TREND_PERIOD 를 줄이거나 패널이 채워질 때까지 기다리세요)"
        )

    last_close = wide_close.iloc[-1]
    ma_trend = wide_close.tail(cfg.MA_TREND_PERIOD).mean()
    avg_val_20 = wide_val.tail(20).mean()
    vol_5 = pd.to_numeric(wide_vol.tail(5).mean(), errors="coerce")
    vol_20 = pd.to_numeric(wide_vol.tail(20).mean(), errors="coerce")
    # 20일 평균 거래량이 0 인 무거래 종목은 급증비를 계산할 수 없으니 NaN 으로 둔다.
    # 여기서 pd.NA 를 쓰면 Series 가 object dtype 이 되어 아래 astype(float) 이
    # "float() argument must be a string or a real number, not 'NAType'" 로 터지고
    # 스크리닝 전체가 실패한다 — float 를 유지하려면 NaN 이어야 한다.
    vol_surge = (vol_5 / vol_20.replace(0.0, float("nan"))).astype(float)
    ret_5d = (wide_close.iloc[-1] / wide_close.iloc[-6] - 1.0) if len(wide_close) > 5 else last_close * 0

    # 시가총액 = 현재가 x 상장주식수 (ka10099 마스터에 이미 들어 있어 별도 조회가 필요 없다)
    master_idx = master.set_index("code")
    market_cap = master_idx["last_price"] * master_idx["listing_shares"]

    stats = pd.DataFrame(
        {
            "close": last_close,
            "ma60": ma_trend,
            "avg_value_20d": avg_val_20,
            "vol_surge": vol_surge,
            "ret_5d": ret_5d,
        }
    ).dropna(subset=["close", "ma60"])
    stats["market_cap"] = market_cap.reindex(stats.index)

    passed = stats[
        (stats["avg_value_20d"] >= cfg.MIN_TRADING_VALUE)
        & (stats["close"] >= cfg.MIN_PRICE)
        & (stats["close"] <= cfg.MAX_PRICE)
        & (stats["close"] > stats["ma60"])
        & (stats["vol_surge"] >= cfg.VOLUME_SURGE_RATIO)
    ]
    if cfg.MIN_MARKET_CAP > 0:
        passed = passed[passed["market_cap"].fillna(0) >= cfg.MIN_MARKET_CAP]
    if cfg.MAX_MARKET_CAP > 0:
        passed = passed[passed["market_cap"].fillna(0) <= cfg.MAX_MARKET_CAP]

    if passed.empty:
        log.warning("스크리닝 통과 종목 없음 — 조건을 완화하세요")
        return []

    # 랭킹: 거래대금 / 거래량 급증 / 60일선 이격도를 백분위로 합산
    rank = (
        passed["avg_value_20d"].rank(pct=True) * 0.4
        + passed["vol_surge"].rank(pct=True) * 0.4
        + (passed["close"] / passed["ma60"]).rank(pct=True) * 0.2
    )
    passed = passed.assign(score=rank).sort_values("score", ascending=False)

    names = master.set_index("code")[["name", "market"]]
    out: list[Candidate] = []
    for code, row in passed.head(top_n).iterrows():
        meta = names.loc[code] if code in names.index else {"name": code, "market": ""}
        out.append(
            Candidate(
                code=str(code),
                name=str(meta["name"]),
                market=str(meta["market"]),
                close=float(row["close"]),
                market_cap=float(row["market_cap"]) if pd.notna(row["market_cap"]) else 0.0,
                avg_value_20d=float(row["avg_value_20d"]),
                ma60=float(row["ma60"]),
                vol_surge=float(row["vol_surge"]),
                score=float(row["score"]),
            )
        )

    log.info("스크리닝 완료: 후보 %d / 통과 %d / 선정 %d", len(stats), len(passed), len(out))
    return out


def save_universe(candidates: list[Candidate]) -> None:
    import json

    payload = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "items": [c.as_row() for c in candidates],
    }
    UNIVERSE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_universe(max_age_hours: int = 12) -> list[dict]:
    """
    오늘 만들어 둔 유니버스를 다시 읽는다(프로세스 재시작·스크리닝 실패 복구용).

    저장된 원본 항목(code/name/avg_value_20d 등)을 그대로 돌려준다 — 코드만 돌려주면
    폴백 경로에서 Factor A 판정용 전일 거래대금(prev_turnover) 기준선을 잃어버려
    거래대금 조건을 영원히 통과할 수 없게 된다(하루 종일 조용한 매매 중단).
    """
    import json

    try:
        payload = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    try:
        gen = datetime.fromisoformat(payload["generated_at"])
    except (KeyError, ValueError):
        return []
    if (datetime.now() - gen) > timedelta(hours=max_age_hours):
        log.warning("저장된 유니버스가 오래됨(%s) — 무시", payload.get("generated_at"))
        return []
    return list(payload.get("items", []))
