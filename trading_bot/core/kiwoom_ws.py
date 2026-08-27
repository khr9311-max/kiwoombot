"""
키움 실시간 시세 WebSocket 클라이언트.

  wss://api.kiwoom.com:10000/api/dostk/websocket   (모의: mockapi)

프로토콜 (키움 REST API 문서 '실시간시세' 장 + 공식 파이썬 샘플):
  1) 접속 후  {"trnm": "LOGIN", "token": "<access_token>"}   전송
  2) 서버가   {"trnm": "LOGIN", "return_code": 0}            응답
  3) 등록     {"trnm": "REG", "grp_no": "1", "refresh": "1",
               "data": [{"item": ["005930"], "type": ["0B"]}]}
     refresh 0 = 기존 등록 유지하며 추가, 1 = 기존 등록 해제 후 등록(Default)
  4) 수신     {"trnm": "REAL", "data": [{"type": "0B", "item": "005930",
                                         "values": {"10": "-20800", ...}}]}
  5) 서버가 {"trnm": "PING"} 을 보내면 받은 그대로 되돌려 보내야 연결이 유지된다.

실시간 TR:
  0B = 주식체결   (종목별 체결 틱)
  00 = 주문체결   (내 주문의 접수/체결/취소 통보 — item 없이 type 만 등록)

0B 주요 필드번호:
  20 체결시간(HHMMSS)  10 현재가   11 전일대비  12 등락율   13 누적거래량
  14 누적거래대금      15 체결량   16 시가      17 고가     18 저가
  27 최우선매도호가    28 최우선매수호가        228 체결강도

00 주요 필드번호:
  9201 계좌번호  9203 주문번호  9205 관리자사번  9001 종목코드  913 주문상태
  302 종목명    900 주문수량   901 주문가격    902 미체결수량  903 체결누계금액
  904 원주문번호 905 주문구분   906 매매구분    907 매도수구분  908 주문/체결시간
  909 체결번호   910 체결가     911 체결량      10 현재가       914 단위체결가
  915 단위체결량 938 당일매매수수료 939 당일매매세금
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from ..config import settings as cfg

log = logging.getLogger(__name__)

TickHandler = Callable[[str, dict[str, str]], Awaitable[None] | None]
FillHandler = Callable[[dict[str, str]], Awaitable[None] | None]


class KiwoomWebSocket:
    """자동 재접속 + 등록 복원을 하는 실시간 시세 클라이언트."""

    def __init__(
        self,
        token_provider: Callable[[], str],
        url: str = cfg.WS_HOST,
        on_tick: TickHandler | None = None,
        on_fill: FillHandler | None = None,
    ):
        self._token_provider = token_provider
        self._url = url
        self._on_tick = on_tick
        self._on_fill = on_fill

        self._ws: websockets.WebSocketClientProtocol | None = None
        self._connected = asyncio.Event()
        self._stop = asyncio.Event()
        self._codes: set[str] = set()
        self._order_registered = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ 수명주기
    async def run(self) -> None:
        """연결이 끊기면 지수 백오프로 재접속하며 계속 돈다."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self._url, ping_interval=None, max_size=2**22) as ws:
                    self._ws = ws
                    await self._login()
                    backoff = 1.0
                    await self._restore_subscriptions()
                    await self._recv_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 네트워크/프로토콜 오류 전부 재접속 대상
                log.warning("WebSocket 끊김(%s) -> %.1fs 후 재접속", exc, backoff)
            finally:
                self._ws = None
                self._connected.clear()
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ------------------------------------------------------------ 내부
    async def _login(self) -> None:
        await self._send({"trnm": "LOGIN", "token": self._token_provider()})
        raw = await asyncio.wait_for(self._ws.recv(), timeout=15)
        body = json.loads(raw)
        if body.get("trnm") != "LOGIN" or body.get("return_code") not in (0, None):
            raise RuntimeError(f"WS LOGIN 실패: {body}")
        self._connected.set()
        log.info("WebSocket LOGIN 성공 (%s)", self._url)

    async def _send(self, payload: dict) -> None:
        if self._ws is None:
            raise RuntimeError("WebSocket 미연결")
        await self._ws.send(json.dumps(payload))

    async def _restore_subscriptions(self) -> None:
        if self._order_registered:
            await self._register_orders(force=True)
        if self._codes:
            await self._register_codes(sorted(self._codes), refresh="1")

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        async for raw in self._ws:
            try:
                body = json.loads(raw)
            except ValueError:
                log.debug("WS 파싱 불가 메시지: %.200s", raw)
                continue

            trnm = body.get("trnm")
            if trnm == "PING":
                # 받은 그대로 되돌려 보내야 서버가 연결을 유지한다.
                await self._send(body)
                continue
            if trnm == "REAL":
                await self._dispatch_real(body.get("data") or [])
                continue
            if trnm in ("REG", "REMOVE"):
                if body.get("return_code") not in (0, None):
                    log.error("WS %s 실패: %s", trnm, body.get("return_msg"))
                continue
            log.debug("WS 기타 메시지: %s", body)

    async def _dispatch_real(self, items: list[dict[str, Any]]) -> None:
        for item in items:
            typ = item.get("type")
            values = item.get("values") or {}
            try:
                if typ == "0B" and self._on_tick:
                    res = self._on_tick(str(item.get("item", "")).strip(), values)
                    if asyncio.iscoroutine(res):
                        await res
                elif typ == "00" and self._on_fill:
                    res = self._on_fill(values)
                    if asyncio.iscoroutine(res):
                        await res
            except Exception:
                log.exception("실시간 콜백 처리 중 예외 (type=%s)", typ)

    # ------------------------------------------------------------ 구독 관리
    async def _register_codes(self, codes: list[str], refresh: str) -> None:
        if not codes:
            return
        await self._send(
            {
                "trnm": "REG",
                "grp_no": "1",
                "refresh": refresh,
                "data": [{"item": codes, "type": ["0B"]}],
            }
        )
        log.info("실시간 체결(0B) 등록 %d종목 (refresh=%s)", len(codes), refresh)

    async def _register_orders(self, force: bool = False) -> None:
        if self._order_registered and not force:
            return
        await self._send(
            {
                "trnm": "REG",
                "grp_no": "2",
                "refresh": "0",
                "data": [{"item": [""], "type": ["00"]}],
            }
        )
        self._order_registered = True
        log.info("실시간 주문체결(00) 등록")

    async def subscribe(self, codes: list[str]) -> None:
        """감시 종목 전체를 이 목록으로 교체한다."""
        async with self._lock:
            self._codes = {c for c in codes if c}
            if self._ws is not None:
                await self._register_codes(sorted(self._codes), refresh="1")

    async def add(self, code: str) -> None:
        async with self._lock:
            if code in self._codes:
                return
            self._codes.add(code)
            if self._ws is not None:
                await self._register_codes([code], refresh="0")

    async def remove(self, code: str) -> None:
        async with self._lock:
            if code not in self._codes:
                return
            self._codes.discard(code)
            if self._ws is not None:
                await self._send(
                    {
                        "trnm": "REMOVE",
                        "grp_no": "1",
                        "refresh": "0",
                        "data": [{"item": [code], "type": ["0B"]}],
                    }
                )

    async def subscribe_orders(self) -> None:
        async with self._lock:
            self._order_registered = False
            if self._ws is not None:
                await self._register_orders()
            else:
                self._order_registered = True  # 접속 시 복원되도록 표시만
