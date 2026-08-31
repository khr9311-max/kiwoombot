"""
키움증권 REST API 래퍼.

문서 근거 (키움 REST API 문서):
  au10001  POST /oauth2/token       접근토큰 발급
  au10002  POST /oauth2/revoke      접근토큰 폐기
  kt00001  POST /api/dostk/acnt     예수금상세현황요청
  kt00018  POST /api/dostk/acnt     계좌평가잔고내역요청
  ka10075  POST /api/dostk/acnt     미체결요청
  ka10080  POST /api/dostk/chart    주식분봉차트조회요청
  kt10000  POST /api/dostk/ordr     주식 매수주문
  kt10001  POST /api/dostk/ordr     주식 매도주문
  kt10002  POST /api/dostk/ordr     주식 정정주문
  kt10003  POST /api/dostk/ordr     주식 취소주문

주의: 키움 응답의 가격 필드는 부호 접두어가 붙어 온다(예: "-78800" 은 하락 표시이지
      음수 가격이 아니다). 반드시 parse_price() 로 파싱할 것.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

import requests

from ..config import settings as cfg

log = logging.getLogger(__name__)


class KiwoomAPIError(RuntimeError):
    """return_code != 0 이거나 HTTP 오류."""

    def __init__(self, api_id: str, code: Any, msg: str, payload: dict | None = None):
        self.api_id = api_id
        self.code = code
        self.msg = msg
        self.payload = payload
        super().__init__(f"[{api_id}] return_code={code} msg={msg}")


# --------------------------------------------------------------- 응답 코드
# 키움 공식 샘플(official_reference/kiwoom/core/errors.py) 기준.
#
# 중요: 키움은 업무 오류를 HTTP 200 + 본문 return_code 로 내려보낸다.
# 토큰 만료도 401 이 아니라 return_code=8005, 또는 일반 코드(3)와 함께
# return_msg 에 "[8005:Token이 유효하지 않습니다]" 형태로 끼워 온다.
# HTTP 상태코드만 보면 토큰 만료를 영영 못 잡는다.
_EMBEDDED_CODE_RE = re.compile(r"\[(\d{3,5}):|CODE=(\d{3,5})")

# 토큰을 재발급하고 한 번 재시도할 코드
AUTH_RETRY_CODES = frozenset({8003, 8005, 8006, 8009, 8015, 8016, 8031, 8103})
# 잠시 쉬었다 재시도할 코드 (유량 제한)
RATE_LIMIT_CODES = frozenset({1700, 1701, 1702})


def normalize_return_code(value: Any) -> int | None:
    """
    return_code 를 int 로. 키움은 엔드포인트에 따라 0, "0", "0000", "8005" 처럼
    int 와 숫자 문자열을 섞어 보낸다. 문자열 "0" 을 오류로 오인하면 안 된다.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text or not text.lstrip("-").isdigit():
        return None
    return int(text)


def embedded_return_code(return_msg: Any) -> int | None:
    """return_msg 안에 끼워 온 구체 코드를 꺼낸다. 예) '[8005:...]' / 'CODE=8005'"""
    match = _EMBEDDED_CODE_RE.search(str(return_msg or ""))
    if match is None:
        return None
    return int(match.group(1) or match.group(2))


def _effective_code(return_code: Any, return_msg: Any) -> int | None:
    """최상위 코드가 일반 코드(3 등)면 메시지에 끼워 온 구체 코드를 우선한다."""
    code = normalize_return_code(return_code)
    if code in AUTH_RETRY_CODES or code in RATE_LIMIT_CODES:
        return code
    embedded = embedded_return_code(return_msg)
    if embedded in AUTH_RETRY_CODES or embedded in RATE_LIMIT_CODES:
        return embedded
    return code


# --------------------------------------------------------------------- 파싱
def parse_price(value: Any) -> float:
    """'-78800' / '+21150' / '000000059000' -> 78800.0 / 21150.0 / 59000.0"""
    if value is None:
        return 0.0
    s = str(value).strip().replace(",", "")
    if not s:
        return 0.0
    s = s.lstrip("+-")
    if not s:
        return 0.0
    try:
        return abs(float(s))
    except ValueError:
        return 0.0


def parse_int(value: Any) -> int:
    return int(parse_price(value))


