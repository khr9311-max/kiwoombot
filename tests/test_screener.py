"""
스크리닝 실패 시 유니버스 폴백 경로 회귀 테스트.

save_universe() -> load_universe() 왕복에서 avg_value_20d 가 유실되면, 폴백 유니버스로는
Factor A(거래대금 유입) 판정용 prev_turnover 기준선을 세울 수 없어 하루 종일 로그 없이
매매가 중단된다 — 실제로 있었던 장애의 재발 방지 테스트.

  ./.venv/Scripts/python.exe -m pytest tests/test_screener.py -q
"""
from __future__ import annotations

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

from trading_bot.core import screener  # noqa: E402
from trading_bot.core.screener import Candidate  # noqa: E402


@pytest.fixture
def universe_path(tmp_path, monkeypatch):
    path = tmp_path / "universe.json"
    monkeypatch.setattr(screener, "UNIVERSE_PATH", path)
    return path


class TestUniverseRoundTrip:
    def test_load_returns_avg_value_20d_needed_for_factor_a(self, universe_path):
        candidates = [
            Candidate(code="005930", name="삼성전자", market="KOSPI", close=70_000,
                      market_cap=4e14, avg_value_20d=1_234_500_000.0, ma60=68_000,
                      vol_surge=1.8, score=0.9),
        ]
        screener.save_universe(candidates)

        items = screener.load_universe()

        assert items and items[0]["code"] == "005930"
        assert items[0]["avg_value_20d"] == pytest.approx(1_234_500_000.0)

    def test_missing_file_returns_empty(self, universe_path):
        assert screener.load_universe() == []

    def test_expired_universe_is_ignored(self, universe_path):
        stale = {
            "date": "2020-01-01",
            "generated_at": (datetime.now() - timedelta(hours=48)).isoformat(timespec="seconds"),
            "items": [{"code": "005930", "avg_value_20d": 999.0}],
        }
        universe_path.write_text(json.dumps(stale), encoding="utf-8")
        assert screener.load_universe(max_age_hours=12) == []

    def test_empty_candidates_save_round_trips_to_empty_items(self, universe_path):
        screener.save_universe([])
        assert screener.load_universe() == []


class _StubClient:
    """screen() 이 쓰는 두 엔드포인트(ka10099 마스터, ka10081 일봉)만 흉내낸다."""

    def __init__(self, master_rows: dict[str, dict], bars: dict[str, list[dict]]):
        self.master_rows = master_rows
        self.bars = bars

    def request(self, api_id, path, params):
        # KOSPI(0) 에만 종목을 두어 시장별 중복을 피한다.
        rows = list(self.master_rows.values()) if params.get("mrkt_tp") == "0" else []
        return {"list": rows}, "", ""

    def get_daily_chart(self, code, count=90):
        return self.bars[code]


def _bars(closes: list[float], volumes: list[float], values: list[float]) -> list[dict]:
    return [
        {"date": f"2026{(i // 30) + 1:02d}{(i % 30) + 1:02d}", "close": c, "volume": v, "value": t}
        for i, (c, v, t) in enumerate(zip(closes, volumes, values))
    ]


class TestScreenWithUntradedStock:
    """
    20일 평균 거래량이 0 인 종목(사실상 무거래)이 섞여 있어도 스크리닝이 끝까지 돌아야 한다.

    거래량 급증비의 분모를 pd.NA 로 치환하면 Series 가 object dtype 이 되고
    astype(float) 이 "float() argument must be a string or a real number, not 'NAType'"
    로 터져 스크리닝 전체가 실패했다 — 실제로 있었던 장애의 재발 방지 테스트.
    """

    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setattr(screener.cfg, "SCREEN_REQUEST_DELAY_SEC", 0.0)
        monkeypatch.setattr(screener.cfg, "FIXED_UNIVERSE", ())
        n = 70

        def master_row(code, name, price, shares):
            return {
                "code": code,
                "name": name,
                "state": "",
                "auditInfo": "",
                "orderWarning": "0",
                "upName": "전기전자",
                "lastPrice": str(price),
                "listCount": str(shares),
                "regDay": "20200101",
            }

        alive_closes = [9_000 + 1_000 * i / (n - 1) for i in range(n)]
        alive_vol = [1_000_000.0] * (n - 5) + [2_000_000.0] * 5
        alive = _bars(alive_closes, alive_vol, [1e10] * n)
        # 최근 20일 거래량이 0 인 무거래 종목 -> 급증비 분모가 0
        dead = _bars([5_000.0] * n, [1_000.0] * (n - 20) + [0.0] * 20, [0.0] * n)

        return _StubClient(
            {
                "000010": master_row("000010", "살아있는종목", 10_000, 50_000_000),
                "000020": master_row("000020", "무거래종목", 5_000, 50_000_000),
            },
            {"000010": alive, "000020": dead},
        )

    def test_zero_volume_stock_does_not_break_screening(self, client):
        out = screener.screen(client, top_n=10)
        assert [c.code for c in out] == ["000010"]
        assert out[0].vol_surge == pytest.approx(1.6)
