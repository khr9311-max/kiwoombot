"""
main.TradingBot 배선 통합 테스트.

실시간 틱을 밀어 넣어 봉 집계 -> 시그널 -> 메타필터 -> 리스크 -> 주문 -> 체결 -> 청산
까지 한 줄로 이어지는지 확인한다. 네트워크·스케줄러는 타지 않는다.
"""
from __future__ import annotations

import asyncio
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

from test_execution import FakeClient, fill_msg  # noqa: E402

from trading_bot.config import settings as cfg  # noqa: E402
from trading_bot.core.notifier import NullNotifier  # noqa: E402
from trading_bot.database.db import Database  # noqa: E402
from trading_bot.main import TradingBot  # noqa: E402


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "i.db")
    monkeypatch.setattr(cfg, "NO_NEW_ENTRY_AFTER", datetime(2026, 1, 1, 23, 59).time())
    monkeypatch.setattr(cfg, "FLATTEN_TIME", datetime(2026, 1, 1, 23, 58).time())
    monkeypatch.setattr(cfg, "SIGNAL_SCORE_THRESHOLD", 4.0)

    b = TradingBot.__new__(TradingBot)      # __init__ 의 네트워크 의존을 건너뛴다
    from trading_bot.core.bars import BarStore
    from trading_bot.core.executor import OrderExecutor
    from trading_bot.core.meta_filter import PassThroughFilter
    from trading_bot.core.risk_manager import RiskManager
    from trading_bot.core.strategy import SignalEngine

    b.client = FakeClient()
    b.db = Database(tmp_path / "i.db")
    b.notifier = NullNotifier()
    b.risk = RiskManager()
    b.risk.cash = b.risk.orderable_cash = b.risk.total_equity = 10_000_000
    b.risk.reset_day(10_000_000)
    b.engine = SignalEngine()
    b.meta = PassThroughFilter()
    b.bars = BarStore(maxlen=400, interval_min=1)
    b.executor = OrderExecutor(b.client, b.risk, b.db, b.notifier)
    b.ws = None
    b.scheduler = None
    b.universe = ["005930"]
    b.names = {"005930": "삼성전자"}
    b.session_active = True
    b._stop = asyncio.Event()
    b._tasks = []
    b._last_sync = datetime.min
    b._session_opened_at = datetime.now()
    b._factor_a_seen = False
    b._factor_a_alerted = False
    return b


async def drain() -> None:
    """
    on_tick 이 만든 evaluate 태스크가 모두 끝날 때까지 기다린다.
    고정 sleep 은 첫 테스트에서 스레드풀·SQLite 커넥션 초기화 비용 때문에
    부족할 수 있어 실행 순서에 따라 결과가 흔들린다.
    """
    for _ in range(200):
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if not pending:
            return
        await asyncio.wait(pending, timeout=5)


