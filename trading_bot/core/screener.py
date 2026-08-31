"""
[1단계] 장 전 종목 스크리닝 — 전 종목(2,500+) 을 감시 유니버스(10~30) 로 압축.

두 개의 데이터 소스를 조합한다.
  - 키움 ka10099 (종목정보 리스트): 종목 마스터 + 거래 가능 여부
      state       : 종목상태 (관리종목 / 거래정지 / 투자유의 등)
      auditInfo   : 감사정보 (투자주의환기종목 등)
      orderWarning: 투자경고/위험 단계 ("0" 이 정상)
      upName      : 업종명, ETF 는 "" 로 온다
  - pykrx: 일봉 시세 / 시가총액 (거래대금·이동평균·거래량 급증 판정)

data.krx.co.kr 은 비로그인 조회를 차단하므로(응답 400 "LOGOUT") pykrx 를 쓰려면
KRX_ID / KRX_PW 환경 변수가 반드시 있어야 한다. 없으면 pykrx 는 예외 대신 빈
DataFrame 을 돌려주고 안내문을 stdout 으로만 print 하기 때문에, 아래에서 stdout 을
가로채 로그로 남기고 원인을 ScreenerError 로 올려 텔레그램 알림까지 전달한다.

pykrx 는 날짜별 횡단면 조회가 종목별 조회보다 훨씬 빠르므로, 최근 N영업일 패널을
data/daily_panel.pkl 에 캐시해 두고 매일 없는 날짜만 증분으로 받아온다.
"""
from __future__ import annotations

import contextlib
import io
import logging
import os
import pickle
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd

from ..config import settings as cfg
from .kiwoom_client import KiwoomClient, norm_code, parse_price

log = logging.getLogger(__name__)

# 종목명에 포함되면 제외 (스팩/리츠 신주인수권 등)
NAME_EXCLUDE_KEYWORDS = ("스팩", "제1호", "리츠", "인수권")
# state / auditInfo 에 이 단어가 있으면 제외
STATE_EXCLUDE_KEYWORDS = ("관리", "거래정지", "정지", "투자유의", "환기", "정리매매", "우선주")
MARKET_CODES = {"KOSPI": "0", "KOSDAQ": "10"}

PANEL_PATH = cfg.DATA_DIR / "daily_panel.pkl"
UNIVERSE_PATH = cfg.DATA_DIR / "universe.json"

KRX_LOGIN_HINT = (
    "data.krx.co.kr 은 로그인 없는 시세 조회를 차단합니다. "
    "data.krx.co.kr 에서 무료 회원가입한 뒤 .env 에 KRX_ID / KRX_PW 를 넣으세요"
)


class ScreenerError(RuntimeError):
    """스크리닝을 진행할 수 없는 상태(데이터 소스 장애·인증 누락 등)."""


def krx_login_ready() -> bool:
    """pykrx 가 KRX 로그인에 쓰는 환경 변수가 채워져 있는지."""
    return bool(os.getenv("KRX_ID", "").strip() and os.getenv("KRX_PW", "").strip())


def _pykrx(fn, *args, **kwargs):
    """
    pykrx 호출 래퍼. pykrx 는 로그인 실패·JSON 파싱 실패를 예외가 아니라 stdout
    print 로 알리므로, 그대로 두면 원인이 로그 어디에도 남지 않는다.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    for line in buf.getvalue().splitlines():
        line = line.strip()
        if line:
            log.warning("pykrx: %s", line)
    return result


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
    오늘이 개장일인지 확인한다(주말 + 공휴일).
    조회에 실패하면 True 를 돌려준다 — 휴장일에 잘못 도는 것보다
    개장일에 봇이 안 도는 쪽이 더 나쁘기 때문이다. 휴장일에 돌아도 시세가 없어
    아무 일도 일어나지 않는다.
    """
    day = day or date.today()
    if day.weekday() >= 5:
        return False
    try:
        from pykrx import stock

        nearest = stock.get_nearest_business_day_in_a_week(day.strftime("%Y%m%d"), prev=False)
    except Exception as exc:
        log.warning("개장일 확인 실패(%s) — 개장일로 간주하고 진행합니다", exc)
        return True
    return str(nearest) == day.strftime("%Y%m%d")


# ------------------------------------------------------------------ 일봉 패널
def _business_days(end: date, count: int) -> list[str]:
    """주말을 뺀 최근 count 영업일(YYYYMMDD). 공휴일은 pykrx 가 빈 결과로 알려준다."""
    days: list[str] = []
    cur = end
    while len(days) < count:
        if cur.weekday() < 5:
            days.append(cur.strftime("%Y%m%d"))
        cur -= timedelta(days=1)
    return sorted(days)


def _load_panel() -> pd.DataFrame:
    try:
        with PANEL_PATH.open("rb") as fh:
            return pickle.load(fh)
    except (OSError, pickle.UnpicklingError, EOFError):
        return pd.DataFrame(columns=["date", "code", "close", "volume", "value"])


