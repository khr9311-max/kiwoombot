"""
전역 설정. 모든 값은 .env 로 덮어쓸 수 있다.

계좌 환경 3단계:
  KIWOOM_ENV=mock  -> https://mockapi.kiwoom.com  (모의투자, 실제 주문 전송)
  KIWOOM_ENV=real  -> https://api.kiwoom.com      (실계좌)
  DRY_RUN=true     -> 위 환경과 무관하게 주문 API 만 전송하지 않고 로그로 남김
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time as dtime
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _b(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "y", "on")


def _i(key: str, default: int) -> int:
    return int(os.getenv(key, default))


def _f(key: str, default: float) -> float:
    return float(os.getenv(key, default))


def _hhmm(key: str, default: str) -> dtime:
    h, m = os.getenv(key, default).split(":")
    return dtime(int(h), int(m))


# ---------------------------------------------------------------- 접속/인증
KIWOOM_ENV = os.getenv("KIWOOM_ENV", "mock").strip().lower()
if KIWOOM_ENV not in ("mock", "real"):
    raise ValueError(f"KIWOOM_ENV must be 'mock' or 'real', got {KIWOOM_ENV!r}")

IS_MOCK = KIWOOM_ENV == "mock"
REST_HOST = "https://mockapi.kiwoom.com" if IS_MOCK else "https://api.kiwoom.com"
WS_HOST = (
    "wss://mockapi.kiwoom.com:10000/api/dostk/websocket"
    if IS_MOCK
    else "wss://api.kiwoom.com:10000/api/dostk/websocket"
)

# 키움은 실전과 모의투자의 앱키가 서로 다르다. 환경별 키를 따로 두고,
# 없으면 공통 키로 폴백한다. (키가 환경과 어긋나면 return_code 8031 이 난다)
_suffix = "MOCK" if IS_MOCK else "REAL"
APP_KEY = os.getenv(f"KIWOOM_APP_KEY_{_suffix}") or os.getenv("KIWOOM_APP_KEY", "")
APP_SECRET = os.getenv(f"KIWOOM_APP_SECRET_{_suffix}") or os.getenv("KIWOOM_APP_SECRET", "")

# 실계좌에서 실수로 주문이 나가는 것을 막는 최후 방어선.
# 실계좌인데 DRY_RUN 을 명시적으로 false 로 두지 않으면 자동으로 DRY_RUN 이 켜진다.
DRY_RUN = _b("DRY_RUN", default=not IS_MOCK)

# 국내거래소 구분: KRX / NXT / SOR
DMST_STEX_TP = os.getenv("DMST_STEX_TP", "KRX").strip().upper()

# ---------------------------------------------------------------- 레이트리밋
# 키움 REST 초당 호출 제한 방어용 토큰 버킷 (보수적으로 설정)
REST_RATE_PER_SEC = _f("REST_RATE_PER_SEC", 4.0)
REST_BURST = _i("REST_BURST", 8)
REST_TIMEOUT = _f("REST_TIMEOUT", 10.0)
REST_MAX_RETRY = _i("REST_MAX_RETRY", 3)

# ---------------------------------------------------------------- 운용 시간
TZ = os.getenv("TZ_NAME", "Asia/Seoul")
SCREENING_TIME = _hhmm("SCREENING_TIME", "08:10")          # 장 전 유니버스 압축
SESSION_START = _hhmm("SESSION_START", "09:00")            # 시그널 엔진 가동
NO_NEW_ENTRY_AFTER = _hhmm("NO_NEW_ENTRY_AFTER", "14:30")  # 신규 진입 중단
FLATTEN_TIME = _hhmm("FLATTEN_TIME", "15:15")              # 당일 포지션 일괄 청산
SESSION_END = _hhmm("SESSION_END", "15:25")                # 엔진 종료
EOD_REPORT_TIME = _hhmm("EOD_REPORT_TIME", "16:00")        # 장 마감 후 리포트

# ---------------------------------------------------------------- 스크리닝
UNIVERSE_MAX = _i("UNIVERSE_MAX", 20)
MIN_TRADING_VALUE = _f("MIN_TRADING_VALUE", 5_000_000_000)   # 20일 평균 거래대금 50억
MIN_MARKET_CAP = _f("MIN_MARKET_CAP", 100_000_000_000)       # 시가총액 1,000억
MAX_MARKET_CAP = _f("MAX_MARKET_CAP", 0)                     # 0 = 상한 없음
MIN_PRICE = _f("MIN_PRICE", 2_000)
MAX_PRICE = _f("MAX_PRICE", 500_000)
MA_TREND_PERIOD = _i("MA_TREND_PERIOD", 60)                  # 60일선 위
VOLUME_SURGE_RATIO = _f("VOLUME_SURGE_RATIO", 1.5)           # 최근 5일 / 20일 평균 >= 1.5
# 종목별 일봉(ka10081) 순차 조회 간 지연. REST_RATE_PER_SEC 보다 훨씬 낮게 잡는다 —
# 이 값을 REST_RATE_PER_SEC 자체를 낮춰서 대신하면 실거래 주문·취소 응답성까지
# 함께 느려지므로, 스크리닝 루프에만 별도로 완만하게 페이싱한다.
SCREEN_REQUEST_DELAY_SEC = _f("SCREEN_REQUEST_DELAY_SEC", 0.5)
UNIVERSE_MARKETS = tuple(
    m.strip().upper() for m in os.getenv("UNIVERSE_MARKETS", "KOSPI,KOSDAQ").split(",") if m.strip()
)
# 스크리닝을 건너뛰고 이 종목들만 감시하고 싶을 때 (쉼표구분 6자리 코드)
FIXED_UNIVERSE = tuple(c.strip() for c in os.getenv("FIXED_UNIVERSE", "").split(",") if c.strip())

# ---------------------------------------------------------------- 시그널
WARMUP_BARS = _i("WARMUP_BARS", 120)             # 시작 시 ka10080 으로 채워둘 1분봉 개수
SIGNAL_SCORE_THRESHOLD = _f("SIGNAL_SCORE_THRESHOLD", 4.0)
RSI_PERIOD = _i("RSI_PERIOD", 14)
MA_FAST = _i("MA_FAST", 5)
MA_SLOW = _i("MA_SLOW", 20)
ATR_PERIOD = _i("ATR_PERIOD", 14)
# Factor A: 당일 누적거래대금 / 전일 거래대금 비율 임계치
FACTOR_A_TURNOVER_RATIO = _f("FACTOR_A_TURNOVER_RATIO", 0.30)
# Factor D: 체결강도 임계치(%)
FACTOR_D_STRENGTH = _f("FACTOR_D_STRENGTH", 110.0)

# 팩터별 배점. strategy.SignalEngine.evaluate() 가 이 값을 그대로 가져다 쓴다 —
# validate() 의 "Factor A 없이는 진입 불가능" 검증과 실제 채점 로직이 같은 값을
# 보도록 여기 한 곳에서만 정의한다.
FACTOR_A_WEIGHT = 2.0
FACTOR_B_MA_WEIGHT = 1.0
FACTOR_B_CROSS_WEIGHT = 0.5
FACTOR_C_WEIGHT = 1.0
FACTOR_D_WEIGHT = 1.0
# 같은 종목 재진입 쿨다운(초)
REENTRY_COOLDOWN_SEC = _i("REENTRY_COOLDOWN_SEC", 600)

# ---------------------------------------------------------------- 메타 필터(ML 슬롯)
META_FILTER_ENABLED = _b("META_FILTER_ENABLED", False)
META_MODEL_PATH = os.getenv("META_MODEL_PATH", str(BASE_DIR / "models" / "meta_lgbm.pkl"))
META_PROB_THRESHOLD = _f("META_PROB_THRESHOLD", 0.60)
# 삼중 장벽 라벨링 파라미터 (학습 데이터 생성용)
TB_UPPER_ATR_MULT = _f("TB_UPPER_ATR_MULT", 2.0)
TB_LOWER_ATR_MULT = _f("TB_LOWER_ATR_MULT", 1.0)
TB_VERTICAL_MIN = _i("TB_VERTICAL_MIN", 60)

# ---------------------------------------------------------------- 리스크/자금
POSITION_PCT = _f("POSITION_PCT", 0.10)               # 주문가능금액 대비 1회 진입 비중
MAX_ORDER_AMOUNT = _f("MAX_ORDER_AMOUNT", 3_000_000)  # 1회 최대 주문금액
MIN_ORDER_AMOUNT = _f("MIN_ORDER_AMOUNT", 100_000)    # 이보다 작으면 주문하지 않음
MAX_POSITIONS = _i("MAX_POSITIONS", 5)

STOP_LOSS_PCT = _f("STOP_LOSS_PCT", -0.02)           # -2.0%
TAKE_PROFIT_PCT = _f("TAKE_PROFIT_PCT", 0.03)        # +3.0%
TAKE_PROFIT_RATIO = _f("TAKE_PROFIT_RATIO", 0.5)     # 1차 익절 시 매도 비중
TRAILING_STOP_PCT = _f("TRAILING_STOP_PCT", -0.015)  # 최고점 대비 -1.5%
TIME_CUT_MIN = _i("TIME_CUT_MIN", 60)                # 진입 후 N분 횡보 시 정리
TIME_CUT_BAND_PCT = _f("TIME_CUT_BAND_PCT", 0.01)    # ±1% 이내면 '횡보'로 간주

DAILY_LOSS_LIMIT_PCT = _f("DAILY_LOSS_LIMIT_PCT", -0.03)  # 킬스위치: 당일 -3%

# 포지션 사이징 방식: "fixed_pct" | "atr_risk" | "half_kelly"
SIZING_MODE = os.getenv("SIZING_MODE", "fixed_pct").strip().lower()
RISK_PER_TRADE_PCT = _f("RISK_PER_TRADE_PCT", 0.005)  # atr_risk 모드: 1회 감내 손실 0.5%
ATR_STOP_MULT = _f("ATR_STOP_MULT", 1.5)
KELLY_FRACTION = _f("KELLY_FRACTION", 0.5)            # half kelly
KELLY_WIN_RATE = _f("KELLY_WIN_RATE", 0.55)
KELLY_PAYOFF = _f("KELLY_PAYOFF", 1.5)
KELLY_CAP = _f("KELLY_CAP", 0.20)                     # 켈리 결과 상한

# ---------------------------------------------------------------- 주문 집행
# trde_tp: 0=보통(지정가) 3=시장가 6=최유리지정가 7=최우선지정가
#          10=보통(IOC) 13=시장가(IOC) 16=최유리(IOC) 20=보통(FOK)
ENTRY_ORDER_TYPE = os.getenv("ENTRY_ORDER_TYPE", "7").strip()   # 최우선지정가
EXIT_ORDER_TYPE = os.getenv("EXIT_ORDER_TYPE", "3").strip()     # 시장가 청산
UNFILLED_TIMEOUT_SEC = _i("UNFILLED_TIMEOUT_SEC", 30)
UNFILLED_MAX_CHASE = _i("UNFILLED_MAX_CHASE", 1)     # 취소 후 재시도 횟수
SLIPPAGE_GUARD_PCT = _f("SLIPPAGE_GUARD_PCT", 0.01)  # 시그널가 대비 1% 이상 뛰면 진입 포기

# ---------------------------------------------------------------- 알림/로그
NOTIFIER = os.getenv("NOTIFIER", "telegram").strip().lower()  # telegram | discord | null
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_DIR = Path(os.getenv("LOG_DIR", str(BASE_DIR / "logs")))
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "trading.db")))
TOKEN_CACHE = Path(os.getenv("TOKEN_CACHE", str(DATA_DIR / "token.json")))

for _d in (LOG_DIR, DATA_DIR, Path(META_MODEL_PATH).parent):
    _d.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Summary:
    """기동 시 로그에 찍는 요약."""

    env: str = KIWOOM_ENV
    host: str = REST_HOST
    dry_run: bool = DRY_RUN
    universe_max: int = UNIVERSE_MAX
    max_positions: int = MAX_POSITIONS
    sizing: str = SIZING_MODE
    meta_filter: bool = META_FILTER_ENABLED
    notifier: str = NOTIFIER
    extras: dict = field(default_factory=dict)

    def as_text(self) -> str:
        return (
            f"env={self.env} host={self.host} DRY_RUN={self.dry_run} "
            f"universe<={self.universe_max} max_pos={self.max_positions} "
            f"sizing={self.sizing} meta_filter={self.meta_filter} notifier={self.notifier}"
        )


def validate() -> list[str]:
    """치명적 설정 오류를 리스트로 반환한다(빈 리스트면 정상)."""
    errors: list[str] = []
    if not APP_KEY or not APP_SECRET:
        errors.append(
            f"{KIWOOM_ENV} 환경의 앱키가 비어 있습니다. .env 에 "
            f"KIWOOM_APP_KEY_{_suffix} / KIWOOM_APP_SECRET_{_suffix} "
            f"(또는 공통 KIWOOM_APP_KEY / KIWOOM_APP_SECRET) 를 넣으세요. "
            f"실전과 모의투자는 앱키가 서로 다릅니다."
        )
    if NOTIFIER == "telegram" and not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        errors.append("NOTIFIER=telegram 인데 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 없습니다")
    if NOTIFIER == "discord" and not DISCORD_WEBHOOK_URL:
        errors.append("NOTIFIER=discord 인데 DISCORD_WEBHOOK_URL 이 없습니다")
    if not 0 < POSITION_PCT <= 1:
        errors.append(f"POSITION_PCT 는 0~1 이어야 합니다: {POSITION_PCT}")
    if STOP_LOSS_PCT >= 0:
        errors.append(f"STOP_LOSS_PCT 는 음수여야 합니다: {STOP_LOSS_PCT}")
    if TAKE_PROFIT_PCT <= 0:
        errors.append(f"TAKE_PROFIT_PCT 는 양수여야 합니다: {TAKE_PROFIT_PCT}")
    if DAILY_LOSS_LIMIT_PCT >= 0:
        errors.append(f"DAILY_LOSS_LIMIT_PCT 는 음수여야 합니다: {DAILY_LOSS_LIMIT_PCT}")
    if SIZING_MODE not in ("fixed_pct", "atr_risk", "half_kelly"):
        errors.append(f"알 수 없는 SIZING_MODE: {SIZING_MODE}")
    return errors


# Factor A(거래대금 유입) 없이 B+C+D 만으로 도달 가능한 최대점수.
# SIGNAL_SCORE_THRESHOLD(기본 4.0)가 이보다 높다는 것은 설계상 Factor A가 사실상
# 필수 조건이라는 뜻이다 — 그 자체는 의도된 전략이지만, 그래서 Factor A 판정 경로
# (prev_turnover 기준선, 단위 환산 등)에 결함이 생기면 다른 팩터를 다 만족해도
# 하루 종일 로그 한 줄 없이 조용히 매매가 중단된다(실제로 있었던 장애).
# main.TradingBot 이 장중에 이 값을 근거로 "Factor A 무응답" 워치독을 돌린다.
FACTOR_MAX_SCORE_WITHOUT_A = FACTOR_B_MA_WEIGHT + FACTOR_B_CROSS_WEIGHT + FACTOR_C_WEIGHT + FACTOR_D_WEIGHT
