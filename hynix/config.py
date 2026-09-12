"""000660 전용 설정.

trading_bot.config.settings 의 접속/인증/레이트리밋은 그대로 물려 쓰고,
여기서는 '무엇을 수집할 것인가' 만 정의한다.
"""
from __future__ import annotations

import os
from pathlib import Path

from trading_bot.config import settings as cfg

# ---------------------------------------------------------------- 대상 종목
TARGET = "000660"           # SK하이닉스 — 매매 대상
TARGET_NAME = "SK하이닉스"

# 동조/대조군. 매매하지 않고 피처로만 쓴다.
PEERS: tuple[str, ...] = (
    "005930",               # 삼성전자 — 국내 메모리 대조군
)
KR_CODES: tuple[str, ...] = (TARGET,) + PEERS

# ---------------------------------------------------------------- 외부 티커
# yfinance 심볼. SKHY 는 2026-07-10 상장이라 이력이 짧다(백테스트 불가, 실시간 감시용).
EXT_TICKERS: dict[str, str] = {
    "SKHY": "SK하이닉스 ADR (NASDAQ)",
    "^SOX": "필라델피아 반도체지수",
    "MU": "마이크론",
    "NVDA": "엔비디아",
    "KRW=X": "USD/KRW",
    "NQ=F": "나스닥100 선물",
    "ES=F": "S&P500 선물",
    "000660.KS": "SK하이닉스 원주(교차검증용)",
}

# ---------------------------------------------------------------- 수집 파라미터
# ka10060(투자자별)은 REST_RATE_PER_SEC 를 지켜도 429 가 나서 별도로 더 느리게 페이싱한다.
FLOW_REQUEST_DELAY_SEC = float(os.getenv("HYNIX_FLOW_DELAY_SEC", "1.2"))
# 연속조회 최대 페이지. ka10060 은 1페이지 100행이고 600행(약 2.5년)까지 확인됐다.
MAX_PAGES = int(os.getenv("HYNIX_MAX_PAGES", "12"))

DB_PATH = Path(os.getenv("HYNIX_DB_PATH", str(cfg.DATA_DIR / "hynix.db")))

# ---------------------------------------------------------------- 단위 주의
# ka10060 은 amt_qty_tp 로 단위가 바뀐다(2026-09-12 실측 확인):
#   amt_qty_tp="1" -> 순매수 '금액', 단위 백만원
#   amt_qty_tp="2" -> 순매수 '수량', 단위 주
# 그리고 acc_trde_prica 필드는 이름과 달리 두 모드 모두 '거래량(주)' 이 온다.
# 거래대금이 필요하면 ka10081(일봉)의 trde_prica 를 쓸 것 — 여기서 섞으면
# Factor A 때와 같은 '조용한 단위 버그' 가 그대로 재현된다.
AMT_UNIT_KRW = 1_000_000
