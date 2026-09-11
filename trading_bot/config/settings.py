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

# 확장진입 차단(2026-09-11 실거래 487건 분석). 점수(A~D)는 전부 "얼마나 이미 올라왔는가"를
# 점수화하는 모멘텀 팩터인데, 정작 실현손익과 median-split 해보면 RSI/당일고점근접도
# (day_range_pos)/거래대금비율이 median 이상인 쪽이 median 미만인 쪽보다 오히려 총수익률이
# 낮았다(각각 -0.24%/-0.14%/-0.05% vs +0.40%/+0.31%/+0.21%) — 초단기(중앙값 23분) 홀딩에서는
# 이미 튄 종목을 사면 상투를 잡는 평균회귀 구간이라는 뜻. 세 조건을 모두 만족(=아직 안 튄
# 상태)한 71건만 보면 비용 차감 후 거의 본전(-6,535원)까지 좁혀진다 — 나머지 416건이 손실의
# 대부분을 만들었다. 점수 임계치를 올리는 것(SIGNAL_SCORE_THRESHOLD)은 이 분석과 반대
# 방향이라 채택하지 않았다: score>=5 구간이 score=4 구간보다 오히려 총수익률이 더 나빴다.
ENTRY_EXTENSION_GUARD = _b("ENTRY_EXTENSION_GUARD", True)
MAX_ENTRY_DAY_RANGE_POS = _f("MAX_ENTRY_DAY_RANGE_POS", 0.60)   # 당일 저가~고가 구간에서 위치
MAX_ENTRY_RSI = _f("MAX_ENTRY_RSI", 60.0)
MAX_ENTRY_TURNOVER_RATIO = _f("MAX_ENTRY_TURNOVER_RATIO", 0.75)  # 전일 대비 당일 누적거래대금 비율

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
# 회전율 캡: 쿨다운은 속도만 늦추고 총량을 안 막아서 하루 같은 종목에 10회 넘게
# 재진입 -> 손절 -> 재진입이 반복됐다(2026-09-08 JW신약 12회 전패 -70,634원 등).
# 종목당/일일 총 진입 횟수를 못박아 손절이 반복되는 종목을 하루 안에서 포기시킨다.
MAX_DAILY_ENTRIES_PER_SYMBOL = _i("MAX_DAILY_ENTRIES_PER_SYMBOL", 2)
MAX_DAILY_ENTRIES_TOTAL = _i("MAX_DAILY_ENTRIES_TOTAL", 20)

# ---------------------------------------------------------------- 메타 필터(ML 슬롯)
META_FILTER_ENABLED = _b("META_FILTER_ENABLED", False)
META_MODEL_PATH = os.getenv("META_MODEL_PATH", str(BASE_DIR / "models" / "meta_lgbm.pkl"))
META_PROB_THRESHOLD = _f("META_PROB_THRESHOLD", 0.60)
# 삼중 장벽 라벨링 파라미터 (학습 데이터 생성용)
TB_UPPER_ATR_MULT = _f("TB_UPPER_ATR_MULT", 2.0)
TB_LOWER_ATR_MULT = _f("TB_LOWER_ATR_MULT", 1.0)
TB_VERTICAL_MIN = _i("TB_VERTICAL_MIN", 60)

# 학습 표본 선별.
#   TRAIN_EXCLUDE_DATES: 장애로 시그널이 중복 폭주한 날은 통째로 뺀다.
#     2026-09-02 는 RC4026 거부 폭주로 같은 시그널이 봉마다 재발행돼
#     917건 중 3종목이 40%를 차지한다(동국제약 133 / 가온전선 122 / 컴투스 117).
#   TRAIN_DEDUPE_MIN: 같은 종목에서 이 분(分) 안에 다시 뜬 시그널은 한 건으로 본다.
#     삼중 장벽의 보유 구간이 겹치면 사실상 같은 표본이라 독립 가정이 깨진다.
TRAIN_EXCLUDE_DATES = tuple(
    d.strip() for d in os.getenv("TRAIN_EXCLUDE_DATES", "2026-09-02").split(",") if d.strip()
)
TRAIN_DEDUPE_MIN = _i("TRAIN_DEDUPE_MIN", TB_VERTICAL_MIN)
# 교차검증 AUC 가 이보다 낮은 모델은 저장하지 않는다(야간 재학습이 좋은 모델을 덮어쓰지 않도록).
META_MIN_CV_AUC = _f("META_MIN_CV_AUC", 0.55)

