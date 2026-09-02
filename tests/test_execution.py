"""
주문 집행 · 실시간 프로토콜 테스트. 네트워크를 타지 않도록 클라이언트를 가짜로 바꾼다.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("KIWOOM_APP_KEY", "TESTKEY")
os.environ.setdefault("KIWOOM_APP_SECRET", "TESTSECRET")
os.environ.setdefault("KIWOOM_ENV", "mock")
os.environ.setdefault("NOTIFIER", "null")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from trading_bot.config import settings as cfg  # noqa: E402
from trading_bot.core.executor import OrderExecutor  # noqa: E402
from trading_bot.core.kiwoom_client import KiwoomAPIError  # noqa: E402
from trading_bot.core.kiwoom_ws import KiwoomWebSocket  # noqa: E402
from trading_bot.core.notifier import NullNotifier  # noqa: E402
from trading_bot.core.risk_manager import ExitOrder, RiskManager  # noqa: E402
from trading_bot.database.db import Database  # noqa: E402


# ------------------------------------------------------------------ 더미들
class FakeClient:
    """키움 REST 클라이언트 대역. 보낸 주문을 기록만 한다."""

    def __init__(self, dry_run: bool = False, fail_on: set[str] | None = None):
        self.dry_run = dry_run
        self.sent: list[tuple[str, dict]] = []
        self.fail_on = fail_on or set()
        self._seq = 0
        self.unfilled: list[dict] = []

    def _next(self) -> str:
        self._seq += 1
        return f"ORD{self._seq:05d}"

    def buy(self, code, qty, price="", trade_type="7", cond_price=""):
        if "buy" in self.fail_on:
            raise KiwoomAPIError("kt10000", 3, "주문가능금액 부족")
        self.sent.append(("BUY", {"code": code, "qty": qty, "price": price, "tt": trade_type}))
        return {"ord_no": self._next(), "return_code": 0}

    def sell(self, code, qty, price="", trade_type="3", cond_price=""):
        if "sell" in self.fail_on:
            raise KiwoomAPIError("kt10001", 3, "주문 거부")
        self.sent.append(("SELL", {"code": code, "qty": qty, "price": price, "tt": trade_type}))
        return {"ord_no": self._next(), "return_code": 0}

    def cancel(self, orig_order_no, code, qty=0):
        if "cancel" in self.fail_on:
            raise KiwoomAPIError("kt10003", 3, "이미 체결된 주문")
        self.sent.append(("CANCEL", {"ord_no": orig_order_no, "code": code, "qty": qty}))
        return {"ord_no": self._next(), "return_code": 0}

    def get_unfilled(self, *a, **k):
        return self.unfilled

    def get_quote(self, code):
        return {"price": 10_000}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "t.db")
    # 진입 차단 시각(기본 14:30)에 걸려 테스트가 실행 시간에 좌우되지 않도록 고정한다.
    monkeypatch.setattr(cfg, "NO_NEW_ENTRY_AFTER", datetime(2026, 1, 1, 23, 59).time())
    db = Database(tmp_path / "t.db")
    risk = RiskManager()
    risk.cash = risk.orderable_cash = risk.total_equity = 10_000_000
    risk.reset_day(10_000_000)
    client = FakeClient()
    ex = OrderExecutor(client, risk, db, NullNotifier())
    return client, risk, db, ex


def fill_msg(order_no, code, side, qty, price, remain=0, name="테스트"):
    """실시간 주문체결(00) 메시지 values 블록."""
    return {
        "9203": order_no, "9001": f"A{code}", "302": name,
        "913": "체결", "900": str(qty), "901": str(int(price)),
        "902": str(remain), "907": "1" if side == "SELL" else "2",
        "910": f"+{int(price)}", "911": str(qty), "938": "150", "939": "0",
    }


# ------------------------------------------------------------------ 진입
class TestBuyFlow:
    def test_order_then_realtime_fill_opens_position(self, rig):
        client, risk, db, ex = rig
        po = ex.submit_buy("005930", "삼성전자", 10, 70_000, 70_000)
        assert po is not None
        assert client.sent[0][0] == "BUY"
        # 체결 대기 중이라는 '그 이유로' 막혀야 한다 (시각 제한 등 다른 이유가 아니라)
        ok, why = risk.can_buy("005930", 70_000)
        assert ok is False and "체결 대기" in why

        ex.on_realtime_fill(fill_msg(po.order_no, "005930", "BUY", 10, 70_000))
        pos = risk.positions["005930"]
        assert pos.qty == 10 and pos.avg_price == 70_000
        assert ex.pending == {}
        assert ex.stats.buy_fills == 1

    def test_partial_fills_accumulate(self, rig):
        client, risk, db, ex = rig
        po = ex.submit_buy("005930", "삼성전자", 10, 70_000, 70_000)
        ex.on_realtime_fill(fill_msg(po.order_no, "005930", "BUY", 4, 70_000, remain=6))
        assert risk.positions["005930"].qty == 4
        assert po.order_no in ex.pending          # 잔량이 남아 계속 추적

        ex.on_realtime_fill(fill_msg(po.order_no, "005930", "BUY", 6, 70_100, remain=0))
        pos = risk.positions["005930"]
        assert pos.qty == 10
        assert pos.avg_price == pytest.approx(70_060)   # 가중평균
        assert ex.pending == {}

    def test_slippage_guard_blocks_chase(self, rig, monkeypatch):
        monkeypatch.setattr(cfg, "SLIPPAGE_GUARD_PCT", 0.01)
        client, risk, db, ex = rig
        # 시그널가 70,000 인데 현재가가 71,500 (+2.14%) -> 포기
        assert ex.submit_buy("005930", "삼성전자", 10, 70_000, 71_500) is None
        assert client.sent == []
        assert ex.stats.slippage_blocks == 1
        # 0.5% 상승은 허용
        assert ex.submit_buy("005930", "삼성전자", 10, 70_000, 70_350) is not None

    def test_rejected_order_clears_pending_state(self, rig):
        client, risk, db, ex = rig
        client.fail_on = {"buy"}
        risk.mark_pending_buy("005930", 10)
        assert ex.submit_buy("005930", "삼성전자", 10, 70_000, 70_000) is None
        assert ex.stats.rejects == 1
        # 거부되면 대기 상태를 풀어야 다음 기회를 잡을 수 있다
        assert risk.can_buy("005930", 70_000)[0] is True


# ------------------------------------------------------------------ 청산
class TestSellFlow:
    def test_exit_records_completed_trade(self, rig):
        client, risk, db, ex = rig
        risk.open_position("005930", 10, 70_000, name="삼성전자")
        po = ex.submit_exit(ExitOrder("005930", 10, "손절 -2.00%"), 68_600)
        assert po is not None and client.sent[-1][0] == "SELL"
        assert risk.positions["005930"].pending_exit == "손절 -2.00%"

        ex.on_realtime_fill(fill_msg(po.order_no, "005930", "SELL", 10, 68_600))
        assert "005930" not in risk.positions

        rows = db.today_stats()
        assert rows["trades"] == 1
        assert rows["realized"] == pytest.approx((68_600 - 70_000) * 10)

    def test_partial_take_profit_marks_flag_and_keeps_rest(self, rig):
        client, risk, db, ex = rig
        risk.open_position("005930", 10, 70_000, name="삼성전자")
        po = ex.submit_exit(ExitOrder("005930", 5, "1차익절 +3.00%"), 72_100)
        ex.on_realtime_fill(fill_msg(po.order_no, "005930", "SELL", 5, 72_100))

        pos = risk.positions["005930"]
        assert pos.qty == 5
        assert pos.took_profit is True      # 이제 트레일링 스탑이 활성화된다
        assert pos.pending_exit == ""

    def test_exit_not_duplicated_while_pending(self, rig):
        client, risk, db, ex = rig
        risk.open_position("005930", 10, 70_000, name="삼성전자")
        ex.submit_exit(ExitOrder("005930", 10, "손절"), 68_000)
        sells = [s for s in client.sent if s[0] == "SELL"]
        # pending_exit 이 걸려 있으면 RiskManager 가 재판정을 하지 않는다
        assert risk.check_exit(risk.positions["005930"], 60_000) is None
        assert len(sells) == 1

    def test_sell_fill_updates_cash_immediately_not_only_at_next_sync(self, rig):
        """
        cash 가 체결 시점이 아니라 다음 30초 대사까지 지연되면, 그 사이 킬스위치
        판정이 매도금액을 통째로 못 본 것처럼 계산해 정상 익절에도 오발동한다.
        """
        client, risk, db, ex = rig
        risk.cash = risk.orderable_cash = 7_000_000
        risk.open_position("005930", 40, 75_000, name="삼성전자")  # 잔고상 300만 평가

        po = ex.submit_exit(ExitOrder("005930", 40, "1차익절 +3.00%"), 77_250)
        ex.on_realtime_fill(fill_msg(po.order_no, "005930", "SELL", 40, 77_250))

        # 대사(sync) 없이도 매도대금(수수료 차감)이 즉시 cash 에 반영되어 있어야 한다.
        # fill_msg 기본값: 938(수수료)=150, 939(세금)=0.
        assert risk.cash == pytest.approx(7_000_000 + 40 * 77_250 - 150)
        assert risk.mark_to_market({}) == pytest.approx(10_089_850)

    def test_realtime_fill_side_trusts_pending_order_over_field_907(self, rig):
        """
        907(매도수구분) 이 비거나 예상 밖 값으로 와도, 우리가 이미 알고 있는
        po.side(주문 시점에 확정된 방향)로 정확히 처리해야 한다 — 907 단독 의존은
        매도 체결을 매수로 오분류해 유령 포지션을 만들 수 있다.
        """
        client, risk, db, ex = rig
        risk.open_position("005930", 10, 70_000, name="삼성전자")
        po = ex.submit_exit(ExitOrder("005930", 10, "손절 -2.00%"), 68_600)

        msg = fill_msg(po.order_no, "005930", "SELL", 10, 68_600)
        msg["907"] = ""  # 필드 누락/오염 상황을 흉내낸다

        ex.on_realtime_fill(msg)

        assert "005930" not in risk.positions   # 매도로 정상 처리되어 청산됨
        assert ex.stats.sell_fills == 1
        assert ex.stats.buy_fills == 0


# ------------------------------------------------------------------ 미체결
class TestUnfilledSweep:
    def test_cancels_stale_buy_and_retries_once(self, rig, monkeypatch):
        monkeypatch.setattr(cfg, "UNFILLED_TIMEOUT_SEC", 30)
        monkeypatch.setattr(cfg, "UNFILLED_MAX_CHASE", 1)
        client, risk, db, ex = rig
        po = ex.submit_buy("005930", "삼성전자", 10, 70_000, 70_000)
        po.sent_at = datetime.now() - timedelta(seconds=31)

        ex.sweep_unfilled(lambda c: 70_050)
        kinds = [s[0] for s in client.sent]
        assert "CANCEL" in kinds
        assert kinds.count("BUY") == 2         # 취소 후 1회 재시도
        assert ex.stats.cancels == 1

    def test_stale_sell_is_resent_not_abandoned(self, rig, monkeypatch):
        monkeypatch.setattr(cfg, "UNFILLED_TIMEOUT_SEC", 30)
        client, risk, db, ex = rig
        risk.open_position("005930", 10, 70_000, name="삼성전자")
        po = ex.submit_exit(ExitOrder("005930", 10, "손절"), 68_000)
        po.sent_at = datetime.now() - timedelta(seconds=31)

        ex.sweep_unfilled(lambda c: 67_900)
        sells = [s for s in client.sent if s[0] == "SELL"]
        assert len(sells) == 2                 # 청산은 반드시 나가야 한다
        assert "재전송" in list(ex.pending.values())[0].reason

    def test_cancel_failure_stops_tracking(self, rig, monkeypatch):
        """이미 체결된 주문의 취소는 실패한다. 무한 재시도에 빠지면 안 된다."""
        monkeypatch.setattr(cfg, "UNFILLED_TIMEOUT_SEC", 30)
        client, risk, db, ex = rig
        po = ex.submit_buy("005930", "삼성전자", 10, 70_000, 70_000)
        po.sent_at = datetime.now() - timedelta(seconds=31)
        client.fail_on = {"cancel"}

        ex.sweep_unfilled(lambda c: 70_000)
        assert ex.pending == {}

    def test_reconcile_clears_orders_missing_from_broker(self, rig):
        client, risk, db, ex = rig
        po = ex.submit_buy("005930", "삼성전자", 10, 70_000, 70_000)
        client.unfilled = []                   # 증권사 원장에 없다 = 체결 또는 취소됨
        ex.reconcile()
        assert ex.pending == {}
        assert risk.can_buy("005930", 70_000)[0] is True


# ------------------------------------------------------------------ DRY-RUN
class TestDryRun:
    def test_no_order_is_sent_but_state_advances(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "d.db")
        db = Database(tmp_path / "d.db")
        risk = RiskManager()
        risk.cash = risk.orderable_cash = risk.total_equity = 10_000_000
        risk.reset_day(10_000_000)
        client = FakeClient(dry_run=True)
        ex = OrderExecutor(client, risk, db, NullNotifier())

        po = ex.submit_buy("005930", "삼성전자", 10, 70_000, 70_000)
        assert po is not None
        # 실제 주문 API 는 호출되었지만 FakeClient 라 기록만 남는다.
        # 진짜 KiwoomClient 의 dry_run 경로는 _send_order 에서 차단된다(아래 테스트).
        assert risk.positions["005930"].qty == 10      # 즉시 체결 시뮬레이션
        assert ex.pending == {}

    def test_real_client_dry_run_never_calls_request(self, monkeypatch):
        from trading_bot.core.kiwoom_client import KiwoomClient

        client = KiwoomClient(app_key="k", app_secret="s", dry_run=True)
        called = []
        monkeypatch.setattr(client, "request", lambda *a, **k: called.append(a))

        res = client.buy("005930", 10, "", "7")
        assert called == []                    # HTTP 요청이 한 번도 나가지 않는다
        assert res["dry_run"] is True
        assert res["ord_no"].startswith("DRY")

        client.sell("005930", 10)
        client.cancel("X", "005930", 10)
        assert called == []


# ------------------------------------------------------------------ 실시간
class TestWebSocketProtocol:
    """LOGIN -> REG -> REAL, PING 에코, 재접속 시 구독 복원."""

    class FakeWS:
        def __init__(self, script):
            self.sent: list[dict] = []
            self._script = list(script)

        async def send(self, raw):
            self.sent.append(json.loads(raw))

        async def recv(self):
            return json.dumps(self._script.pop(0))

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._script:
                raise StopAsyncIteration
            return json.dumps(self._script.pop(0))

        async def close(self):
            pass

    def _ws(self, script, on_tick=None, on_fill=None):
        ws = KiwoomWebSocket(token_provider=lambda: "TOKEN", on_tick=on_tick, on_fill=on_fill)
        ws._ws = self.FakeWS(script)
        return ws

    def test_login_sends_token(self):
        ws = self._ws([{"trnm": "LOGIN", "return_code": 0}])
        asyncio.run(ws._login())
        assert ws._ws.sent[0] == {"trnm": "LOGIN", "token": "TOKEN"}

    def test_login_failure_raises(self):
        ws = self._ws([{"trnm": "LOGIN", "return_code": 1, "return_msg": "토큰 오류"}])
        with pytest.raises(RuntimeError):
            asyncio.run(ws._login())

    def test_ping_is_echoed_verbatim(self):
        ping = {"trnm": "PING", "seq": 7}
        ws = self._ws([ping])
        asyncio.run(ws._recv_loop())
        assert ws._ws.sent == [ping]     # 받은 그대로 되돌려야 연결이 유지된다

    def test_real_0B_tick_is_dispatched(self):
        got = []
        values = {"20": "091530", "10": "-70800", "15": "+120", "13": "1500000",
                  "228": "112.35"}
        ws = self._ws(
            [{"trnm": "REAL", "data": [{"type": "0B", "item": "005930", "values": values}]}],
            on_tick=lambda code, v: got.append((code, v)),
        )
        asyncio.run(ws._recv_loop())
        assert got[0][0] == "005930"
        assert got[0][1]["228"] == "112.35"

    def test_real_00_fill_is_dispatched(self):
        got = []
        ws = self._ws(
            [{"trnm": "REAL", "data": [{"type": "00", "item": "", "values": {"9203": "1"}}]}],
            on_fill=lambda v: got.append(v),
        )
        asyncio.run(ws._recv_loop())
        assert got == [{"9203": "1"}]

    def test_callback_exception_does_not_kill_the_stream(self):
        seen = []

        def boom(code, v):
            seen.append(code)
            raise ValueError("콜백 폭발")

        ws = self._ws(
            [
                {"trnm": "REAL", "data": [{"type": "0B", "item": "A", "values": {}}]},
                {"trnm": "REAL", "data": [{"type": "0B", "item": "B", "values": {}}]},
            ],
            on_tick=boom,
        )
        asyncio.run(ws._recv_loop())
        assert seen == ["A", "B"]     # 첫 예외 후에도 계속 수신한다

    def test_subscribe_registers_and_restores(self):
        ws = self._ws([])
        asyncio.run(ws.subscribe(["005930", "000660"]))
        reg = ws._ws.sent[-1]
        assert reg["trnm"] == "REG" and reg["refresh"] == "1"
        assert reg["data"][0]["type"] == ["0B"]
        assert sorted(reg["data"][0]["item"]) == ["000660", "005930"]

        # 재접속 시 기존 구독을 복원한다
        ws._ws = self.FakeWS([])
        asyncio.run(ws._restore_subscriptions())
        assert ws._ws.sent[-1]["data"][0]["item"] == ["000660", "005930"]

    def test_order_subscription_uses_separate_group(self):
        ws = self._ws([])
        asyncio.run(ws._register_orders())
        msg = ws._ws.sent[-1]
        assert msg["grp_no"] == "2" and msg["data"][0]["type"] == ["00"]

    def test_bare_string_ping_is_echoed(self):
        """
        공식 클라이언트는 PING 을 JSON 뿐 아니라 맨 문자열 "PING" 으로도 받는다.
        (근거: official_reference/kiwoom/core/ws_client.py::_is_ping_message)
        놓치면 서버가 연결을 끊는다.
        """
        ws = KiwoomWebSocket(token_provider=lambda: "T")

        class RawWS(self.FakeWS):
            async def send(self, raw):
                self.sent.append(raw)           # 원문 그대로 보관(JSON 이 아닐 수 있다)

            async def __anext__(self):
                if not self._script:
                    raise StopAsyncIteration
                return self._script.pop(0)      # JSON 직렬화 없이 원문 그대로

        ws._ws = RawWS(["PING"])
        asyncio.run(ws._recv_loop())
        assert ws._ws.sent == ["PING"]          # 원문 그대로 되돌려 보낸다

    def test_ping_before_login_ack_is_answered(self):
        """LOGIN 응답보다 PING 이 먼저 와도 로그인이 성립해야 한다."""
        ws = self._ws([{"trnm": "PING"}, {"trnm": "LOGIN", "return_code": 0}])
        asyncio.run(ws._login())
        assert {"trnm": "PING"} in ws._ws.sent
        assert ws._connected.is_set()

    def test_login_string_return_code_zero_is_success(self):
        ws = self._ws([{"trnm": "LOGIN", "return_code": "0"}])
        asyncio.run(ws._login())
        assert ws._connected.is_set()

    def test_token_error_on_login_flags_auth_failure(self):
        """토큰 문제로 로그인이 거부되면 같은 토큰으로 재접속해도 소용없다."""
        ws = self._ws([{"trnm": "LOGIN", "return_code": 8005, "return_msg": "토큰 만료"}])
        with pytest.raises(RuntimeError):
            asyncio.run(ws._login())
        assert ws._auth_failed is True

    def test_embedded_token_code_on_login_is_detected(self):
        ws = self._ws([{"trnm": "LOGIN", "return_code": 3, "return_msg": "인증 실패 CODE=8005"}])
        with pytest.raises(RuntimeError):
            asyncio.run(ws._login())
        assert ws._auth_failed is True

    def test_non_auth_login_failure_does_not_flag_auth(self):
        ws = self._ws([{"trnm": "LOGIN", "return_code": 1501, "return_msg": "입력 오류"}])
        with pytest.raises(RuntimeError):
            asyncio.run(ws._login())
        assert ws._auth_failed is False


# ------------------------------------------------------------------ 응답코드 처리
class TestReturnCodeHandling:
    """
    키움은 업무 오류를 HTTP 200 + 본문 return_code 로 내려보낸다.
    토큰 만료도 401 이 아니라 본문 코드(8005) 또는 return_msg 안의 '[8005:...]' 로 온다.
    (근거: official_reference/kiwoom/core/client.py, errors.py)
    """

    def _client(self, monkeypatch, responses):
        """responses: 순서대로 돌려줄 (status_code, json) 목록."""
        from trading_bot.core.kiwoom_client import AccessToken, KiwoomClient

        client = KiwoomClient(app_key="k", app_secret="s", dry_run=False)
        client._token = AccessToken("TOK", "bearer", "20991231235959")

        calls = []
        queue = list(responses)

        class FakeResp:
            def __init__(self, status, payload):
                self.status_code = status
                self._payload = payload
                self.headers = {}
                self.text = str(payload)

            def json(self):
                return self._payload

            def raise_for_status(self):
                pass

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append({"url": url, "body": json})
            status, payload = queue.pop(0)
            return FakeResp(status, payload)

        monkeypatch.setattr(client._session, "post", fake_post)
        monkeypatch.setattr(client, "_bucket", type("B", (), {"acquire": lambda self: None})())
        monkeypatch.setattr("time.sleep", lambda *_: None)
        return client, calls

    def test_string_zero_return_code_is_success(self, monkeypatch):
        """return_code 는 0, "0", "0000" 로 섞여 온다. 문자열 0 을 오류로 보면 안 된다."""
        for ok in (0, "0", "0000"):
            client, _ = self._client(monkeypatch, [(200, {"return_code": ok, "entr": "1000"})])
            data, _, _ = client.request("kt00001", "/api/dostk/acnt", {})
            assert data["entr"] == "1000"

    def test_token_expiry_in_body_triggers_refresh_and_retry(self, monkeypatch):
        """HTTP 200 + return_code 8005 -> 토큰 재발급 후 재시도해야 한다."""
        client, calls = self._client(monkeypatch, [
            (200, {"return_code": 8005, "return_msg": "Token이 유효하지 않습니다"}),
            (200, {"return_code": 0, "entr": "5000"}),
        ])
        refreshed = []
        monkeypatch.setattr(client, "issue_token",
                            lambda force=False: refreshed.append(force) or client._token)

        data, _, _ = client.request("kt00001", "/api/dostk/acnt", {})
        assert data["entr"] == "5000"
        # access_token 조회(force=False)와 섞이므로 '강제' 재발급만 센다
        assert refreshed.count(True) == 1
        assert len(calls) == 2

    def test_token_expiry_embedded_in_message_is_detected(self, monkeypatch):
        """일반 코드(3) + return_msg '[8005:...]' 형태도 잡아야 한다."""
        client, calls = self._client(monkeypatch, [
            (200, {"return_code": 3, "return_msg": "[8005:Token이 유효하지 않습니다]"}),
            (200, {"return_code": 0, "entr": "7000"}),
        ])
        refreshed = []
        monkeypatch.setattr(client, "issue_token",
                            lambda force=False: refreshed.append(force) or client._token)

        data, _, _ = client.request("kt00001", "/api/dostk/acnt", {})
        assert data["entr"] == "7000"
        assert refreshed.count(True) == 1

    def test_rate_limit_code_backs_off_and_retries(self, monkeypatch):
        client, calls = self._client(monkeypatch, [
            (200, {"return_code": 1700, "return_msg": "유량 제한"}),
            (200, {"return_code": 0, "entr": "9000"}),
        ])
        data, _, _ = client.request("kt00001", "/api/dostk/acnt", {})
        assert data["entr"] == "9000"
        assert len(calls) == 2

    def test_business_error_raises_immediately_without_retry(self, monkeypatch):
        """입력 오류 같은 업무 오류는 재시도해도 소용없다 — 바로 올린다."""
        client, calls = self._client(monkeypatch, [
            (200, {"return_code": 1501, "return_msg": "입력값 오류"}),
        ])
        with pytest.raises(KiwoomAPIError) as exc:
            client.request("kt10000", "/api/dostk/ordr", {})
        assert exc.value.code == 1501
        assert len(calls) == 1

    def test_auth_refresh_happens_only_once_per_request(self, monkeypatch):
        """재발급 후에도 계속 8005 면 무한 재발급 루프에 빠지면 안 된다."""
        client, calls = self._client(monkeypatch, [
            (200, {"return_code": 8005, "return_msg": "만료"}),
            (200, {"return_code": 8005, "return_msg": "만료"}),
        ])
        refreshed = []
        monkeypatch.setattr(client, "issue_token",
                            lambda force=False: refreshed.append(force) or client._token)
        with pytest.raises(KiwoomAPIError):
            client.request("kt00001", "/api/dostk/acnt", {})
        assert refreshed.count(True) == 1

    def test_unfilled_omits_stock_code_when_querying_all(self, monkeypatch):
        client, calls = self._client(monkeypatch, [(200, {"return_code": 0, "oso": []})])
        client.get_unfilled()
        assert "stk_cd" not in calls[0]["body"]
        assert calls[0]["body"]["all_stk_tp"] == "0"


# ------------------------------------------------------------------ 주문 거부
class TestOrderRejection:
    def test_rejection_frees_the_slot_and_alerts(self, rig):
        """실시간 00 의 919(거부사유)가 오면 즉시 정리해야 다음 기회를 잡는다."""
        client, risk, db, ex = rig
        po = ex.submit_buy("005930", "삼성전자", 10, 70_000, 70_000)
        assert risk.can_buy("005930", 70_000)[0] is False

        values = fill_msg(po.order_no, "005930", "BUY", 0, 0)
        values["911"], values["910"] = "0", "0"
        values["919"] = "주문가능금액 부족"
        ex.on_realtime_fill(values)

        assert ex.pending == {}
        assert risk.can_buy("005930", 70_000)[0] is True
        assert ex.stats.rejects == 1
        assert risk.positions == {}


# ------------------------------------------------------------------ 매매구분
_RC4026 = "[2000](RC4026:모의투자 최유리지정가와 최우선지정가 주문은 불가합니다.)"


class RejectingClient(FakeClient):
    """지정한 매매구분을 거부하는 대역 (모의투자 RC4026 재현)."""

    def __init__(self, bad_types=("6", "7", "16", "26")):
        super().__init__()
        self.bad = set(bad_types)

    def buy(self, code, qty, price="", trade_type="7", cond_price=""):
        if trade_type in self.bad:
            raise KiwoomAPIError("kt10000", 2000, _RC4026)
        return super().buy(code, qty, price, trade_type, cond_price)

    def sell(self, code, qty, price="", trade_type="3", cond_price=""):
        if trade_type in self.bad:
            raise KiwoomAPIError("kt10001", 2000, _RC4026)
        return super().sell(code, qty, price, trade_type, cond_price)


class TestOrderTypeFallback:
    def test_mock_env_never_ships_unsupported_entry_type(self):
        """모의투자에서 6/7/16/26 은 기동 시점에 대체되어 나가지 않아야 한다."""
        assert cfg.IS_MOCK
        assert cfg.ENTRY_ORDER_TYPE not in cfg.MOCK_UNSUPPORTED_ORDER_TYPES
        assert cfg.EXIT_ORDER_TYPE not in cfg.MOCK_UNSUPPORTED_ORDER_TYPES

    def test_rc4026_downgrades_and_retries_once(self, rig, monkeypatch):
        client, risk, db, ex = rig
        ex.client = client = RejectingClient()
        ex.entry_order_type = "7"          # 설정이 잘못 들어온 상황을 가정

        po = ex.submit_buy("005930", "삼성전자", 10, 70_000, 70_000)
        assert po is not None
        assert client.sent[0][1]["tt"] == "0"      # 대체 구분으로 접수
        assert client.sent[0][1]["price"] == 70_000  # 지정가는 가격을 실어야 한다
        assert ex.entry_order_type == "0"          # 세션 내내 유지
        assert ex.stats.rejects == 0

        ex.submit_buy("000660", "SK하이닉스", 5, 70_000, 70_000)
        assert client.sent[1][1]["tt"] == "0"      # 두 번째 주문은 거부 없이 바로

    def test_exit_falls_back_so_position_still_closes(self, rig):
        client, risk, db, ex = rig
        po = ex.submit_buy("005930", "삼성전자", 10, 70_000, 70_000)
        ex.on_realtime_fill(fill_msg(po.order_no, "005930", "BUY", 10, 70_000))

        ex.client = client = RejectingClient()
        ex.exit_order_type = "6"
        assert ex.submit_exit(ExitOrder("005930", 10, "손절", urgent=True), 69_000) is not None
        assert client.sent[0][0] == "SELL" and client.sent[0][1]["tt"] == "0"


class TestRejectThrottle:
    def test_same_code_is_not_retried_during_cooldown(self, rig, monkeypatch):
        monkeypatch.setattr(cfg, "ORDER_REJECT_COOLDOWN_SEC", 300)
        client, risk, db, ex = rig
        ex.client = FakeClient(fail_on={"buy"})

        assert ex.submit_buy("005930", "삼성전자", 10, 70_000, 70_000) is None
        assert ex.stats.rejects == 1
        # 다음 봉에서 같은 시그널이 또 떠도 API 를 두드리지 않는다
        assert ex.submit_buy("005930", "삼성전자", 10, 70_000, 70_000) is None
        assert ex.stats.rejects == 1
        assert ex.client.sent == []
        assert risk.can_buy("005930", 70_000)[0] is True   # 슬롯은 풀려 있다

    def test_consecutive_rejects_halt_new_entries(self, rig, monkeypatch):
        monkeypatch.setattr(cfg, "MAX_CONSECUTIVE_REJECTS", 3)
        client, risk, db, ex = rig
        ex.client = FakeClient(fail_on={"buy"})

        for i, code in enumerate(("005930", "000660", "035720")):
            assert ex.submit_buy(code, f"종목{i}", 10, 70_000, 70_000) is None
        assert ex.entry_halted_reason
        assert ex.stats.rejects == 3

        # 중단 뒤로는 새 종목도 주문하지 않는다 (거부 카운트도 늘지 않는다)
        assert ex.submit_buy("068270", "셀트리온", 10, 70_000, 70_000) is None
        assert ex.stats.rejects == 3

        # 청산은 계속 나가야 한다
        ex.client = FakeClient()
        ex.resume_entries()
        assert ex.entry_halted_reason == ""
        assert ex.submit_buy("005930", "삼성전자", 10, 70_000, 70_000) is not None

    def test_error_list_folds_duplicates(self, rig):
        client, risk, db, ex = rig
        ex.client = FakeClient(fail_on={"buy"})
        for _ in range(20):
            ex._reject_until.clear()
            ex.entry_halted_reason = ""
            ex.submit_buy("005930", "삼성전자", 10, 70_000, 70_000)
        assert len(ex.stats.errors) == 1