def tick(price, hhmmss, cum_vol, cum_turnover, strength="115.0", high=None, low=None):
    """0B 실시간 체결 메시지. cum_turnover 는 '원' 단위로 받되, 필드 14 는 실제 키움
    스펙대로 '백만원' 단위 문자열로 인코딩한다 — main.py 의 * 1_000_000 환산 경로를
    테스트가 실제로 지나가도록(원 단위를 그대로 흘려보내던 예전 버그의 사각지대 방지)."""
    return {
        "20": hhmmss,
        "10": f"+{int(price)}",
        "15": "100",
        "13": str(cum_vol),
        "14": str(int(cum_turnover) // 1_000_000),
        "16": "70000",
        "17": str(int(high or price)),
        "18": str(int(low or price)),
        "27": str(int(price) + 50),
        "28": str(int(price)),
        "228": strength,
    }


class TestTickToOrderPipeline:
    def test_uptrend_ticks_produce_a_buy_order(self, bot):
        """상승 추세 + 거래대금 유입 + 체결강도 우위 -> 4점 이상 -> 매수."""
        bot.engine.set_prev_turnover({"005930": 1_000_000_000})

        async def drive():
            base = datetime(2026, 8, 27, 9, 0)
            price = 70_000.0
            cum_vol, cum_turn = 0, 0.0
            for m in range(70):
                # 완만한 우상향: 이평 정배열 + RSI 상승을 만든다
                price *= 1.0015 if m % 5 else 0.9995
                cum_vol += 5_000
                cum_turn += 5_000 * price
                bot.on_tick("005930", tick(price, (base + timedelta(minutes=m)).strftime("%H%M%S"),
                                           cum_vol, cum_turn))
                # 실전에서는 봉 간격이 1분이라 평가가 시세를 따라잡는다.
                # 틱마다 평가를 끝내야 슬리피지 가드가 끼어들지 않는다.
                await drain()
            await drain()

        asyncio.run(drive())

        buys = [s for s in bot.client.sent if s[0] == "BUY"]
        assert buys, "상승 추세에서 매수 주문이 나오지 않았습니다"
        assert bot.executor.stats.buy_orders >= 1
        assert buys[0][1]["tt"] == cfg.ENTRY_ORDER_TYPE       # 최우선지정가로 진입
        assert buys[0][1]["qty"] > 0

        # 주문 단계에서는 아직 포지션이 아니다. 체결 통보가 와야 보유로 잡힌다.
        assert bot.risk.positions == {}
        po = list(bot.executor.pending.values())[0]
        bot.executor.on_realtime_fill(
            fill_msg(po.order_no, "005930", "BUY", po.qty, po.price)
        )
        pos = bot.risk.positions["005930"]
        assert pos.qty == po.qty
        assert bot.executor.pending == {}

    def test_concurrent_signals_do_not_double_order(self, bot, monkeypatch):
        """
        회귀 테스트: evaluate() 는 can_buy 확인과 실제 발주 사이에 await 지점이 여러 개
        있다. 같은 종목의 시그널이 연달아 나도 주문은 한 번만 나가야 한다.

        평가 태스크가 서로 겹치도록 틱을 몰아넣고, 슬리피지 가드를 풀어
        '자리 선점' 만이 유일한 관문이 되게 한다.
        """
        monkeypatch.setattr(cfg, "SLIPPAGE_GUARD_PCT", 10.0)
        bot.engine.set_prev_turnover({"005930": 1_000_000_000})

        async def drive():
            base = datetime(2026, 8, 27, 9, 0)
            price, cum_vol, cum_turn = 70_000.0, 0, 0.0
            for m in range(70):
                price *= 1.0015 if m % 5 else 0.9995
                cum_vol += 5_000
                cum_turn += 5_000 * price
                bot.on_tick("005930", tick(price, (base + timedelta(minutes=m)).strftime("%H%M%S"),
                                           cum_vol, cum_turn))
                await asyncio.sleep(0)
            await drain()

        asyncio.run(drive())

        buys = [s for s in bot.client.sent if s[0] == "BUY"]
        assert len(buys) == 1, f"같은 종목에 매수 주문이 {len(buys)}건 나갔습니다"
        assert len(bot.executor.pending) == 1

    def test_max_positions_is_never_exceeded_under_concurrency(self, bot, monkeypatch):
        monkeypatch.setattr(cfg, "MAX_POSITIONS", 2)
        monkeypatch.setattr(cfg, "SLIPPAGE_GUARD_PCT", 10.0)
        codes = ["005930", "000660", "035720", "005380"]
        bot.universe = codes
        bot.names = {c: c for c in codes}
        bot.engine.set_prev_turnover({c: 1_000_000_000 for c in codes})

        async def drive():
            base = datetime(2026, 8, 27, 9, 0)
            price = {c: 70_000.0 for c in codes}
            cum_vol, cum_turn = 0, 0.0
            for m in range(70):
                cum_vol += 5_000
                for c in codes:
                    price[c] *= 1.0015 if m % 5 else 0.9995
                    cum_turn += 5_000 * price[c]
                    bot.on_tick(c, tick(price[c],
                                        (base + timedelta(minutes=m)).strftime("%H%M%S"),
                                        cum_vol, cum_turn))
                await asyncio.sleep(0)
            await drain()

        asyncio.run(drive())
        buys = [s for s in bot.client.sent if s[0] == "BUY"]
        assert len(buys) <= cfg.MAX_POSITIONS, f"최대 보유 종목 수를 넘겼습니다: {len(buys)}건"

    def test_signals_are_persisted_with_features_for_training(self, bot):
        """메타 모델 학습을 위해 시그널·피처가 DB 에 남아야 한다."""
        bot.engine.set_prev_turnover({"005930": 1_000_000_000})

        async def drive():
            base = datetime(2026, 8, 27, 9, 0)
            price, cum_vol, cum_turn = 70_000.0, 0, 0.0
            for m in range(70):
                price *= 1.0015 if m % 5 else 0.9995
                cum_vol += 5_000
                cum_turn += 5_000 * price
                bot.on_tick("005930", tick(price, (base + timedelta(minutes=m)).strftime("%H%M%S"),
                                           cum_vol, cum_turn))
                await drain()
            await drain()

        asyncio.run(drive())

        rows = bot.db.unlabeled_signals("BUY")
        assert rows, "시그널이 DB 에 기록되지 않았습니다"
        feats = rows[0]["features"]
        from trading_bot.core.meta_filter import FEATURE_ORDER
        for f in FEATURE_ORDER:
            assert f in feats, f"학습 피처 누락: {f}"

    def test_flat_market_produces_no_order(self, bot):
        """횡보장에서는 진입하지 않아야 한다(오탐 방지)."""
        bot.engine.set_prev_turnover({"005930": 1_000_000_000_000})   # 거래대금 팩터 미충족

        async def drive():
            base = datetime(2026, 8, 27, 9, 0)
            for m in range(70):
                price = 70_000 + (50 if m % 2 else -50)
                bot.on_tick("005930", tick(price, (base + timedelta(minutes=m)).strftime("%H%M%S"),
                                           m * 100, m * 100 * 70_000, strength="95.0"))
                await asyncio.sleep(0)
            await drain()

        asyncio.run(drive())
        assert [s for s in bot.client.sent if s[0] == "BUY"] == []
        assert bot.risk.positions == {}


class TestExitPipeline:
    def test_stop_loss_fires_from_the_exit_loop(self, bot, monkeypatch):
        monkeypatch.setattr(cfg, "STOP_LOSS_PCT", -0.02)
        bot.risk.open_position("005930", 10, 70_000, name="삼성전자")

        async def drive():
            base = datetime(2026, 8, 27, 10, 0)
            # 손절선(-2%) 아래로 떨어뜨린다
            bot.bars.get("005930").on_tick(base, 68_500, cum_volume=1000)
            await bot._check_exits()

        asyncio.run(drive())
        sells = [s for s in bot.client.sent if s[0] == "SELL"]
        assert sells and sells[0][1]["qty"] == 10
        assert sells[0][1]["tt"] == cfg.EXIT_ORDER_TYPE      # 시장가 청산

    def test_kill_switch_liquidates_and_halts(self, bot, monkeypatch):
        monkeypatch.setattr(cfg, "DAILY_LOSS_LIMIT_PCT", -0.03)
        bot.risk.cash = 5_000_000
        bot.risk.open_position("005930", 100, 50_000, name="삼성전자")
        bot.risk.day_start_equity = 10_000_000

        async def drive():
            base = datetime(2026, 8, 27, 10, 0)
            bot.bars.get("005930").on_tick(base, 46_000, cum_volume=1000)  # 평가액 급락
            await bot._check_exits()

        asyncio.run(drive())
        assert bot.risk.kill_switch is True
        assert bot.session_active is False
        assert bot.risk.can_buy("000660", 1000)[0] is False
        assert [s for s in bot.client.sent if s[0] == "SELL"]

    def test_round_trip_records_a_completed_trade(self, bot):
        bot.risk.open_position("005930", 10, 70_000, name="삼성전자")
        pos = bot.risk.positions["005930"]

        async def drive():
            base = datetime(2026, 8, 27, 10, 0)
            bot.bars.get("005930").on_tick(base, 68_000, cum_volume=1000)
            await bot._check_exits()

        asyncio.run(drive())
        po = list(bot.executor.pending.values())[0]
        bot.executor.on_realtime_fill(fill_msg(po.order_no, "005930", "SELL", 10, 68_000))

        stats = bot.db.today_stats()
        assert stats["trades"] == 1
        assert stats["realized"] == pytest.approx((68_000 - 70_000) * 10)
        assert "005930" not in bot.risk.positions
        assert pos.qty == 0


class TestFeedHealth:
    def test_stale_held_symbol_is_reported(self, bot):
        bot.risk.open_position("005930", 10, 70_000, name="삼성전자")
        s = bot.bars.get("005930")
        s.snapshot.updated = datetime.now() - timedelta(minutes=10)
        stale = bot.bars.stale_codes(datetime.now(), timedelta(minutes=5))
        assert "005930" in stale


class TestFactorAWatchdog:
    """
    Factor A 무응답 워치독. SIGNAL_SCORE_THRESHOLD(기본 4.0) 는 Factor A 없이는
    B+C+D 만으로 도달 불가능하게 설계돼 있다(3.5 < 4.0) — 이번 장애가 바로 이
    경로였다: prev_turnover 유실/단위 환산 버그로 A 가 하루 종일 0점이었다.
    """

    def test_alerts_after_grace_period_when_no_symbol_ever_scored_factor_a(self, bot):
        assert cfg.SIGNAL_SCORE_THRESHOLD > cfg.FACTOR_MAX_SCORE_WITHOUT_A
        sent = []
        bot.notifier.send = lambda text, level="INFO": sent.append((level, text)) or True

        bot._session_opened_at = datetime.now() - timedelta(minutes=31)
        bot._factor_a_seen = False
        bot._factor_a_alerted = False

        bot._check_factor_a_health()

        assert bot._factor_a_alerted is True
        assert any(lvl == "ERROR" for lvl, _ in sent)

        # 한 번 경보한 뒤로는 반복 알림을 보내지 않는다.
        sent.clear()
        bot._check_factor_a_health()
        assert sent == []

    def test_no_alert_before_grace_period_elapses(self, bot):
        sent = []
        bot.notifier.send = lambda text, level="INFO": sent.append((level, text)) or True
        bot._session_opened_at = datetime.now() - timedelta(minutes=5)

        bot._check_factor_a_health()

        assert bot._factor_a_alerted is False
        assert sent == []

    def test_no_alert_once_factor_a_has_been_seen(self, bot):
        sent = []
        bot.notifier.send = lambda text, level="INFO": sent.append((level, text)) or True
        bot._session_opened_at = datetime.now() - timedelta(minutes=31)
        bot._factor_a_seen = True

        bot._check_factor_a_health()

        assert bot._factor_a_alerted is False
        assert sent == []