def parse_signed(value: Any) -> float:
    """등락률처럼 실제 부호가 의미 있는 필드용."""
    if value is None:
        return 0.0
    s = str(value).strip().replace(",", "").replace("%", "")
    if not s or s in ("+", "-"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def norm_code(code: str) -> str:
    """'A005930' / '005930_AL' -> '005930'"""
    c = str(code).strip().upper()
    for suffix in ("_NX", "_AL"):
        if c.endswith(suffix):
            c = c[: -len(suffix)]
    if len(c) == 7 and c[0].isalpha():
        c = c[1:]
    return c


# --------------------------------------------------------------- 레이트리밋
class TokenBucket:
    """초당 호출 제한 방어. 스레드 안전."""

    def __init__(self, rate_per_sec: float, burst: int):
        self._rate = max(rate_per_sec, 0.1)
        self._capacity = max(burst, 1)
        self._tokens = float(self._capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            time.sleep(min(wait, 0.5))


# --------------------------------------------------------------------- 토큰
@dataclass
class AccessToken:
    token: str
    token_type: str
    expires_dt: str  # YYYYMMDDHHMMSS

    @property
    def expires_at(self) -> datetime:
        try:
            return datetime.strptime(self.expires_dt, "%Y%m%d%H%M%S")
        except ValueError:
            return datetime.min

    def is_valid(self, margin_sec: int = 600) -> bool:
        if not self.token:
            return False
        return (self.expires_at - datetime.now()).total_seconds() > margin_sec

    def to_dict(self) -> dict:
        return {"token": self.token, "token_type": self.token_type, "expires_dt": self.expires_dt}


# -------------------------------------------------------------------- 클라이언트
class KiwoomClient:
    def __init__(
        self,
        app_key: str = cfg.APP_KEY,
        app_secret: str = cfg.APP_SECRET,
        host: str = cfg.REST_HOST,
        dry_run: bool = cfg.DRY_RUN,
    ):
        self.app_key = app_key
        self.app_secret = app_secret
        self.host = host.rstrip("/")
        self.dry_run = dry_run
        self._session = requests.Session()
        self._bucket = TokenBucket(cfg.REST_RATE_PER_SEC, cfg.REST_BURST)
        self._token: AccessToken | None = None
        self._token_lock = threading.Lock()
        self._dry_order_seq = 0

    # ------------------------------------------------------------ 인증
    def _load_cached_token(self) -> AccessToken | None:
        try:
            raw = json.loads(cfg.TOKEN_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if raw.get("host") != self.host:
            return None
        tok = AccessToken(raw.get("token", ""), raw.get("token_type", "bearer"), raw.get("expires_dt", ""))
        return tok if tok.is_valid() else None

    def _save_cached_token(self, tok: AccessToken) -> None:
        try:
            payload = tok.to_dict() | {"host": self.host}
            cfg.TOKEN_CACHE.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:
            log.warning("토큰 캐시 저장 실패: %s", exc)

    def issue_token(self, force: bool = False) -> AccessToken:
        """au10001. 캐시가 유효하면 재사용한다."""
        with self._token_lock:
            if not force and self._token and self._token.is_valid():
                return self._token
            if not force:
                cached = self._load_cached_token()
                if cached:
                    self._token = cached
                    log.info("캐시된 접근토큰 재사용 (만료 %s)", cached.expires_dt)
                    return cached

            self._bucket.acquire()
            resp = self._session.post(
                f"{self.host}/oauth2/token",
                headers={"Content-Type": "application/json;charset=UTF-8"},
                json={
                    "grant_type": "client_credentials",
                    "appkey": self.app_key,
                    "secretkey": self.app_secret,
                },
                timeout=cfg.REST_TIMEOUT,
            )
            resp.raise_for_status()
            body = resp.json()
            code = normalize_return_code(body.get("return_code"))
            if code is not None and code != 0:
                raise KiwoomAPIError("au10001", code, body.get("return_msg", ""))
            tok = AccessToken(body["token"], body.get("token_type", "bearer"), body.get("expires_dt", ""))
            self._token = tok
            self._save_cached_token(tok)
            log.info("접근토큰 발급 완료 (만료 %s)", tok.expires_dt)
            return tok

    def revoke_token(self) -> None:
        """au10002."""
        if not self._token:
            return
        try:
            self._bucket.acquire()
            self._session.post(
                f"{self.host}/oauth2/revoke",
                headers={"Content-Type": "application/json;charset=UTF-8"},
                json={"appkey": self.app_key, "secretkey": self.app_secret, "token": self._token.token},
                timeout=cfg.REST_TIMEOUT,
            )
        except requests.RequestException as exc:
            log.warning("토큰 폐기 실패: %s", exc)
        finally:
            self._token = None

    @property
    def access_token(self) -> str:
        return self.issue_token().token

    # ------------------------------------------------------------ 공통 호출
    def request(
        self,
        api_id: str,
        path: str,
        body: dict,
        cont_yn: str = "N",
        next_key: str = "",
    ) -> tuple[dict, str, str]:
        """(응답본문, cont-yn, next-key) 를 반환."""
        url = f"{self.host}{path}"
        last_exc: Exception | None = None
        auth_refreshed = False   # 토큰 재발급은 요청당 한 번만
        attempt = 0

        while attempt < cfg.REST_MAX_RETRY:
            attempt += 1
            headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "authorization": f"Bearer {self.access_token}",
                "api-id": api_id,
                "cont-yn": cont_yn,
                "next-key": next_key,
            }
            self._bucket.acquire()
            try:
                resp = self._session.post(url, headers=headers, json=body, timeout=cfg.REST_TIMEOUT)
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("[%s] 네트워크 오류 (%d/%d): %s", api_id, attempt, cfg.REST_MAX_RETRY, exc)
                time.sleep(min(2 ** attempt * 0.3, 3.0))
                continue

            if resp.status_code == 401 and not auth_refreshed:
                log.warning("[%s] HTTP 401 -> 토큰 재발급", api_id)
                auth_refreshed = True
                self.issue_token(force=True)
                attempt -= 1        # 인증 재시도는 재시도 횟수로 세지 않는다
                continue
            if resp.status_code == 429:
                log.warning("[%s] HTTP 429 -> 백오프", api_id)
                time.sleep(min(2 ** attempt * 0.5, 5.0))
                continue
            if resp.status_code >= 500:
                last_exc = KiwoomAPIError(api_id, resp.status_code, resp.text[:200])
                time.sleep(min(2 ** attempt * 0.3, 3.0))
                continue

            try:
                data = resp.json()
            except ValueError as exc:
                last_exc = KiwoomAPIError(api_id, resp.status_code, f"JSON 아님: {resp.text[:200]}")
                if resp.status_code >= 400:
                    raise last_exc from exc
                time.sleep(0.3)
                continue

            msg = data.get("return_msg", "")
            code = _effective_code(data.get("return_code"), msg)

            # 키움은 토큰 만료를 HTTP 200 + 본문 코드로 알려준다. 여기서 잡지 않으면
            # 토큰 수명(24시간)이 끝나는 순간 봇이 통째로 멎는다.
            if code in AUTH_RETRY_CODES and not auth_refreshed:
                log.warning("[%s] 토큰 오류(return_code=%s, %s) -> 재발급 후 재시도", api_id, code, msg)
                auth_refreshed = True
                self.issue_token(force=True)
                attempt -= 1
                continue

            if code in RATE_LIMIT_CODES:
                wait = min(2 ** attempt * 0.5, 5.0)
                log.warning("[%s] 유량 제한(return_code=%s) -> %.1f초 대기", api_id, code, wait)
                last_exc = KiwoomAPIError(api_id, code, msg, body)
                time.sleep(wait)
                continue

            if resp.status_code >= 400 or (code is not None and code != 0):
                raise KiwoomAPIError(api_id, code, msg, body)

            return data, resp.headers.get("cont-yn", "N"), resp.headers.get("next-key", "")

        raise KiwoomAPIError(api_id, "RETRY_EXHAUSTED", str(last_exc), body)

    def request_all(
        self, api_id: str, path: str, body: dict, list_key: str, max_pages: int = 20
    ) -> Iterator[dict]:
        """cont-yn / next-key 연속조회를 모두 소진하며 리스트 항목을 yield."""
        cont_yn, next_key = "N", ""
        for _ in range(max_pages):
            data, cont_yn, next_key = self.request(api_id, path, body, cont_yn, next_key)
            yield from (data.get(list_key) or [])
            if cont_yn != "Y" or not next_key:
                return

    # ------------------------------------------------------------ 계좌
    def get_deposit(self) -> dict:
        """kt00001 예수금상세현황요청 -> {'entr':예수금, 'ord_alow_amt':주문가능금액, ...}"""
        data, _, _ = self.request("kt00001", "/api/dostk/acnt", {"qry_tp": "3"})
        return {
            "entr": parse_price(data.get("entr")),
            "ord_alow_amt": parse_price(data.get("ord_alow_amt")),
            "d2_entra": parse_price(data.get("d2_entra")),
            "stk_ord_alow_100": parse_price(data.get("100stk_ord_alow_amt")),
            "raw": data,
        }

    def get_balance(self) -> dict:
        """kt00018 계좌평가잔고내역요청 -> 총평가 + 종목별 보유내역."""
        body = {"qry_tp": "1", "dmst_stex_tp": cfg.DMST_STEX_TP}
        data, cont_yn, next_key = self.request("kt00018", "/api/dostk/acnt", body)
        rows = list(data.get("acnt_evlt_remn_indv_tot") or [])
        pages = 1
        while cont_yn == "Y" and next_key and pages < 20:
            data2, cont_yn, next_key = self.request("kt00018", "/api/dostk/acnt", body, cont_yn, next_key)
            rows.extend(data2.get("acnt_evlt_remn_indv_tot") or [])
            pages += 1

        holdings = [
            {
                "code": norm_code(r.get("stk_cd", "")),
                "name": (r.get("stk_nm") or "").strip(),
                "qty": parse_int(r.get("rmnd_qty")),
                "sellable_qty": parse_int(r.get("trde_able_qty")),
                "avg_price": parse_price(r.get("pur_pric")),
                "cur_price": parse_price(r.get("cur_prc")),
                "eval_amt": parse_price(r.get("evlt_amt")),
                "pnl": parse_signed(r.get("evltv_prft")),
                "pnl_pct": parse_signed(r.get("prft_rt")),
            }
            for r in rows
            if parse_int(r.get("rmnd_qty")) > 0
        ]
        return {
            "total_purchase": parse_price(data.get("tot_pur_amt")),
            "total_eval": parse_price(data.get("tot_evlt_amt")),
            "total_pnl": parse_signed(data.get("tot_evlt_pl")),
            "total_pnl_pct": parse_signed(data.get("tot_prft_rt")),
            "est_deposit_asset": parse_price(data.get("prsm_dpst_aset_amt")),
            "holdings": holdings,
        }

    def get_unfilled(self, code: str = "", all_stocks: bool = True, trade_type: str = "0") -> list[dict]:
        """ka10075 미체결요청. all_stk_tp 0=전체 1=종목, trde_tp 0=전체 1=매도 2=매수"""
        body = {
            "all_stk_tp": "0" if all_stocks else "1",
            "trde_tp": trade_type,
            "stex_tp": "0",
        }
        if code:  # 종목코드는 선택 파라미터 — 전체 조회 시엔 아예 보내지 않는다
            body["stk_cd"] = code
        rows = list(self.request_all("ka10075", "/api/dostk/acnt", body, "oso"))
        return [
            {
                "order_no": (r.get("ord_no") or "").strip(),
                "orig_order_no": (r.get("orig_ord_no") or "").strip(),
                "code": norm_code(r.get("stk_cd", "")),
                "name": (r.get("stk_nm") or "").strip(),
                "side": "SELL" if "매도" in (r.get("io_tp_nm") or "") else "BUY",
                "order_qty": parse_int(r.get("ord_qty")),
                "remain_qty": parse_int(r.get("oso_qty")),
                "order_price": parse_price(r.get("ord_pric")),
                "filled_qty": parse_int(r.get("cntr_qty")),
                "status": (r.get("ord_stt") or "").strip(),
                "time": (r.get("tm") or "").strip(),
                "raw": r,
            }
            for r in rows
        ]

    # ------------------------------------------------------------ 시세
    def get_minute_chart(self, code: str, tic: int = 1, count: int = 120, adjusted: bool = True) -> list[dict]:
        """ka10080 주식분봉차트조회. 과거->현재 오름차순 리스트로 반환."""
        body = {"stk_cd": code, "tic_scope": str(tic), "upd_stkpc_tp": "1" if adjusted else "0"}
        bars: list[dict] = []
        cont_yn, next_key = "N", ""
        for _ in range(10):
            data, cont_yn, next_key = self.request("ka10080", "/api/dostk/chart", body, cont_yn, next_key)
            rows = data.get("stk_min_pole_chart_qry") or []
            for r in rows:
                ts = (r.get("cntr_tm") or "").strip()
                if len(ts) != 14:
                    continue
                bars.append(
                    {
                        "time": datetime.strptime(ts, "%Y%m%d%H%M%S"),
                        "open": parse_price(r.get("open_pric")),
                        "high": parse_price(r.get("high_pric")),
                        "low": parse_price(r.get("low_pric")),
                        "close": parse_price(r.get("cur_prc")),
                        "volume": parse_int(r.get("trde_qty")),
                        "cum_volume": parse_int(r.get("acc_trde_qty")),
                    }
                )
            if len(bars) >= count or cont_yn != "Y" or not next_key:
                break
        bars.sort(key=lambda b: b["time"])
        return bars[-count:]

    def get_quote(self, code: str) -> dict:
        """ka10007 시세표성정보요청 -> 현재가/1호가 스냅샷 (웹소켓 미가동 시 폴백)."""
        data, _, _ = self.request("ka10007", "/api/dostk/mrkcond", {"stk_cd": code})
        return {
            "code": code,
            "name": (data.get("stk_nm") or "").strip(),
            "price": parse_price(data.get("cur_prc")),
            "open": parse_price(data.get("open_pric")),
            "high": parse_price(data.get("high_pric")),
            "low": parse_price(data.get("low_pric")),
            "prev_close": parse_price(data.get("pred_close_pric")),
            "ask": parse_price(data.get("sel_1bid")),
            "bid": parse_price(data.get("buy_1bid")),
            "volume": parse_int(data.get("trde_qty")),
            "upper_limit": parse_price(data.get("upl_pric")),
            "lower_limit": parse_price(data.get("lst_pric")),
            "raw": data,
        }

    # ------------------------------------------------------------ 주문
    def _dry_order_no(self) -> str:
        self._dry_order_seq += 1
        return f"DRY{self._dry_order_seq:07d}"

    def _send_order(self, api_id: str, body: dict, label: str) -> dict:
        if self.dry_run:
            order_no = self._dry_order_no()
            log.warning("[DRY-RUN] %s 미전송: %s -> ord_no=%s", label, body, order_no)
            return {"ord_no": order_no, "dry_run": True, "return_code": 0, "return_msg": "DRY-RUN"}
        data, _, _ = self.request(api_id, "/api/dostk/ordr", body)
        log.info("%s 전송 완료: %s -> ord_no=%s", label, body, data.get("ord_no"))
        return data

    def buy(self, code: str, qty: int, price: float | int | str = "", trade_type: str = cfg.ENTRY_ORDER_TYPE,
            cond_price: str = "") -> dict:
        """kt10000 주식 매수주문. 시장가(3)/최우선지정가(7) 등은 price 를 빈 문자열로 둔다."""
        body = {
            "dmst_stex_tp": cfg.DMST_STEX_TP,
            "stk_cd": code,
            "ord_qty": str(int(qty)),
            "ord_uv": "" if price in ("", None) else str(int(price)),
            "trde_tp": trade_type,
            "cond_uv": cond_price,
        }
        return self._send_order("kt10000", body, f"매수 {code} {qty}주")

    def sell(self, code: str, qty: int, price: float | int | str = "", trade_type: str = cfg.EXIT_ORDER_TYPE,
             cond_price: str = "") -> dict:
        """kt10001 주식 매도주문."""
        body = {
            "dmst_stex_tp": cfg.DMST_STEX_TP,
            "stk_cd": code,
            "ord_qty": str(int(qty)),
            "ord_uv": "" if price in ("", None) else str(int(price)),
            "trde_tp": trade_type,
            "cond_uv": cond_price,
        }
        return self._send_order("kt10001", body, f"매도 {code} {qty}주")

    def modify(self, orig_order_no: str, code: str, qty: int, price: float | int) -> dict:
        """kt10002 주식 정정주문."""
        body = {
            "dmst_stex_tp": cfg.DMST_STEX_TP,
            "orig_ord_no": orig_order_no,
            "stk_cd": code,
            "mdfy_qty": str(int(qty)),
            "mdfy_uv": str(int(price)),
            "mdfy_cond_uv": "",
        }
        return self._send_order("kt10002", body, f"정정 {code} {orig_order_no}")

    def cancel(self, orig_order_no: str, code: str, qty: int = 0) -> dict:
        """kt10003 주식 취소주문. qty=0 이면 잔량 전부 취소."""
        body = {
            "dmst_stex_tp": cfg.DMST_STEX_TP,
            "orig_ord_no": orig_order_no,
            "stk_cd": code,
            "cncl_qty": "0" if qty <= 0 else str(int(qty)),
        }
        return self._send_order("kt10003", body, f"취소 {code} {orig_order_no}")
