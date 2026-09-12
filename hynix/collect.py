"""Phase 0 — 데이터 수집기. 매매는 하지 않는다.

  ka10081  국내 일봉 OHLCV + 거래대금
  ka10060  투자자별 순매수 (금액 백만원 / 수량 주)
  ka10008  외국인 보유주식수·지분율 추이
  yfinance SKHY / SOX / MU / NVDA / USDKRW / 지수선물

부호 처리 규칙 — 이 둘을 섞으면 조용히 틀린다(2026-09-12 실제로 밟음):
  가격 필드 -> parse_price.  키움은 '전일 대비 방향'을 가격 앞 부호로 실어 보낸다.
    ka10080(분봉)은 하락봉의 open/high/low/close 가 전부 음수로 온다(실측 43%).
    ka10081(일봉)은 부호를 안 붙이지만, 규칙을 통일해 두는 편이 안전하다.
  수급·변동량 필드 -> parse_signed. 여기서는 부호가 순매수/순매도 그 자체다.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from trading_bot.core.kiwoom_client import (KiwoomClient, parse_int, parse_price,
                                            parse_signed)

from . import store
from .config import EXT_TICKERS, FLOW_REQUEST_DELAY_SEC, KR_CODES, MAX_PAGES, TARGET

log = logging.getLogger(__name__)


def _pages(client: KiwoomClient, api_id: str, path: str, body: dict, list_key: str,
           max_pages: int = MAX_PAGES, delay: float = FLOW_REQUEST_DELAY_SEC) -> list[dict]:
    """연속조회. 키움이 429 를 자주 뱉는 TR 들이라 페이지 사이를 넉넉히 쉰다."""
    rows: list[dict] = []
    cont_yn, next_key = "N", ""
    for _page in range(max_pages):
        data, cont_yn, next_key = client.request(api_id, path, body, cont_yn, next_key)
        chunk = data.get(list_key) or []
        rows.extend(chunk)
        if cont_yn != "Y" or not next_key or not chunk:
            break
        time.sleep(delay)
    return rows


# ---------------------------------------------------------------- 국내 일봉
def collect_kr_daily(client: KiwoomClient, code: str) -> int:
    """ka10081 일봉.

    trading_bot 쪽 get_daily_chart 는 종가/거래량만 담아서 OHLC 가 없다 —
    레짐 분류와 갭 분석에 시가·고가·저가가 필수라 여기서 따로 받는다.
    """
    rows = _pages(client, "ka10081", "/api/dostk/chart",
                  {"stk_cd": code, "base_dt": time.strftime("%Y%m%d"), "upd_stkpc_tp": "1"},
                  "stk_dt_pole_chart_qry")
    out = []
    for r in rows:
        dt = (r.get("dt") or "").strip()
        if len(dt) != 8:
            continue
        out.append({
            "code": code, "dt": dt,
            "open": parse_price(r.get("open_pric")),
            "high": parse_price(r.get("high_pric")),
            "low": parse_price(r.get("low_pric")),
            "close": parse_price(r.get("cur_prc")),
            "volume": parse_int(r.get("trde_qty")),
            # trde_prica 는 백만원 단위로 온다 (trading_bot 쪽과 동일 취급)
            "value": parse_int(r.get("trde_prica")) * 1_000_000,
        })
    cols = ["code", "dt", "open", "high", "low", "close", "volume", "value"]
    n = store.upsert("kr_daily", cols, out)
    if out:
        dts = sorted(x["dt"] for x in out)
        store.log_run("ka10081", code, n, dts[0], dts[-1])
        log.info("kr_daily   %s %d행 %s~%s", code, n, dts[0], dts[-1])
    return n


# ---------------------------------------------------------------- 분봉
def collect_kr_min(client: KiwoomClient, code: str, tic: int = 5,
                   max_pages: int = 60) -> int:
    """ka10080 분봉. 연속조회로 받을 수 있는 데까지 거슬러 올라간다.

    실측(2026-09-12): 15페이지에 1분봉 36거래일 / 5분봉 174거래일. 페이지당 900행이라
    granularity 를 키울수록 같은 호출수로 더 긴 기간을 덮는다. 장중 가설 검증에는
    5분봉이 현실적인 절충점이다 — 1분봉은 7주치뿐이라 표본이 안 된다.
    """
    rows = _pages(client, "ka10080", "/api/dostk/chart",
                  {"stk_cd": code, "tic_scope": str(tic), "upd_stkpc_tp": "1"},
                  "stk_min_pole_chart_qry", max_pages=max_pages)
    out = []
    for r in rows:
        ts = (r.get("cntr_tm") or "").strip()
        if len(ts) != 14:
            continue
        out.append({
            "code": code, "tic": tic, "ts": ts,
            "open": parse_price(r.get("open_pric")),
            "high": parse_price(r.get("high_pric")),
            "low": parse_price(r.get("low_pric")),
            "close": parse_price(r.get("cur_prc")),
            "volume": parse_int(r.get("trde_qty")),
        })
    cols = ["code", "tic", "ts", "open", "high", "low", "close", "volume"]
    n = store.upsert("kr_min", cols, out)
    if out:
        tss = sorted(x["ts"] for x in out)
        days = len({t[:8] for t in tss})
        store.log_run("ka10080", f"{code}/{tic}분", n, tss[0], tss[-1], f"{days}거래일")
        log.info("kr_min     %s %d분봉 %d행 %s~%s (%d거래일)",
                 code, tic, n, tss[0][:8], tss[-1][:8], days)
    return n


# ---------------------------------------------------------------- 투자자별 수급
_FLOW_AMT = {"frgnr_amt": "frgnr_invsr", "orgn_amt": "orgn", "ind_amt": "ind_invsr",
             "fnnc_invt_amt": "fnnc_invt", "invtrt_amt": "invtrt",
             "penfnd_amt": "penfnd_etc", "etc_corp_amt": "etc_corp"}
_FLOW_QTY = {"frgnr_qty": "frgnr_invsr", "orgn_qty": "orgn", "ind_qty": "ind_invsr"}


def collect_kr_flow(client: KiwoomClient, code: str) -> int:
    """ka10060 을 금액(백만원)·수량(주) 두 번 받아 한 행으로 합친다.

    같은 TR 이 amt_qty_tp 로 단위만 바꿔 돌아오는데 어느 쪽인지 응답에 표시가 없다.
    둘 다 저장해두면 나중에 단위를 헷갈릴 여지가 없어진다.
    """
    def fetch(tp: str) -> dict[str, dict]:
        rows = _pages(client, "ka10060", "/api/dostk/chart",
                      {"dt": time.strftime("%Y%m%d"), "stk_cd": code,
                       "amt_qty_tp": tp, "trde_tp": "0", "unit_tp": "1"},
                      "stk_invsr_orgn_chart")
        return {(r.get("dt") or "").strip(): r for r in rows if len(r.get("dt") or "") == 8}

    amt = fetch("1")
    time.sleep(FLOW_REQUEST_DELAY_SEC)
    qty = fetch("2")

    out = []
    for dt, r in amt.items():
        rec: dict[str, Any] = {
            "code": code, "dt": dt,
            "close": parse_price(r.get("cur_prc")),
            # acc_trde_prica 는 이름과 달리 거래량(주)이다. 거래대금은 kr_daily.value 를 쓸 것.
            "volume": parse_int(r.get("acc_trde_prica")),
        }
        for col, src in _FLOW_AMT.items():
            rec[col] = int(parse_signed(r.get(src)))
        q = qty.get(dt)
        for col, src in _FLOW_QTY.items():
            rec[col] = int(parse_signed(q.get(src))) if q else None
        out.append(rec)

    cols = ["code", "dt", "close", "volume"] + list(_FLOW_AMT) + list(_FLOW_QTY)
    n = store.upsert("kr_flow", cols, out)
    if out:
        dts = sorted(x["dt"] for x in out)
        store.log_run("ka10060", code, n, dts[0], dts[-1])
        log.info("kr_flow    %s %d행 %s~%s", code, n, dts[0], dts[-1])
    return n


# ---------------------------------------------------------------- 외국인 보유
def collect_kr_foreign(client: KiwoomClient, code: str) -> int:
    """ka10008 외국인 보유주식수·지분율 추이."""
    rows = _pages(client, "ka10008", "/api/dostk/frgnistt", {"stk_cd": code}, "stk_frgnr")
    out = []
    for r in rows:
        dt = (r.get("dt") or "").strip()
        if len(dt) != 8:
            continue
        out.append({
            "code": code, "dt": dt,
            "close": parse_price(r.get("close_pric")),
            "volume": parse_int(r.get("trde_qty")),
            "chg_qty": int(parse_signed(r.get("chg_qty"))),
            "poss_stkcnt": parse_int(r.get("poss_stkcnt")),
            "wght": parse_signed(r.get("wght")),
            "limit_exh_rt": parse_signed(r.get("limit_exh_rt")),
        })
    cols = ["code", "dt", "close", "volume", "chg_qty", "poss_stkcnt", "wght", "limit_exh_rt"]
    n = store.upsert("kr_foreign", cols, out)
    if out:
        dts = sorted(x["dt"] for x in out)
        store.log_run("ka10008", code, n, dts[0], dts[-1])
        log.info("kr_foreign %s %d행 %s~%s", code, n, dts[0], dts[-1])
    return n


# ---------------------------------------------------------------- 외부 일봉
def collect_external(period: str = "max") -> int:
    import yfinance as yf

    total = 0
    for ticker in EXT_TICKERS:
        try:
            d = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
        except Exception as exc:                      # 네트워크/심볼 변경 등
            log.warning("ext_daily %s 실패: %s", ticker, exc)
            store.log_run("yfinance", ticker, 0, note=f"실패: {exc}")
            continue
        if d.empty:
            log.warning("ext_daily %s 응답 없음", ticker)
            store.log_run("yfinance", ticker, 0, note="빈 응답")
            continue
        if d.index.tz is not None:
            d.index = d.index.tz_localize(None)
        out = [{
            "ticker": ticker, "dt": ts.strftime("%Y%m%d"),
            "open": float(r["Open"]), "high": float(r["High"]),
            "low": float(r["Low"]), "close": float(r["Close"]),
            # 지수·환율은 거래량이 없어 NaN 으로 온다
            "volume": int(r["Volume"]) if r["Volume"] == r["Volume"] else 0,
        } for ts, r in d.iterrows() if r["Close"] == r["Close"]]
        cols = ["ticker", "dt", "open", "high", "low", "close", "volume"]
        n = store.upsert("ext_daily", cols, out)
        dts = sorted(x["dt"] for x in out)
        store.log_run("yfinance", ticker, n, dts[0], dts[-1])
        log.info("ext_daily  %-10s %d행 %s~%s", ticker, n, dts[0], dts[-1])
        total += n
    return total


# ---------------------------------------------------------------- 엔트리포인트
def backfill(kiwoom: bool = True, external: bool = True) -> None:
    store.init()
    if external:
        collect_external()
    if kiwoom:
        client = KiwoomClient()
        for code in KR_CODES:
            collect_kr_daily(client, code)
            time.sleep(FLOW_REQUEST_DELAY_SEC)
            collect_kr_flow(client, code)
            time.sleep(FLOW_REQUEST_DELAY_SEC)
            # 수급 상세와 분봉은 매매 대상에만 필요하다. 대조군까지 받으면 429 만 늘어난다.
            if code == TARGET:
                collect_kr_foreign(client, code)
                time.sleep(FLOW_REQUEST_DELAY_SEC)
                # 5분봉이 1년, 1분봉이 약 3개월 확보된다(연속조회 한계).
                for tic, pages in ((5, 80), (1, 30)):
                    collect_kr_min(client, code, tic=tic, max_pages=pages)
                    time.sleep(FLOW_REQUEST_DELAY_SEC)

    problems = store.validate()
    if problems:
        for p in problems:
            log.warning("수집 데이터 검증 실패: %s", p)
    else:
        log.info("수집 데이터 검증 통과")