# ---------------------------------------------------------------- 리스크/자금
POSITION_PCT = _f("POSITION_PCT", 0.10)               # 주문가능금액 대비 1회 진입 비중
MAX_ORDER_AMOUNT = _f("MAX_ORDER_AMOUNT", 3_000_000)  # 1회 최대 주문금액
MIN_ORDER_AMOUNT = _f("MIN_ORDER_AMOUNT", 100_000)    # 이보다 작으면 주문하지 않음
MAX_POSITIONS = _i("MAX_POSITIONS", 5)

# 2026-09-04~11 실거래 517건 분석(승률 42.2% / 페이오프 0.83)에서 손절·익절의 손익
# 비대칭이 기대값을 구조적으로 마이너스로 만들었다(손절은 전량 -2%, 1차익절은 절반만
# +3%). 트레일링스탑(청산 후 잔량)의 평균 실현수익률(+4.02%)이 1차익절(+3.12%)보다
# 높았으므로, 손절은 -1.5%로 좁히고 1차익절 비중은 줄여 더 많은 물량이 트레일링까지
# 가도록 조정한다. 승률 42%·손절 -1.5%·트레일링 위주 구성 기준 손익비 손익분기는
# 0.42/(1-0.42)≈0.72 — 기존 -2%/50% 구성의 손익분기 1.37보다 훨씬 낮다.
STOP_LOSS_PCT = _f("STOP_LOSS_PCT", -0.015)          # -1.5% (기존 -2.0%)
TAKE_PROFIT_PCT = _f("TAKE_PROFIT_PCT", 0.03)        # +3.0%
TAKE_PROFIT_RATIO = _f("TAKE_PROFIT_RATIO", 0.35)    # 1차 익절 시 매도 비중 (기존 0.5)
TRAILING_STOP_PCT = _f("TRAILING_STOP_PCT", -0.015)  # 최고점 대비 -1.5%
# 타임컷 150건을 실현손익 기준으로만 보면 비용 차감 전 평균 수익률이 +0.02%(사실상 0)였고
# 비용까지 반영하면 -394,677원으로 순손실 2위 사유였다(2026-09-11 분석) — 그래서 처음엔
# 비활성(0)으로 바꿨었다. 하지만 이 숫자는 "타임컷이 실제로 만든 결과"일 뿐 "타임컷이
# 없었다면 그 포지션들이 어떻게 됐을지"는 말해주지 않는다. bars 테이블의 9일치 실데이터로
# 리플레이해 반사실을 직접 비교해보니(tools/backtest.py --source db), TIME_CUT_MIN=0 은
# 오히려 결과를 크게 악화시켰다(같은 9일 재생 기준 -30.44% -> -40.81%, 손절 44->52건 +
# 새로 발생한 장마감 강제청산 손실). 횡보하다 타임컷으로 빠지던 포지션들이 타임컷이
# 없으면 손절이나 15:15 강제청산으로 더 크게 깨진다는 뜻 — 그래서 되돌렸다.
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
# trde_tp: 0=보통(지정가) 3=시장가 5=조건부지정가 6=최유리지정가 7=최우선지정가
#          10=보통(IOC) 13=시장가(IOC) 16=최유리(IOC) 20=보통(FOK) 23=시장가(FOK)
#          26=최유리(FOK) 28=스톱지정가 29=중간가 30=중간가(IOC) 31=중간가(FOK)

# ord_uv(주문가)를 반드시 실어 보내야 하는 지정가 계열.
# 시장가/최유리/최우선은 가격을 빈 문자열로 둔다.
LIMIT_ORDER_TYPES = ("0", "5", "10", "20", "28")

