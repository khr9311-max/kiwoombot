"""
핵심 로직 회귀 테스트. 네트워크 없이 전부 돈다.

  ./.venv/Scripts/python.exe -m pytest tests -q
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("KIWOOM_APP_KEY", "TESTKEY")
os.environ.setdefault("KIWOOM_APP_SECRET", "TESTSECRET")
os.environ.setdefault("KIWOOM_ENV", "mock")
os.environ.setdefault("NOTIFIER", "null")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from trading_bot.config import settings as cfg  # noqa: E402
from trading_bot.core import indicators  # noqa: E402
from trading_bot.core.bars import BarSeries, floor_minute  # noqa: E402
from trading_bot.core.kiwoom_client import (  # noqa: E402
    TokenBucket, norm_code, parse_price, parse_int, parse_signed,
)
from trading_bot.core.meta_filter import PassThroughFilter, label_triple_barrier  # noqa: E402
from trading_bot.core.risk_manager import Position, RiskManager, kelly_fraction  # noqa: E402


# ============================================================ 응답 파싱
class TestParsing:
    def test_price_sign_prefix_is_a_marker_not_a_negative(self):
        # 키움은 하락을 '-78800' 으로 표현한다. 음수 가격이 아니다.
        assert parse_price("-78800") == 78800.0
        assert parse_price("+21150") == 21150.0
        assert parse_price("000000059000") == 59000.0
        assert parse_price("") == 0.0
        assert parse_price(None) == 0.0
        assert parse_price("-") == 0.0

    def test_int_and_signed(self):
        assert parse_int("000000000000003") == 3
        # 등락률처럼 진짜 부호가 의미 있는 필드는 부호를 살린다
        assert parse_signed("-52.71") == -52.71
        assert parse_signed("+46.25") == 46.25
        assert parse_signed("") == 0.0

    def test_code_normalisation(self):
        assert norm_code("A005930") == "005930"
        assert norm_code("005930_NX") == "005930"
        assert norm_code("005930_AL") == "005930"
        assert norm_code("005930") == "005930"


class TestTokenBucket:
    def test_limits_rate(self):
        import time

        bucket = TokenBucket(rate_per_sec=20.0, burst=2)
        start = time.monotonic()
        for _ in range(6):
            bucket.acquire()
        elapsed = time.monotonic() - start
        # 버스트 2개를 뺀 4개는 20/s 로 흘러야 하므로 최소 0.2s
        assert elapsed >= 0.15


# ============================================================ 지표
class TestIndicators:
    def test_rsi_bounds_and_extremes(self):
        up = pd.Series(range(1, 60), dtype=float)
        assert indicators.rsi(up, 14).iloc[-1] == pytest.approx(100.0)

        down = pd.Series(range(60, 1, -1), dtype=float)
        assert indicators.rsi(down, 14).iloc[-1] == pytest.approx(0.0)

        flat = pd.Series([100.0] * 60)
        assert indicators.rsi(flat, 14).iloc[-1] == 50.0

    def test_atr_positive_and_matches_true_range(self):
        n = 60
        df = pd.DataFrame({
            "high": [100 + i * 0.5 + 1 for i in range(n)],
            "low": [100 + i * 0.5 - 1 for i in range(n)],
            "close": [100 + i * 0.5 for i in range(n)],
        })
        a = indicators.atr(df["high"], df["low"], df["close"], 14)
        assert a.iloc[-1] > 0
        # 고가-저가가 항상 2 이고 갭이 0.5 이므로 ATR 은 그 사이에 수렴한다
        assert 2.0 <= a.iloc[-1] <= 2.6

    def test_frac_diff_is_stationary_ish(self):
        import numpy as np

        rng = np.random.default_rng(0)
        walk = pd.Series(rng.normal(0, 1, 500).cumsum() + 100)
        fd = indicators.frac_diff(walk, d=0.4).dropna()
        # 원계열은 추세가 있고, 분수차분 계열은 평균회귀해야 한다
        assert fd.std() < walk.std()
        # 가중치 폭만큼 앞쪽이 소진된다(d=0.4, threshold=1e-4 -> 281개). 나머지는 살아야 한다.
        assert 200 < len(fd) < 500
        assert abs(fd.mean()) < walk.std()


# ============================================================ 봉 집계
class TestBarAggregation:
    def test_floor_minute(self):
        ts = datetime(2026, 8, 27, 10, 33, 47)
        assert floor_minute(ts) == datetime(2026, 8, 27, 10, 33, 0)
        assert floor_minute(ts, 5) == datetime(2026, 8, 27, 10, 30, 0)

    def test_ticks_form_ohlc_and_close_on_minute_rollover(self):
        s = BarSeries("005930")
        base = datetime(2026, 8, 27, 9, 0, 0)
        for i, px in enumerate([100.0, 105.0, 98.0, 102.0]):
            closed = s.on_tick(base + timedelta(seconds=i * 10), px, cum_volume=10 * (i + 1))
            assert closed is None

        # 다음 분 첫 틱이 직전 봉을 확정시킨다
        closed = s.on_tick(base + timedelta(minutes=1), 103.0, cum_volume=60)
        assert closed is not None
        assert (closed.open, closed.high, closed.low, closed.close) == (100.0, 105.0, 98.0, 102.0)
        # 첫 틱은 기준선을 잡기만 하고(체결량 0), 이후 3틱의 차분 10+10+10 만 쌓인다.
        assert closed.volume == 30

    def test_first_tick_does_not_dump_whole_day_volume_into_one_bar(self):
        """
        장중에 구독을 시작하면 첫 틱의 누적거래량은 '그날 전체'다.
        기준선 없이 차분을 계산하면 하루치가 한 봉에 통째로 들어간다.
        """
        s = BarSeries("005930")
        base = datetime(2026, 8, 27, 13, 0)
        s.on_tick(base, 70_000, cum_volume=1_500_000)          # 하루치 누적
        s.on_tick(base + timedelta(seconds=30), 70_100, cum_volume=1_500_500)
        closed = s.on_tick(base + timedelta(minutes=1), 70_200, cum_volume=1_501_000)

        assert closed is not None
        assert closed.volume == 500        # 1,500,000 이 아니라 실제 증분만
        assert closed.volume < 10_000

    def test_warmup_without_cumulative_volume_still_seeds_safely(self):
        """ka10080 응답 스펙에는 누적거래량이 없다. 없어도 첫 봉이 오염되면 안 된다."""
        s = BarSeries("005930")
        base = datetime(2026, 8, 27, 9, 0)
        s.warmup([
            {"time": base + timedelta(minutes=i), "open": 100.0, "high": 101.0,
             "low": 99.0, "close": 100.0, "volume": 10}      # cum_volume 키 자체가 없음
            for i in range(30)
        ])
        s.on_tick(base + timedelta(minutes=30), 110.0, cum_volume=2_000_000)
        closed = s.on_tick(base + timedelta(minutes=31), 111.0, cum_volume=2_000_300)
        assert closed is not None
        assert closed.volume == 0          # 기준선을 잡는 봉이라 증분 없음
        assert s.to_frame()["volume"].max() < 10_000

    def test_warmup_then_live_ticks(self):
        s = BarSeries("005930")
        base = datetime(2026, 8, 27, 9, 0)
        s.warmup([
            {"time": base + timedelta(minutes=i), "open": 100.0, "high": 101.0,
             "low": 99.0, "close": 100.0, "volume": 10, "cum_volume": 10 * (i + 1)}
            for i in range(30)
        ])
        assert len(s) == 30
        s.on_tick(base + timedelta(minutes=30), 110.0, cum_volume=320)
        s.on_tick(base + timedelta(minutes=31), 111.0, cum_volume=330)
        assert len(s) == 31
        assert s.last_close == 111.0

    def test_frame_is_ordered_and_tailable(self):
        s = BarSeries("X")
        base = datetime(2026, 8, 27, 9, 0)
        for i in range(50):
            s.on_tick(base + timedelta(minutes=i), 100.0 + i, cum_volume=i)
        df = s.to_frame()
        assert df.index.is_monotonic_increasing
        assert len(s.to_frame(tail=10)) == 10
        assert list(s.to_frame(tail=10).index) == list(df.index[-10:])

    def test_cum_turnover_fallback_and_authoritative_update(self):
        """14번 필드 누락 시 tick_volume * price 폴백, 주어지면 직접 반영."""
        s = BarSeries("005930")
        base = datetime(2026, 8, 27, 9, 0)
        # 1. 14번 필드 없이 틱 수신 -> 폴백 누적
        s.on_tick(base, 10_000.0, cum_volume=100, tick_volume=100, cum_turnover=0.0)
        assert s.snapshot.cum_turnover == 1_000_000.0  # 100 * 10,000

        s.on_tick(base + timedelta(seconds=10), 10_000.0, cum_volume=150, tick_volume=50, cum_turnover=0.0)
        assert s.snapshot.cum_turnover == 1_500_000.0  # + 50 * 10,000

        # 2. 14번 필드(백만원 환산된 원 단위)가 주어지면 확정치로 업데이트
        s.on_tick(base + timedelta(seconds=20), 10_000.0, cum_volume=200, tick_volume=50, cum_turnover=2_500_000.0)
        assert s.snapshot.cum_turnover == 2_500_000.0

    def test_bar_series_and_store_reset_day(self):
        from trading_bot.core.bars import BarStore
        store = BarStore()
        s = store.get("005930")
        base = datetime(2026, 8, 27, 9, 0)
        s.on_tick(base, 10_000.0, cum_volume=100, tick_volume=100, cum_turnover=1_000_000.0, strength=120.0)
        assert s.snapshot.cum_turnover == 1_000_000.0
        assert s.snapshot.strength == 120.0

        store.reset_day()
        assert s.snapshot.cum_turnover == 0.0
        assert s.snapshot.strength == 0.0
        assert s.snapshot.cum_volume == 0



# ============================================================ 리스크 관리
def _risk(equity: float = 10_000_000) -> RiskManager:
    r = RiskManager()
    r.cash = equity
    r.orderable_cash = equity
    r.total_equity = equity
    r.reset_day(equity)
    return r


class TestSizing:
    def test_fixed_pct_respects_caps(self, monkeypatch):
        monkeypatch.setattr(cfg, "SIZING_MODE", "fixed_pct")
        monkeypatch.setattr(cfg, "POSITION_PCT", 0.10)
        monkeypatch.setattr(cfg, "MAX_ORDER_AMOUNT", 3_000_000)
        r = _risk(10_000_000)

        qty, amount, _ = r.calc_qty(price=50_000)
        assert qty == 20                     # 1,000,000 / 50,000
        assert amount == 1_000_000

        # 1회 주문금액 상한이 비중보다 먼저 걸리는 경우
        r2 = _risk(100_000_000)
        qty2, amount2, _ = r2.calc_qty(price=50_000)
        assert amount2 <= cfg.MAX_ORDER_AMOUNT

    def test_min_order_amount_blocks_dust(self, monkeypatch):
        monkeypatch.setattr(cfg, "MIN_ORDER_AMOUNT", 100_000)
        r = _risk(500_000)
        qty, _, note = r.calc_qty(price=400_000)   # 50,000원어치 -> 0주
        assert qty == 0
        assert "최소주문금액" in note

    def test_atr_risk_sizing_scales_inversely_with_volatility(self, monkeypatch):
        monkeypatch.setattr(cfg, "SIZING_MODE", "atr_risk")
        monkeypatch.setattr(cfg, "RISK_PER_TRADE_PCT", 0.005)
        monkeypatch.setattr(cfg, "ATR_STOP_MULT", 1.5)
        monkeypatch.setattr(cfg, "MAX_ORDER_AMOUNT", 100_000_000)
        r = _risk(10_000_000)
        calm, _, _ = r.calc_qty(price=50_000, atr=200)
        wild, _, _ = r.calc_qty(price=50_000, atr=2000)
        assert calm > wild > 0

    def test_half_kelly(self):
        # p=0.6, b=1.8 -> f* = (0.6*2.8 - 1)/1.8 = 0.3778, 하프 = 0.1889
        assert kelly_fraction(0.6, 1.8, fraction=0.5, cap=1.0) == pytest.approx(0.1889, abs=1e-3)
        # 기대값이 음수면 진입하지 않는다
        assert kelly_fraction(0.3, 1.0, fraction=0.5, cap=1.0) == 0.0
        # 상한이 적용된다
        assert kelly_fraction(0.9, 3.0, fraction=1.0, cap=0.2) == 0.2


class TestEntryGate:
    def test_blocks_duplicate_and_over_limit(self, monkeypatch):
        monkeypatch.setattr(cfg, "MAX_POSITIONS", 2)
        monkeypatch.setattr(cfg, "NO_NEW_ENTRY_AFTER", datetime(2026, 1, 1, 23, 59).time())
        r = _risk()

        assert r.can_buy("A", 1000)[0] is True
        r.open_position("A", 10, 1000)
        assert r.can_buy("A", 1000)[0] is False          # 중복 보유

        r.mark_pending_buy("B", 5)
        assert r.can_buy("B", 1000)[0] is False          # 체결 대기 중
        assert r.can_buy("C", 1000)[0] is False          # 보유 1 + 대기 1 = 상한

    def test_reentry_cooldown(self, monkeypatch):
        monkeypatch.setattr(cfg, "REENTRY_COOLDOWN_SEC", 600)
        monkeypatch.setattr(cfg, "NO_NEW_ENTRY_AFTER", datetime(2026, 1, 1, 23, 59).time())
        r = _risk()
        r.open_position("A", 10, 1000)
        r.reduce_position("A", 10, now=datetime.now())
        ok, why = r.can_buy("A", 1000)
        assert ok is False and "쿨다운" in why

    def test_kill_switch_blocks_everything(self):
        r = _risk()
        r.kill_switch = True
        r.kill_reason = "테스트"
        assert r.can_buy("A", 1000)[0] is False


class TestExits:
    def _pos(self, avg=10_000, qty=100, minutes_ago=0) -> Position:
        return Position(code="A", name="테스트", qty=qty, avg_price=avg, peak_price=avg,
                        entry_time=datetime.now() - timedelta(minutes=minutes_ago))

    def test_stop_loss_full_exit(self, monkeypatch):
        monkeypatch.setattr(cfg, "STOP_LOSS_PCT", -0.02)
        r = _risk()
        pos = self._pos()
        assert r.check_exit(pos, 9_900) is None          # -1.0%
        order = r.check_exit(pos, 9_800)                 # -2.0%
        assert order is not None and order.qty == 100 and "손절" in order.reason

    def test_partial_take_profit_then_trailing(self, monkeypatch):
        monkeypatch.setattr(cfg, "TAKE_PROFIT_PCT", 0.03)
        monkeypatch.setattr(cfg, "TAKE_PROFIT_RATIO", 0.5)
        monkeypatch.setattr(cfg, "TRAILING_STOP_PCT", -0.015)
        r = _risk()
        pos = self._pos()

        order = r.check_exit(pos, 10_300)                # +3%
        assert order is not None and order.qty == 50 and "1차익절" in order.reason

        # 익절 체결 후 잔량에 트레일링이 붙는다
        pos.took_profit = True
        pos.qty = 50
        assert r.check_exit(pos, 10_500) is None         # 신고가 갱신
        assert pos.peak_price == 10_500
        order2 = r.check_exit(pos, 10_500 * 0.984)       # 고점 대비 -1.6%
        assert order2 is not None and "트레일링" in order2.reason and order2.qty == 50

    def test_time_cut_only_when_flat(self, monkeypatch):
        monkeypatch.setattr(cfg, "TIME_CUT_MIN", 60)
        monkeypatch.setattr(cfg, "TIME_CUT_BAND_PCT", 0.01)
        r = _risk()

        drifting = self._pos(minutes_ago=61)
        order = r.check_exit(drifting, 10_050)           # +0.5% 횡보
        assert order is not None and "타임컷" in order.reason

        # 같은 시간이라도 방향이 나오면 살려둔다
        running = self._pos(minutes_ago=61)
        assert r.check_exit(running, 10_250) is None     # +2.5%

    def test_atr_stop_takes_precedence(self, monkeypatch):
        monkeypatch.setattr(cfg, "ATR_STOP_MULT", 1.5)
        monkeypatch.setattr(cfg, "STOP_LOSS_PCT", -0.05)   # % 손절은 아직 멀다
        r = _risk()
        r.open_position("A", 100, 10_000, atr=100)          # 손절가 9,850
        pos = r.positions["A"]
        assert pos.stop_price == pytest.approx(9_850)
        order = r.check_exit(pos, 9_840)
        assert order is not None and "ATR손절" in order.reason

    def test_pending_exit_prevents_duplicate_orders(self):
        r = _risk()
        pos = self._pos()
        pos.pending_exit = "손절"
        assert r.check_exit(pos, 1) is None


class TestKillSwitch:
    def test_triggers_on_daily_loss_and_latches(self, monkeypatch):
        monkeypatch.setattr(cfg, "DAILY_LOSS_LIMIT_PCT", -0.03)
        r = _risk(10_000_000)

        r.total_equity = 9_800_000                       # -2%
        assert r.check_kill_switch() is False

        r.total_equity = 9_690_000                       # -3.1%
        assert r.check_kill_switch() is True

        # 자산이 회복해도 그날은 다시 켜지지 않는다
        r.total_equity = 11_000_000
        assert r.check_kill_switch() is True

    def test_mark_to_market_uses_live_prices(self):
        r = _risk(5_000_000)
        r.cash = 5_000_000
        r.open_position("A", 100, 10_000)
        assert r.mark_to_market({"A": 11_000}) == 5_000_000 + 1_100_000
        # 시세가 없으면 평단으로 평가한다
        assert r.mark_to_market({}) == 5_000_000 + 1_000_000

    def test_buy_fill_does_not_inflate_equity_before_next_sync(self, monkeypatch):
        """
        cash 는 30초 대사까지 그대로인데 포지션만 즉시 잡히면, 그 사이 mark_to_market
        이 매수금액을 자산에 이중으로 계산해(cash 그대로 + 포지션 평가액) 자산이
        과대계상된다 — apply_fill_cash 로 체결 즉시 cash 를 반영해야 한다.
        """
        monkeypatch.setattr(cfg, "DAILY_LOSS_LIMIT_PCT", -0.03)
        r = _risk(10_000_000)

        r.open_position("005930", 40, 75_000)  # 300만원 매수
        r.apply_fill_cash("BUY", 40, 75_000)
        r.mark_to_market({"005930": 75_000})

        assert r.total_equity == pytest.approx(10_000_000)
        assert r.check_kill_switch() is False

    def test_sell_fill_does_not_crater_equity_before_next_sync(self, monkeypatch):
        """
        매도 체결 직후에도 cash 가 즉시 갱신되지 않으면 매도금액이 통째로 증발한
        것처럼 계산돼, 정상적인 익절인데도 킬스위치가 오발동한다 — 실제로 있었던 장애.
        """
        monkeypatch.setattr(cfg, "DAILY_LOSS_LIMIT_PCT", -0.03)
        r = _risk(10_000_000)
        r.cash = 7_000_000
        r.open_position("005930", 40, 75_000)
        r.mark_to_market({"005930": 77_250})  # +3% 익절가
        assert r.check_kill_switch() is False

        r.reduce_position("005930", 40)
        r.apply_fill_cash("SELL", 40, 77_250)
        r.mark_to_market({})

        assert r.total_equity == pytest.approx(10_090_000)  # 원금 + 익절 9만원
        assert r.daily_pnl_pct() == pytest.approx(0.009, abs=1e-6)
        assert r.check_kill_switch() is False


class TestAccountSync:
    def test_restores_unknown_holdings_from_broker(self):
        """서버 재시작 후 증권사 잔고로 내부 상태를 복원해야 한다."""
        r = RiskManager()
        r.sync(
            deposit={"entr": 3_000_000, "ord_alow_amt": 3_000_000},
            balance={
                "total_eval": 2_000_000,
                "holdings": [
                    {"code": "005930", "name": "삼성전자", "qty": 20, "sellable_qty": 20,
                     "avg_price": 70_000, "cur_price": 72_000, "eval_amt": 1_440_000,
                     "pnl": 40_000, "pnl_pct": 2.86},
                ],
            },
            mark_day_start=True,
        )
        assert "005930" in r.positions
        pos = r.positions["005930"]
        assert pos.qty == 20 and pos.avg_price == 70_000
        assert pos.peak_price == 72_000
        assert r.total_equity == 5_000_000
        assert r.day_start_equity == 5_000_000

    def test_drops_positions_no_longer_held(self):
        r = _risk()
        r.open_position("A", 10, 1000)
        r.sync(deposit={"entr": 1_000_000}, balance={"total_eval": 0, "holdings": []})
        assert r.positions == {}


# ============================================================ 메타 라벨링
class TestTripleBarrier:
    def _series(self, values: list[float], start: datetime) -> pd.Series:
        idx = pd.DatetimeIndex([start + timedelta(minutes=i + 1) for i in range(len(values))])
        return pd.Series(values, index=idx)

    def test_upper_barrier_wins(self):
        t0 = datetime(2026, 8, 27, 10, 0)
        s = self._series([101, 103, 106], t0)
        label, reason, ret = label_triple_barrier(s, t0, 100.0, atr=2.0,
                                                  upper_mult=2.0, lower_mult=1.0, vertical_min=60)
        assert label == 1 and "상단" in reason and ret == pytest.approx(0.06)

    def test_lower_barrier_wins(self):
        t0 = datetime(2026, 8, 27, 10, 0)
        s = self._series([99, 97, 105], t0)
        label, reason, _ = label_triple_barrier(s, t0, 100.0, atr=2.0,
                                                upper_mult=2.0, lower_mult=1.0, vertical_min=60)
        assert label == 0 and "하단" in reason

    def test_vertical_barrier_expiry_is_a_failure(self):
        t0 = datetime(2026, 8, 27, 10, 0)
        s = self._series([100.5, 100.2, 100.8], t0)
        label, reason, _ = label_triple_barrier(s, t0, 100.0, atr=2.0, vertical_min=60)
        assert label == 0 and "수직" in reason

    def test_respects_vertical_deadline(self):
        """수직 장벽 이후에 상단을 쳐도 성공으로 보지 않는다."""
        t0 = datetime(2026, 8, 27, 10, 0)
        idx = pd.DatetimeIndex([t0 + timedelta(minutes=m) for m in (1, 2, 90)])
        s = pd.Series([100.1, 100.2, 200.0], index=idx)
        label, reason, _ = label_triple_barrier(s, t0, 100.0, atr=2.0, vertical_min=60)
        assert label == 0 and "수직" in reason

    def test_passthrough_filter_approves(self):
        d = PassThroughFilter().decide({"rsi": 60})
        assert d.approved and d.probability == 1.0
