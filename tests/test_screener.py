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