# 모의투자에서 거부되는 매매구분 -> 대체 구분.
#   RC4026: "모의투자 최유리지정가와 최우선지정가 주문은 불가합니다."
# 중간가(29/30/31)도 모의투자에서는 지원되지 않아 같이 묶어 둔다.
MOCK_UNSUPPORTED_ORDER_TYPES = {
    "6": "0",    # 최유리지정가 -> 보통(지정가)
    "7": "0",    # 최우선지정가 -> 보통(지정가)
    "16": "10",  # 최유리(IOC)  -> 보통(IOC)
    "26": "20",  # 최유리(FOK)  -> 보통(FOK)
    "29": "0",   # 중간가       -> 보통(지정가)
    "30": "10",  # 중간가(IOC)  -> 보통(IOC)
    "31": "20",  # 중간가(FOK)  -> 보통(FOK)
}

# 기동 시 대체된 내역. main 이 로그/알림으로 한 번 찍는다.
ORDER_TYPE_NOTES: list[str] = []


def _order_type(key: str, default: str) -> str:
    """환경에 맞지 않는 매매구분은 기동 시점에 대체한다 (모의투자 RC4026 예방)."""
    tp = os.getenv(key, default).strip()
    if IS_MOCK and tp in MOCK_UNSUPPORTED_ORDER_TYPES:
        alt = MOCK_UNSUPPORTED_ORDER_TYPES[tp]
        ORDER_TYPE_NOTES.append(
            f"{key}={tp} 는 모의투자에서 거부(RC4026)되므로 {alt} 로 대체합니다"
        )
        return alt
    return tp


# 모의투자 기본 진입은 지정가(0). 실계좌에서는 ENTRY_ORDER_TYPE=7 로 두면 최우선지정가.
ENTRY_ORDER_TYPE = _order_type("ENTRY_ORDER_TYPE", "7")
EXIT_ORDER_TYPE = _order_type("EXIT_ORDER_TYPE", "3")     # 시장가 청산
UNFILLED_TIMEOUT_SEC = _i("UNFILLED_TIMEOUT_SEC", 30)
UNFILLED_MAX_CHASE = _i("UNFILLED_MAX_CHASE", 1)     # 취소 후 재시도 횟수
SLIPPAGE_GUARD_PCT = _f("SLIPPAGE_GUARD_PCT", 0.01)  # 시그널가 대비 1% 이상 뛰면 진입 포기

# 주문이 거부된 종목은 일정 시간 다시 건드리지 않는다(같은 종목 반복 거부 방지).
ORDER_REJECT_COOLDOWN_SEC = _i("ORDER_REJECT_COOLDOWN_SEC", 300)
# 매수 거부가 연속으로 이만큼 나면 구조적 문제로 보고 신규 진입을 멈춘다.
MAX_CONSECUTIVE_REJECTS = _i("MAX_CONSECUTIVE_REJECTS", 5)
# 매도 거부는 포기할 수 없으니 재시도하되, 초 단위 재전송으로 주문가능수량을
# 잠가버리지 않도록 이만큼 쉬었다가 다시 던진다.
EXIT_REJECT_COOLDOWN_SEC = _i("EXIT_REJECT_COOLDOWN_SEC", 60)

# ---------------------------------------------------------------- 백테스트 비용 모델
# 모의투자(mock)는 실계좌보다 훨씬 높은 수수료를 매긴다(실측: 매수/매도 각 0.35%,
# 매도세 0.135% — 2026-09-08 fills 테이블 집계). 백테스트가 실계좌 요율로만 비용을
# 계산하면 실행 환경(mock)의 실제 비용을 과소평가해 전략이 실제보다 좋아 보인다.
# KIWOOM_ENV 에 맞춰 자동으로 골라 쓴다 — real 요율은 국내 통상 수준의 근사치다.
FEE_RATE_MOCK = _f("FEE_RATE_MOCK", 0.0035)
TAX_RATE_MOCK = _f("TAX_RATE_MOCK", 0.00135)
FEE_RATE_REAL = _f("FEE_RATE_REAL", 0.00015)
TAX_RATE_REAL = _f("TAX_RATE_REAL", 0.0018)
FEE_RATE = FEE_RATE_MOCK if IS_MOCK else FEE_RATE_REAL
TAX_RATE = TAX_RATE_MOCK if IS_MOCK else TAX_RATE_REAL

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