def _save_panel(panel: pd.DataFrame) -> None:
    try:
        with PANEL_PATH.open("wb") as fh:
            pickle.dump(panel, fh)
    except OSError as exc:
        log.warning("일봉 패널 캐시 저장 실패: %s", exc)


def build_panel(lookback_days: int = 90, markets: tuple[str, ...] = cfg.UNIVERSE_MARKETS) -> pd.DataFrame:
    """
    최근 lookback_days 영업일의 (date, code, close, volume, value) 패널.
    캐시에 없는 날짜만 pykrx 로 받아온다.
    """
    from pykrx import stock  # 무거운 import 라 함수 안에서

    wanted = _business_days(date.today() - timedelta(days=1), lookback_days)
    panel = _load_panel()
    have = set(panel["date"].unique()) if not panel.empty else set()
    missing = [d for d in wanted if d not in have]

    # 공휴일은 캐시에 영원히 안 채워지므로 missing 에 남는다. 따라서 "덜 받아온 것"
    # 자체는 정상이고, 조회가 예외로 터진 횟수만 장애 신호로 센다.
    if missing and not krx_login_ready():
        raise ScreenerError(f"KRX 계정(KRX_ID/KRX_PW)이 설정되지 않았습니다 — {KRX_LOGIN_HINT}")

    if missing:
        log.info("일봉 패널 증분 수집: %d일 (%s ~ %s)", len(missing), missing[0], missing[-1])
    new_frames: list[pd.DataFrame] = []
    errors = 0
    last_error = ""
    for day in missing:
        for market in markets:
            try:
                df = _pykrx(stock.get_market_ohlcv, day, market=market.upper())
            except Exception as exc:
                log.warning("pykrx %s %s 조회 실패: %s", day, market, exc)
                errors += 1
                last_error = str(exc)
                continue
            if df is None or df.empty or df["거래량"].sum() == 0:
                continue  # 휴장일
            part = pd.DataFrame(
                {
                    "date": day,
                    "code": df.index.astype(str),
                    "close": df["종가"].astype(float),
                    "volume": df["거래량"].astype(float),
                    "value": df["거래대금"].astype(float),
                }
            )
            new_frames.append(part.reset_index(drop=True))
            time.sleep(0.2)  # KRX 서버 배려

    if errors and not new_frames:
        raise ScreenerError(
            f"pykrx 일봉 조회가 {errors}건 모두 실패했습니다 (마지막 오류: {last_error}). "
            f"KRX 계정이 막혔거나 비밀번호가 바뀌었을 수 있습니다 — {KRX_LOGIN_HINT}"
        )
    if errors:
        log.warning("일봉 조회 일부 실패: %d건 (마지막 오류: %s)", errors, last_error)

    if new_frames:
        panel = pd.concat([panel, *new_frames], ignore_index=True)
        panel = panel.drop_duplicates(["date", "code"], keep="last")

    if not panel.empty:
        keep_from = min(wanted)
        panel = panel[panel["date"] >= keep_from].reset_index(drop=True)
        _save_panel(panel)
    return panel


def fetch_market_cap(markets: tuple[str, ...] = cfg.UNIVERSE_MARKETS) -> pd.Series:
    """가장 최근 영업일의 시가총액(원). index=code"""
    from pykrx import stock

    for day in reversed(_business_days(date.today(), 7)):
        frames = []
        for market in markets:
            try:
                df = _pykrx(stock.get_market_cap, day, market=market.upper())
            except Exception as exc:
                log.warning("pykrx 시가총액 %s %s 실패: %s", day, market, exc)
                continue
            if df is not None and not df.empty and df["시가총액"].sum() > 0:
                frames.append(df["시가총액"].astype(float))
        if frames:
            log.info("시가총액 기준일: %s", day)
            return pd.concat(frames)
    log.error("시가총액 조회 실패 — 시총 필터를 건너뜁니다")
    return pd.Series(dtype=float)


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

    panel = build_panel(lookback_days=max(cfg.MA_TREND_PERIOD + 20, 90))
    if panel.empty:
        raise ScreenerError(f"일봉 패널이 비어 있습니다 — {KRX_LOGIN_HINT}")

    panel = panel[panel["code"].isin(set(master["code"]))]
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
    vol_5 = wide_vol.tail(5).mean()
    vol_20 = wide_vol.tail(20).mean()
    vol_surge = (vol_5 / vol_20.replace(0.0, pd.NA)).astype(float)
    ret_5d = (wide_close.iloc[-1] / wide_close.iloc[-6] - 1.0) if len(wide_close) > 5 else last_close * 0
    market_cap = fetch_market_cap()

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


def load_universe(max_age_hours: int = 12) -> list[str]:
    """오늘 만들어 둔 유니버스를 다시 읽는다(프로세스 재시작 복구용)."""
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
    return [i["code"] for i in payload.get("items", [])]
