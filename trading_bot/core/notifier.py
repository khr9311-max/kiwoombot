"""
알림 모듈. 텔레그램(기본) / 디스코드 웹훅 / 무음 을 같은 인터페이스로 제공한다.

알림 실패가 매매를 막으면 안 되므로 모든 전송 오류는 로그로만 삼키고,
연속 실패 시 잠시 침묵(back-off)한다. 동일 메시지 폭주를 막는 스로틀도 있다.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

import requests

from ..config import settings as cfg

log = logging.getLogger(__name__)


class BaseNotifier:
    def send(self, text: str, *, level: str = "INFO") -> bool:
        raise NotImplementedError

    # 편의 메서드
    def info(self, text: str) -> None:
        self.send(text, level="INFO")

    def warn(self, text: str) -> None:
        self.send(f"⚠️ {text}", level="WARN")

    def error(self, text: str) -> None:
        self.send(f"🚨 {text}", level="ERROR")

    def trade(self, text: str) -> None:
        self.send(text, level="TRADE")


class NullNotifier(BaseNotifier):
    def send(self, text: str, *, level: str = "INFO") -> bool:
        log.info("[알림:%s] %s", level, text)
        return True


class _HttpNotifier(BaseNotifier):
    """전송 실패 백오프 + 중복 스로틀 공통 로직."""

    def __init__(self, throttle_sec: int = 5):
        self._session = requests.Session()
        self._lock = threading.Lock()
        self._fail_streak = 0
        self._mute_until = 0.0
        self._recent: dict[str, float] = {}
        self._throttle = throttle_sec

    def _post(self, text: str) -> bool:  # pragma: no cover - 하위 클래스에서 구현
        raise NotImplementedError

    def send(self, text: str, *, level: str = "INFO") -> bool:
        log.info("[알림:%s] %s", level, text.replace("\n", " | ")[:300])
        now = time.monotonic()
        with self._lock:
            if now < self._mute_until:
                return False
            last = self._recent.get(text)
            if last is not None and now - last < self._throttle:
                return False
            self._recent[text] = now
            if len(self._recent) > 200:
                self._recent = {k: v for k, v in self._recent.items() if now - v < 60}

        try:
            ok = self._post(text)
        except requests.RequestException as exc:
            log.warning("알림 전송 실패: %s", exc)
            ok = False

        with self._lock:
            if ok:
                self._fail_streak = 0
            else:
                self._fail_streak += 1
                if self._fail_streak >= 3:
                    self._mute_until = time.monotonic() + 60
                    log.warning("알림 연속 실패 %d회 -> 60초 침묵", self._fail_streak)
        return ok


class TelegramNotifier(_HttpNotifier):
    def __init__(self, token: str = cfg.TELEGRAM_BOT_TOKEN, chat_id: str = cfg.TELEGRAM_CHAT_ID):
        super().__init__()
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id

    def _post(self, text: str) -> bool:
        resp = self._session.post(
            self._url,
            json={"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning("텔레그램 %d: %.200s", resp.status_code, resp.text)
            return False
        return True


class DiscordNotifier(_HttpNotifier):
    def __init__(self, webhook_url: str = cfg.DISCORD_WEBHOOK_URL):
        super().__init__()
        self._url = webhook_url

    def _post(self, text: str) -> bool:
        resp = self._session.post(self._url, json={"content": text[:1900]}, timeout=10)
        if resp.status_code not in (200, 204):
            log.warning("디스코드 %d: %.200s", resp.status_code, resp.text)
            return False
        return True


def build_notifier() -> BaseNotifier:
    kind = cfg.NOTIFIER
    if kind == "telegram" and cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID:
        return TelegramNotifier()
    if kind == "discord" and cfg.DISCORD_WEBHOOK_URL:
        return DiscordNotifier()
    if kind not in ("null", "none", ""):
        log.warning("NOTIFIER=%s 설정이 불완전하여 로그 전용으로 동작합니다", kind)
    return NullNotifier()


def fmt_fill(side: str, code: str, name: str, qty: int, price: float, reason: str = "") -> str:
    icon = "🟢" if side == "BUY" else "🔴"
    label = "매수" if side == "BUY" else "매도"
    body = f"{icon} [{label} 체결] {name}({code}) {qty:,}주 @ {price:,.0f}원 = {qty * price:,.0f}원"
    return f"{body}\n  └ {reason}" if reason else body


def fmt_daily_report(equity: float, pnl_pct: float, trades: int, wins: int,
                     realized: float, positions: int) -> str:
    win_rate = (wins / trades * 100) if trades else 0.0
    return (
        f"📊 일일 리포트 {datetime.now():%Y-%m-%d}\n"
        f"  평가자산 {equity:,.0f}원 ({pnl_pct:+.2%})\n"
        f"  실현손익 {realized:+,.0f}원\n"
        f"  매매 {trades}회 / 승 {wins}회 (승률 {win_rate:.1f}%)\n"
        f"  잔여 보유 {positions}종목"
    )
