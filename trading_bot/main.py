"""
키움 REST API 자동매매 봇 — 메인 이벤트 루프.

일간 운용 타임라인
  08:10  장 전 스크리닝 -> 감시 유니버스 확정 -> 분봉 워밍업 -> 실시간 구독
  09:00  시그널 엔진 가동 (틱 -> 1분봉 -> 다중팩터 스코어링 -> 메타필터 -> 발주)
  09:00~ 1초 주기 청산 감시(손절/익절/트레일링/타임컷), 5초 주기 미체결 스윕,
         30초 주기 계좌 대사, 초 단위 킬스위치 감시
  14:30  신규 진입 중단
  15:15  당일 포지션 일괄 청산 (오버나잇 리스크 회피)
  15:25  실시간 구독 해제, 봉 스냅샷 DB 저장
  16:00  일일 리포트 발송

실행:  python -m trading_bot.main            (스케줄 대기 모드)
       python -m trading_bot.main --now      (지금 즉시 장중 루프 시작)
       python -m trading_bot.main --screen   (스크리닝만 실행하고 종료)
       python -m trading_bot.main --check    (접속/계좌 점검만 하고 종료)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import signal
import sys
from datetime import date, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import settings as cfg
from .core import screener
from .core.bars import BarStore
from .core.executor import OrderExecutor
from .core.kiwoom_client import KiwoomAPIError, KiwoomClient, parse_price, parse_int
from .core.kiwoom_ws import KiwoomWebSocket
from .core.meta_filter import build_filter
from .core.notifier import build_notifier, fmt_daily_report
from .core.risk_manager import RiskManager
from .core.strategy import SignalEngine
from .database.db import Database

log = logging.getLogger("trading_bot")


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(cfg.LOG_LEVEL)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    fileh = logging.handlers.TimedRotatingFileHandler(
        cfg.LOG_DIR / "trading.log", when="midnight", backupCount=30, encoding="utf-8"
    )
    fileh.setFormatter(fmt)
    root.addHandler(fileh)

    for noisy in ("apscheduler", "websockets", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class TradingBot:
    def __init__(self):
        self.client = KiwoomClient()
        self.db = Database()
        self.notifier = build_notifier()
        self.risk = RiskManager()
        self.engine = SignalEngine()
        self.meta = build_filter()
        self.bars = BarStore(maxlen=400, interval_min=1)
        self.executor = OrderExecutor(self.client, self.risk, self.db, self.notifier)
        self.ws = KiwoomWebSocket(
            token_provider=lambda: self.client.access_token,
            token_refresher=lambda: self.client.issue_token(force=True),
            on_tick=self.on_tick,
            on_fill=self.executor.on_realtime_fill,
        )
        self.scheduler = AsyncIOScheduler(timezone=cfg.TZ)

        self.universe: list[str] = []
        self.names: dict[str, str] = {}
        self.session_active = False
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._last_sync = datetime.min
        # Factor A 무응답 워치독: 세션 시작 후 한 번도 A 를 획득한 종목이 없으면 경보.
        self._session_opened_at: datetime | None = None
        self._factor_a_seen = False
        self._factor_a_alerted = False

    # ================================================================ 기동
    async def start(self, run_now: bool = False) -> None:
        errors = cfg.validate()
        if errors:
            for e in errors:
                log.error("설정 오류: %s", e)
            raise SystemExit(1)

        log.info("=" * 78)
        log.info("키움 자동매매 봇 기동 | %s", cfg.Summary().as_text())
        if cfg.DRY_RUN:
            log.warning("DRY-RUN 모드입니다. 주문 API 가 실제로 전송되지 않습니다.")
        for note in cfg.ORDER_TYPE_NOTES:
            log.warning("매매구분 자동 대체: %s", note)
        log.info("주문 매매구분: 진입=%s 청산=%s", cfg.ENTRY_ORDER_TYPE, cfg.EXIT_ORDER_TYPE)
        log.info("=" * 78)

        await asyncio.to_thread(self.client.issue_token)
        await self.sync_account(mark_day_start=True)

        self.notifier.info(
            f"🤖 자동매매 봇 기동\n"
            f"  환경: {cfg.KIWOOM_ENV}{' (DRY-RUN)' if cfg.DRY_RUN else ''}\n"
            f"  자산: {self.risk.total_equity:,.0f}원 / 주문가능 {self.risk.orderable_cash:,.0f}원\n"
            f"  보유: {len(self.risk.positions)}종목"
        )

        self._tasks.append(asyncio.create_task(self.ws.run(), name="ws"))
        if not await self.ws.wait_connected(timeout=30):
            self.notifier.error("WebSocket 접속 실패 — 재접속을 계속 시도합니다")
        else:
            await self.ws.subscribe_orders()

        self._register_jobs()
        self.scheduler.start()

        self._tasks.append(asyncio.create_task(self.exit_loop(), name="exit"))
        self._tasks.append(asyncio.create_task(self.housekeeping_loop(), name="housekeeping"))

        if run_now or self._is_market_hours():
            log.info("장중 시각이므로 즉시 세션을 시작합니다")
            await self.morning_job()
            await self.session_open()

        await self._stop.wait()
        await self.shutdown()

    def _register_jobs(self) -> None:
        def cron(t):
            return CronTrigger(day_of_week="mon-fri", hour=t.hour, minute=t.minute, timezone=cfg.TZ)

        self.scheduler.add_job(self.morning_job, cron(cfg.SCREENING_TIME), id="screening",
                               misfire_grace_time=1800)
        self.scheduler.add_job(self.session_open, cron(cfg.SESSION_START), id="open",
                               misfire_grace_time=600)
        self.scheduler.add_job(self.halt_entry_job, cron(cfg.NO_NEW_ENTRY_AFTER), id="halt",
                               misfire_grace_time=600)
        self.scheduler.add_job(self.flatten_job, cron(cfg.FLATTEN_TIME), id="flatten",
                               misfire_grace_time=300)
        self.scheduler.add_job(self.session_close, cron(cfg.SESSION_END), id="close",
                               misfire_grace_time=600)
        self.scheduler.add_job(self.eod_job, cron(cfg.EOD_REPORT_TIME), id="eod",
                               misfire_grace_time=3600)

    @staticmethod
    def _is_market_hours(now: datetime | None = None) -> bool:
        now = now or datetime.now()
        return now.weekday() < 5 and cfg.SESSION_START <= now.time() < cfg.SESSION_END

    # ================================================================ 장 전
    async def morning_job(self) -> None:
        """스크리닝 -> 유니버스 확정 -> 분봉 워밍업 -> 실시간 구독."""
        if not await asyncio.to_thread(screener.is_trading_day):
            log.info("오늘은 휴장일입니다 — 스크리닝을 건너뜁니다")
            self.universe = []
            return

        log.info("--- 장 전 스크리닝 시작 ---")
        self.risk.reset_day(self.risk.total_equity)
        self.bars.reset_day()
        await self.sync_account(mark_day_start=True)

        reason = ""
        try:
            candidates = await asyncio.to_thread(screener.screen, self.client, cfg.UNIVERSE_MAX)
            if not candidates:
                reason = "조건을 통과한 종목이 없습니다 (스크리닝 조건을 완화하세요)"
        except Exception as exc:
            log.exception("스크리닝 실패")
            reason = str(exc)
            self.notifier.error(f"스크리닝 실패: {exc}\n이전 유니버스로 대체를 시도합니다")
            candidates = []

        if candidates:
            await asyncio.to_thread(screener.save_universe, candidates)
            self.universe = [c.code for c in candidates]
            self.names = {c.code: c.name for c in candidates}
            # Factor A 판정에 쓸 전일 거래대금(스크리닝 패널의 20일 평균으로 근사)
            self.engine.set_prev_turnover({c.code: c.avg_value_20d for c in candidates})
            display_items = [
                {"code": c.code, "name": c.name, "avg_value_20d": c.avg_value_20d, "vol_surge": c.vol_surge}
                for c in candidates
            ]
        else:
            # 스크리닝 실패/공백 시 어제 저장분으로 대체한다. 코드만이 아니라
            # avg_value_20d 도 함께 복원해야 Factor A(거래대금 유입) 판정이 살아난다 —
            # 그렇지 않으면 하루 종일 로그도 없이 조용히 매매가 중단된다.
            items = await asyncio.to_thread(screener.load_universe)
            self.universe = [i["code"] for i in items]
            self.names = {i["code"]: i.get("name", i["code"]) for i in items}
            self.engine.set_prev_turnover({i["code"]: i.get("avg_value_20d", 0.0) for i in items})
            display_items = items
            if items:
                log.info("스크리닝 실패 -> 이전 유니버스로 대체: %d종목", len(items))
            if not self.universe:
                self.notifier.error(
                    "감시 유니버스를 만들지 못했습니다 — 오늘은 신규 진입을 하지 않습니다\n"
                    f"원인: {reason or '알 수 없음 (로그를 확인하세요)'}"
                )

        # 보유 종목은 반드시 감시해야 청산이 돈다.
        for code in self.risk.positions:
            if code not in self.universe:
                self.universe.append(code)

        if not self.universe:
            return

        lines = "\n".join(
            f"  {i+1:2d}. {it.get('name', it['code'])}({it['code']})  "
            f"거래대금 {it.get('avg_value_20d', 0.0)/1e8:,.0f}억  "
            f"거래량급증 {it.get('vol_surge', 0.0):.2f}x"
            for i, it in enumerate(display_items[:15])
        )
        self.notifier.info(f"🔎 감시 유니버스 {len(self.universe)}종목\n{lines}")

        await self.warmup_bars()
        await self.ws.subscribe(self.universe)
        log.info("--- 장 전 준비 완료: %d종목 ---", len(self.universe))

    async def warmup_bars(self) -> None:
        """ka10080 으로 과거 1분봉을 채운다. 이후로는 웹소켓 틱만으로 봉을 잇는다."""
        self.bars.keep_only(self.universe)
        ok = 0
        for code in self.universe:
            try:
                rows = await asyncio.to_thread(
                    self.client.get_minute_chart, code, 1, cfg.WARMUP_BARS
                )
            except KiwoomAPIError as exc:
                log.warning("분봉 워밍업 실패 %s: %s", code, exc.msg)
                continue
            if rows:
                self.bars.get(code).warmup(rows)
                ok += 1
        log.info("분봉 워밍업 완료: %d/%d 종목", ok, len(self.universe))

    async def session_open(self) -> None:
        if not self.universe:
            await self.morning_job()
        self.session_active = True
        self.risk.halt_new_entry = False
        self._session_opened_at = datetime.now()
        self._factor_a_seen = False
        self._factor_a_alerted = False
        await self.sync_account(mark_day_start=self.risk.day_start_equity <= 0)
        log.info("=== 장중 세션 시작 ===")
        self.notifier.info(f"▶️ 장중 세션 시작 — 감시 {len(self.universe)}종목")

    async def halt_entry_job(self) -> None:
        self.risk.halt_new_entry = True
        log.info("신규 진입 중단 시각 도달(%s)", cfg.NO_NEW_ENTRY_AFTER)
        self.notifier.info(f"⏸ {cfg.NO_NEW_ENTRY_AFTER:%H:%M} — 신규 진입 중단, 청산 감시만 계속합니다")

    # ================================================================ 실시간
    def on_tick(self, code: str, values: dict[str, str]) -> None:
        """실시간 체결(0B) 콜백. 봉이 확정되면 시그널을 평가한다."""
        if not code:
            return
        hhmmss = str(values.get("20", "")).strip()
        try:
            ts = datetime.combine(
                date.today(), datetime.strptime(hhmmss, "%H%M%S").time()
            ) if len(hhmmss) == 6 else datetime.now()
        except ValueError:
            ts = datetime.now()

        # 키움 실시간 WebSocket 0B 의 14번 필드(누적거래대금)는 공식 스펙상 예외 없이
        # '백만원' 단위로 수신된다 -> 원 단위로 환산(* 1_000_000).
        # (과거에 "이미 원 단위로 오면 이중 곱셈 방지"라는 크기 기반 휴리스틱이 있었으나,
        #  스펙에 없는 조건이라 오히려 위험했다 — 진짜 원 단위 데이터가 들어오면 조용히
        #  100만 배로 부풀려 Factor A 를 항상 통과시켜 버린다.)
        cum_turnover = parse_price(values.get("14")) * 1_000_000

        series = self.bars.get(code)
        closed = series.on_tick(
            ts=ts,
            price=parse_price(values.get("10")),
            cum_volume=parse_int(values.get("13")),
            tick_volume=parse_int(values.get("15")),
            cum_turnover=cum_turnover,
            strength=parse_price(values.get("228")),
            open_=parse_price(values.get("16")),
            high=parse_price(values.get("17")),
            low=parse_price(values.get("18")),
            ask=parse_price(values.get("27")),
            bid=parse_price(values.get("28")),
        )
        if closed is not None and self.session_active:
            asyncio.create_task(self.evaluate(code))

    async def evaluate(self, code: str) -> None:
        """봉 확정 시점의 시그널 평가 -> 메타 필터 -> 리스크 점검 -> 발주."""
        try:
            series = self.bars.get(code)
            sig = self.engine.evaluate(series)
            if sig.factors.get("A_turnover", 0.0) > 0:
                self._factor_a_seen = True
            if not sig.is_buy:
                if sig.score >= 3.0:
                    name = self.names.get(code, code)
                    log.info("👀 시그널 근접 %s(%s) | %s", name, code, sig.reason)
                else:
                    log.debug("시그널 평가 %s: %s", code, sig.reason)
                return

            # 아래로는 await 지점이 여러 개 있다. 같은 종목의 다음 봉이 그 사이에
            # 확정되어 중복 주문이 나가지 않도록 여기서 자리를 선점한다.
            can, why = self.risk.reserve_slot(code, sig.price)
            if not can:
                log.debug("시그널 무시 %s: %s", code, why)
                return

            reserved = True
            try:
                decision = self.meta.decide(sig.features)
                signal_id = await asyncio.to_thread(
                    self.db.log_signal, sig,
                    name=self.names.get(code, ""),
                    meta_prob=decision.probability,
                    meta_approved=decision.approved,
                )
                if not decision.approved:
                    log.info("메타 필터 거절 %s: %s", code, decision.reason)
                    return

                atr = self.engine.atr_of(series)
                stats = await asyncio.to_thread(self.db.rolling_stats, 30)
                qty, amount, note = self.risk.calc_qty(
                    sig.price, atr=atr, win_rate=stats.get("win_rate"), payoff=stats.get("payoff")
                )
                if qty <= 0:
                    log.info("사이징 결과 진입 불가 %s: %s", code, note)
                    return

                log.info("🟢 매수 시그널 %s %s | %s | %s | 메타 %s",
                         code, self.names.get(code, ""), sig.reason, note, decision.reason)

                po = await asyncio.to_thread(
                    self.executor.submit_buy,
                    code, self.names.get(code, code), qty, sig.price, series.last_close,
                    signal_id=signal_id, reason=sig.reason,
                )
                if po is not None:
                    reserved = False   # 선점한 자리를 주문이 이어받았다
            finally:
                if reserved:
                    self.risk.clear_pending_buy(code)
        except Exception:
            log.exception("시그널 평가 중 예외 (%s)", code)

    # ================================================================ 루프
    async def exit_loop(self) -> None:
        """1초 주기 청산 감시 + 킬스위치."""
        while not self._stop.is_set():
            try:
                if self.session_active:
                    await self._check_exits()
            except Exception:
                log.exception("청산 감시 루프 예외")
            await asyncio.sleep(1.0)

    async def _check_exits(self) -> None:
        prices = {
            code: self.bars.get(code).last_close
            for code in list(self.risk.positions)
            if self.bars.has(code)
        }
        self.risk.mark_to_market(prices)

        if self.risk.check_kill_switch():
            await self.kill_switch_liquidate()
            return

        for pos in list(self.risk.positions.values()):
            price = prices.get(pos.code) or pos.avg_price
            order = self.risk.check_exit(pos, price)
            if order is None:
                continue
            log.info("🔴 청산 시그널 %s: %s", pos.code, order.reason)
            await asyncio.to_thread(self.executor.submit_exit, order, price)

    async def housekeeping_loop(self) -> None:
        """5초: 미체결 스윕 / 30초: 계좌 대사 / 60초: 시세 끊김 점검."""
        tick = 0
        while not self._stop.is_set():
            await asyncio.sleep(5.0)
            tick += 1
            try:
                if self.session_active:
                    await asyncio.to_thread(self.executor.sweep_unfilled, self._price_of)
                if tick % 6 == 0:
                    await self.sync_account()
                    await asyncio.to_thread(self.executor.reconcile)
                if tick % 12 == 0 and self.session_active:
                    self._check_feed_health()
            except Exception:
                log.exception("하우스키핑 루프 예외")

    def _price_of(self, code: str) -> float:
        if self.bars.has(code):
            return self.bars.get(code).last_close
        try:
            return self.client.get_quote(code)["price"]
        except KiwoomAPIError:
            return 0.0

    def _check_feed_health(self) -> None:
        stale = self.bars.stale_codes(datetime.now(), timedelta(minutes=5))
        held_stale = [c for c in stale if c in self.risk.positions]
        if held_stale:
            self.notifier.warn(
                f"보유 종목 실시간 시세 5분 이상 끊김: {', '.join(held_stale)} — 재구독합니다"
            )
            asyncio.create_task(self.ws.subscribe(self.universe))
        self._check_factor_a_health()

    def _check_factor_a_health(self) -> None:
        """
        Factor A 무응답 워치독. SIGNAL_SCORE_THRESHOLD 가 Factor A 없이는 도달 불가능한
        설계(기본값 기준 사실이다)이므로, 세션 시작 후 한참이 지나도 감시 유니버스
        전체에서 A 를 한 번도 못 얻었다면 매매가 조용히 멈춰 있다는 신호다.
        prev_turnover 기준선 유실, 단위 환산 회귀 등 이번에 실제로 있었던 장애를
        당일 안에 잡기 위한 자체 무결성 점검.
        """
        if cfg.SIGNAL_SCORE_THRESHOLD <= cfg.FACTOR_MAX_SCORE_WITHOUT_A:
            return  # 이 설정에선 A 없이도 진입 가능 -> 워치독 의미 없음
        if (self._factor_a_alerted or self._factor_a_seen
                or not self.universe or self._session_opened_at is None):
            return
        if datetime.now() - self._session_opened_at < timedelta(minutes=30):
            return
        self._factor_a_alerted = True
        log.error("Factor A 무응답: 세션 시작 30분 경과, 감시 %d종목 중 A 획득 0건", len(self.universe))
        self.notifier.error(
            "⚠️ 세션 시작 30분이 지나도록 어떤 종목도 Factor A(거래대금 유입) 점수를 "
            "얻지 못했습니다. 이 설정에서는 A 없이 매수 기준을 넘을 수 없어 매매가 조용히 "
            "중단된 상태일 수 있습니다. prev_turnover 기준선(스크리닝/유니버스 폴백)과 "
            "웹소켓 14번 필드 단위 환산을 확인하세요."
        )

    async def sync_account(self, mark_day_start: bool = False) -> None:
        try:
            deposit = await asyncio.to_thread(self.client.get_deposit)
            balance = await asyncio.to_thread(self.client.get_balance)
        except KiwoomAPIError as exc:
            log.warning("계좌 조회 실패: %s", exc)
            return
        self.risk.sync(deposit, balance, mark_day_start=mark_day_start)
        self._last_sync = datetime.now()

    # ================================================================ 청산/마감
    async def kill_switch_liquidate(self) -> None:
        orders = self.risk.flatten_all(f"킬스위치: {self.risk.kill_reason}")
        self.session_active = False
        self.risk.halt_new_entry = True
        await asyncio.to_thread(self.executor.cancel_all)
        for order in orders:
            await asyncio.to_thread(self.executor.submit_exit, order, self._price_of(order.code))
        self.notifier.error(
            f"킬스위치 발동 — {self.risk.kill_reason}\n"
            f"보유 {len(orders)}종목 전량 시장가 청산, 당일 매매를 중단합니다."
        )

    async def flatten_job(self) -> None:
        """장 마감 전 일괄 청산 (오버나잇 리스크 회피)."""
        if not self.risk.positions and not self.executor.has_pending():
            log.info("일괄 청산 대상 없음")
            return
        log.info("=== %s 일괄 청산 ===", cfg.FLATTEN_TIME)
        await asyncio.to_thread(self.executor.cancel_all)
        orders = self.risk.flatten_all("장 마감 일괄청산")
        for order in orders:
            await asyncio.to_thread(self.executor.submit_exit, order, self._price_of(order.code))
        if orders:
            self.notifier.info(f"🔚 일괄 청산 {len(orders)}종목 시장가 매도")

    async def session_close(self) -> None:
        self.session_active = False
        log.info("=== 장중 세션 종료 ===")

        # 형성 중이던 봉을 확정하고, 라벨링용으로 오늘 봉을 DB 에 남긴다.
        for code in self.bars.codes():
            series = self.bars.get(code)
            series.force_close()
            df = series.to_frame()
            if df.empty:
                continue
            today = date.today()
            rows = [
                {
                    "time": ts.to_pydatetime(),
                    "open": float(r.open),
                    "high": float(r.high),
                    "low": float(r.low),
                    "close": float(r.close),
                    "volume": int(r.volume),
                }
                for ts, r in zip(df.index, df.itertuples(index=False))
                if ts.date() == today
            ]
            if rows:
                await asyncio.to_thread(self.db.save_bars, code, rows)

        try:
            await self.ws.subscribe([])
        except Exception:
            log.debug("구독 해제 실패(무시)")

        await self.sync_account()
        if self.risk.positions:
            held = ", ".join(f"{p.name}({p.code}) {p.qty}주" for p in self.risk.positions.values())
            self.notifier.warn(f"장 마감 후에도 보유 중인 종목이 있습니다: {held}")

    async def eod_job(self) -> None:
        await self.sync_account()
        stats = await asyncio.to_thread(self.db.today_stats)
        await asyncio.to_thread(
            self.db.save_daily,
            start_equity=self.risk.day_start_equity,
            end_equity=self.risk.total_equity,
            trades=stats["trades"],
            wins=stats["wins"],
            kill_switch=self.risk.kill_switch,
            note=self.risk.kill_reason,
        )
        report = fmt_daily_report(
            equity=self.risk.total_equity,
            pnl_pct=self.risk.daily_pnl_pct(),
            trades=stats["trades"],
            wins=stats["wins"],
            realized=stats["realized"],
            positions=len(self.risk.positions),
        )
        es = self.executor.stats
        report += (
            f"\n  주문 매수 {es.buy_orders}/체결 {es.buy_fills} · "
            f"매도 {es.sell_orders}/체결 {es.sell_fills}\n"
            f"  취소 {es.cancels} · 거부 {es.rejects} · 슬리피지차단 {es.slippage_blocks}"
        )
        if es.errors:
            report += "\n  ⚠️ " + " / ".join(es.errors[-5:])
        self.notifier.info(report)
        self.executor.stats = type(es)()

    # ================================================================ 종료
    def request_stop(self) -> None:
        log.info("종료 신호 수신")
        self._stop.set()

    async def shutdown(self) -> None:
        log.info("종료 처리 시작")
        self.session_active = False
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        try:
            await self.ws.stop()
        except Exception:
            pass
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

        if self.risk.positions:
            held = ", ".join(f"{p.code} {p.qty}주" for p in self.risk.positions.values())
            self.notifier.warn(f"봇 종료 — 보유 포지션이 남아 있습니다: {held}")
        else:
            self.notifier.info("🛑 봇 정상 종료")
        self.db.close()
        log.info("종료 완료")


# ==================================================================== 엔트리
async def run_check() -> None:
    """접속·계좌 점검만 하고 종료."""
    errors = cfg.validate()
    for e in errors:
        log.error("설정 오류: %s", e)
    if errors:
        raise SystemExit(1)

    client = KiwoomClient()
    tok = await asyncio.to_thread(client.issue_token)
    log.info("토큰 발급 OK (만료 %s)", tok.expires_dt)

    deposit = await asyncio.to_thread(client.get_deposit)
    balance = await asyncio.to_thread(client.get_balance)
    log.info("예수금 %s원 / 주문가능 %s원",
             f"{deposit['entr']:,.0f}", f"{deposit['ord_alow_amt']:,.0f}")
    log.info("평가금액 %s원 (손익 %s원 / %+.2f%%)",
             f"{balance['total_eval']:,.0f}", f"{balance['total_pnl']:+,.0f}",
             balance["total_pnl_pct"])
    for h in balance["holdings"]:
        log.info("  보유 %s %s %d주 @%s -> %s (%+.2f%%)",
                 h["code"], h["name"], h["qty"],
                 f"{h['avg_price']:,.0f}", f"{h['cur_price']:,.0f}", h["pnl_pct"])

    quote = await asyncio.to_thread(client.get_quote, "005930")
    log.info("삼성전자 현재가 %s원 (매도호가 %s / 매수호가 %s)",
             f"{quote['price']:,.0f}", f"{quote['ask']:,.0f}", f"{quote['bid']:,.0f}")

    bars = await asyncio.to_thread(client.get_minute_chart, "005930", 1, 5)
    for b in bars:
        log.info("  분봉 %s O%.0f H%.0f L%.0f C%.0f V%d",
                 b["time"], b["open"], b["high"], b["low"], b["close"], b["volume"])
    log.info("점검 완료 — 이상 없음")


async def run_screen() -> None:
    client = KiwoomClient()
    await asyncio.to_thread(client.issue_token)
    candidates = await asyncio.to_thread(screener.screen, client, cfg.UNIVERSE_MAX)
    await asyncio.to_thread(screener.save_universe, candidates)
    print(f"\n{'순위':>4} {'종목':<20} {'코드':<8} {'종가':>10} {'20일거래대금':>14} {'급증':>6}")
    print("-" * 70)
    for i, c in enumerate(candidates, 1):
        print(f"{i:>4} {c.name:<20} {c.code:<8} {c.close:>10,.0f} "
              f"{c.avg_value_20d/1e8:>12,.0f}억 {c.vol_surge:>5.2f}x")
    print(f"\n{len(candidates)}종목 선정 -> {screener.UNIVERSE_PATH}")


async def run_bot(run_now: bool) -> None:
    bot = TradingBot()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, bot.request_stop)
        except NotImplementedError:
            # Windows 는 add_signal_handler 를 지원하지 않는다.
            signal.signal(sig, lambda *_: bot.request_stop())
    try:
        await bot.start(run_now=run_now)
    except asyncio.CancelledError:
        await bot.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="키움 REST API 자동매매 봇")
    parser.add_argument("--now", action="store_true", help="스케줄을 기다리지 않고 즉시 세션 시작")
    parser.add_argument("--screen", action="store_true", help="장 전 스크리닝만 실행")
    parser.add_argument("--check", action="store_true", help="접속/계좌 점검만 실행")
    args = parser.parse_args()

    setup_logging()
    try:
        if args.check:
            asyncio.run(run_check())
        elif args.screen:
            asyncio.run(run_screen())
        else:
            asyncio.run(run_bot(args.now))
    except KeyboardInterrupt:
        log.info("사용자 중단")


if __name__ == "__main__":
    main()
