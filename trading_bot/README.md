# 키움 REST API 자동매매 봇

업로드하신 세 문서를 하나의 시스템으로 합친 것입니다.

| 문서 | 반영된 부분 |
|---|---|
| `automated_trading_system_guide.txt` | 4단계 파이프라인(스크리닝 → 시그널 → 리스크 → 집행), 다중 팩터 스코어링, 손절/익절/트레일링/타임컷/킬스위치, 레이트리밋·토큰갱신·재시작복구 |
| `ai_trading_strategy_dashboard.html` | 메타 라벨링 2단계 구조, 삼중 장벽 라벨링, 프랙셔널(하프) 켈리 사이징, Purged K-Fold·DSR 검증 |
| `키움 REST API 문서.pdf` | 실제 연동 스펙 전체 (아래 표) |
| `키움 트레이딩뷰 웹훅 가이드.pdf` | 주문 파라미터(`trde_tp`, `dmst_stex_tp`) 코드값 |

전략은 **규칙 기반 데이트레이딩을 본체로 하고, LightGBM 메타 필터를 나중에 끼울 수 있는 슬롯**으로 만들었습니다.
메타 모델은 학습 데이터(= 1차 시그널의 과거 성패 기록)가 있어야 훈련되므로 이 순서가 강제됩니다.

---

## 🚀 빠른 시작 가이드 (Quick Start)

처음 자동매매 봇을 사용하시는 분들은 아래 순서대로 따라해 보세요.

### 1단계: 계좌 및 API 키 준비
1. 키움증권 계좌 개설 후 **모의투자 신청**을 완료합니다.
2. [키움 OpenAPI 포털](https://openapi.kiwoom.com)에 접속하여 회원가입 후 **앱(App) 등록**을 진행합니다.
3. 발급받은 `APP_KEY`와 `APP_SECRET`을 복사해 둡니다.

### 2단계: 텔레그램 알림 봇 만들기 (선택)
1. 텔레그램에서 `@BotFather`를 검색해 새 봇을 만들고 **HTTP API Token**을 받습니다.
2. `@userinfobot` 등을 이용해 본인의 **Chat ID**를 확인합니다.

### 3단계: 환경설정 파일(.env) 작성
설치(아래 2번 항목 참조)를 마친 후, 폴더 내에 `.env` 파일을 만들고 아래 내용을 채워넣습니다. (`.env.example` 파일 참고)
```ini
KIWOOM_ENV=mock                      # 실매매시 real 로 변경

# 실전과 모의투자는 앱키가 서로 다릅니다. KIWOOM_ENV 에 맞는 쪽이 자동 선택됩니다.
KIWOOM_APP_KEY_MOCK=모의투자_앱키
KIWOOM_APP_SECRET_MOCK=모의투자_시크릿
KIWOOM_APP_KEY_REAL=실전_앱키
KIWOOM_APP_SECRET_REAL=실전_시크릿

DRY_RUN=false                        # true 면 주문을 실제로 보내지 않고 로그만 기록
NOTIFIER=telegram
TELEGRAM_BOT_TOKEN=텔레그램_봇_토큰   # 변수명 주의: TELEGRAM_TOKEN 아님
TELEGRAM_CHAT_ID=텔레그램_채팅ID
```

> 스크리닝용 일봉/시가총액도 전부 키움 REST API(ka10081/ka10099) 로 받으므로 별도
> 데이터 계정은 필요 없습니다. 다만 종목별로 순차 조회하다 보니(2,000종목대) 08:10
> 스크리닝 시작부터 09:00 장 시작까지의 시간이 걸립니다.

> 모의투자에서는 `DRY_RUN=false` 로 두는 편이 낫습니다. `true` 면 주문 자체가 나가지 않아
> 체결·미체결·부분체결을 한 번도 겪어보지 못한 채로 실계좌에 가게 됩니다.
> 모의투자 계좌는 어차피 가짜 돈이므로, 안전장치는 `KIWOOM_ENV=mock` 그 자체입니다.
> `DRY_RUN` 은 **실계좌로 넘어간 뒤** 첫 주를 위한 장치입니다(실계좌면 자동으로 켜집니다).

### 4단계: 정상 연동 확인
터미널(또는 명령 프롬프트)에서 아래 명령어를 쳐서 잔고와 시세가 잘 조회되는지 점검합니다.
```bash
python -m trading_bot.main --check
```

### 5단계: 자동매매 시작!
평일 장 시작 전(08:10 이전)에 아래 명령어로 봇을 실행해 두면, 설정된 스케줄에 따라 자동으로 종목 탐색 및 매매를 진행합니다.
```bash
python -m trading_bot.main
```
매매 결과 및 오류 내역은 텔레그램으로 실시간 전송됩니다! 처음에는 **반드시 모의투자(`KIWOOM_ENV=mock`)** 환경에서 봇의 동작을 충분히 지켜보세요.

---

## 1. 사용하는 키움 API

| API ID | 경로 | 용도 |
|---|---|---|
| `au10001` / `au10002` | `/oauth2/token`, `/oauth2/revoke` | 접근토큰 발급·폐기 (만료시각 캐싱, 401 시 자동 재발급) |
| `ka10099` | `/api/dostk/stkinfo` | 종목정보 리스트 — 관리종목·거래정지·투자경고 필터 |
| `ka10080` | `/api/dostk/chart` | 주식분봉차트 — 기동 시 워밍업 |
| `ka10007` | `/api/dostk/mrkcond` | 시세표성정보 — 웹소켓 끊김 시 폴백 |
| `kt00001` | `/api/dostk/acnt` | 예수금상세현황 (`entr`, `ord_alow_amt`) |
| `kt00018` | `/api/dostk/acnt` | 계좌평가잔고내역 — 포지션 복원·대사 |
| `ka10075` | `/api/dostk/acnt` | 미체결요청 — 주문 원장 대사 |
| `kt10000` / `kt10001` | `/api/dostk/ordr` | 매수 / 매도 주문 |
| `kt10002` / `kt10003` | `/api/dostk/ordr` | 정정 / 취소 주문 |
| `0B` (WebSocket) | `wss://…:10000/api/dostk/websocket` | 실시간 주식체결 — 틱을 1분봉으로 집계 |
| `00` (WebSocket) | 〃 | 실시간 주문체결 — 체결 즉시 인지 |

> **설계 포인트**: 장중 시세를 REST 로 폴링하지 않고 웹소켓 `0B` 틱을 받아 봉을 직접 만듭니다.
> 감시 종목이 20개든 30개든 REST 호출이 늘지 않아 초당 호출 제한을 구조적으로 회피합니다.

---

## 2. 설치

```bash
git clone <repo> && cd 키움자동매매

python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # Linux/macOS

pip install -r trading_bot/requirements.txt

cp trading_bot/.env.example trading_bot/.env
# .env 에 KIWOOM_APP_KEY_MOCK / KIWOOM_APP_SECRET_MOCK / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 입력
```

앱키는 [키움 REST API 포털](https://openapi.kiwoom.com)에서 발급받고,
모의투자를 쓰려면 포털에서 **모의투자 신청**을 먼저 해야 `mockapi.kiwoom.com` 이 응답합니다.

### 접속 점검

```bash
python -m trading_bot.main --check
```

토큰 발급 → 예수금 → 잔고 → 현재가 → 분봉까지 한 번에 확인합니다. 여기서 통과해야 다음 단계로 갑니다.

---

## 3. 실행

```bash
python -m trading_bot.main            # 스케줄 상주 (평일 08:10부터 자동 진행)
python -m trading_bot.main --now      # 스케줄 무시하고 즉시 세션 시작
python -m trading_bot.main --screen   # 장 전 스크리닝만 실행하고 종료
python -m trading_bot.main --check    # 접속/계좌 점검만
```

### 일간 타임라인

| 시각 | 동작 |
|---|---|
| 08:10 | 휴장일 확인 → 스크리닝 → 유니버스 확정 → 분봉 워밍업 → 실시간 구독 |
| 09:00 | 시그널 엔진 가동 |
| 상시 | 1초 청산 감시 · 5초 미체결 스윕 · 30초 계좌 대사 · 60초 시세 끊김 점검 |
| 14:30 | 신규 진입 중단 (청산 감시는 계속) |
| 15:15 | 당일 포지션 일괄 청산 |
| 15:25 | 구독 해제, 봉 스냅샷 DB 저장 |
| 16:00 | 일일 리포트 텔레그램 발송 |

---

## 4. 전략

### 진입 — 다중 팩터 스코어링 (합계 4점 이상)

| 팩터 | 조건 | 점수 |
|---|---|---|
| A 거래대금 유입 | 당일 누적거래대금 ≥ 전일 대비 30% | +2 |
| B 단기 정배열 | 1분봉 5선 > 20선 (골든크로스 시 +0.5 가산) | +1 |
| C 모멘텀 | RSI(14) ≥ 50 이며 상승 중 | +1 |
| D 체결강도 | 110% 이상 (매수세 우위) | +1 |

볼린저 상단 돌파 + RSI ≥ 80 인 과열 구간은 점수와 무관하게 진입하지 않습니다.

### 청산 (우선순위 순)

1. **손절** 매수가 대비 −2.0% (또는 ATR 기반 동적 손절가 중 먼저 닿는 쪽) → 전량 시장가
2. **트레일링 스탑** 1차 익절 이후, 고점 대비 −1.5% → 잔량 전량
3. **1차 익절** +3.0% → 보유 수량의 50%
4. **타임컷** 진입 후 60분간 ±1% 횡보 → 전량
5. **일괄 청산** 15:15 오버나잇 회피
6. **킬스위치** 당일 −3% → 전량 청산 + 당일 매매 중단 (한 번 켜지면 그날 안 꺼짐)

### 포지션 사이징 (`SIZING_MODE`)

- `fixed_pct` — 주문가능금액의 10% (가이드 기본값)
- `atr_risk` — 자산 × 0.5% ÷ (1.5 × ATR). 변동성이 큰 종목일수록 적게 삽니다
- `half_kelly` — `f* = (p(b+1) − 1) / b`, 실제 투입은 0.5 × f*
  최근 30일 완결 매매가 20건 이상 쌓이면 실적 승률·손익비를 자동으로 사용합니다

모든 모드에 1회 최대 주문금액(300만원), 최소 주문금액(10만원), 최대 보유 종목 수(5) 상한이 함께 걸립니다.

---

## 5. 안전장치

| 위험 | 대응 |
|---|---|
| 실계좌 오발주 | `KIWOOM_ENV=real` 이면 `DRY_RUN` 이 자동으로 켜집니다. 끄려면 명시적으로 `DRY_RUN=false` |
| 초당 호출 제한 | 토큰 버킷(기본 4 req/s) + HTTP 429 및 본문 코드 1700~1702 지수 백오프. 장중 시세는 웹소켓이라 REST 를 거의 안 씀 |
| 토큰 만료 | 만료시각 캐싱 + 10분 전 갱신. 키움은 만료를 **HTTP 200 + 본문 `return_code` 8005**(또는 `return_msg` 안 `[8005:…]`)로 알리므로 본문 코드까지 보고 재발급·재시도 |
| 네트워크 단절 | 웹소켓 지수 백오프 재접속(최대 30초) + **구독 자동 복원**. PING 은 JSON·문자열 양쪽 모두 에코 |
| 웹소켓 인증 실패 | 토큰 계열 코드면 접근토큰을 재발급한 뒤 재접속(같은 토큰 무한 재시도 방지) |
| 서버 재시작 | 기동 시 `kt00018` 잔고로 포지션 복원. 내부에 없던 보유 종목도 감시 대상에 편입 |
| 통보 유실 | 30초마다 `ka10075` 미체결 원장과 내부 상태 대사 |
| 미체결 방치 | 30초 초과 시 취소. 매수는 1회 재시도, **매도는 시장가로 반드시 재전송** |
| 슬리피지 | 시그널가 대비 +1% 이상 뛴 종목은 진입 포기 |
| 중복/과다 주문 | 진입 판정과 동시에 자리를 선점(`reserve_slot`)해 동시 시그널이 겹쳐도 1건만 발주 |
| 주문 거부 | 실시간 `00` 의 `919`(거부사유)를 감지해 즉시 자리를 풀고 알림 |
| 알림 장애 | 전송 실패가 매매를 막지 않음. 연속 3회 실패 시 60초 침묵 |
| 시세 끊김 | 보유 종목 시세가 5분 이상 없으면 경고 + 자동 재구독 |

---

## 6. 메타 라벨링(ML) 켜는 법

```
1) 데이터 축적   META_FILTER_ENABLED=false 로 모의투자 2~4주 운용
                 → 모든 1차 시그널과 11개 피처가 signals 테이블에 쌓입니다
2) 라벨링·학습   python -m trading_bot.tools.train_meta
                 → 삼중 장벽으로 y∈{0,1} 라벨링
                 → Purged K-Fold(embargo 2%) 로 LightGBM 교차검증
                 → models/meta_lgbm.pkl 저장
3) 필터 가동     .env 에서 META_FILTER_ENABLED=true
                 → P(y=1) ≥ 0.60 인 시그널만 실제 주문
```

교차검증 AUC 가 0.55 미만이면 경고가 뜹니다. **그 상태로 켜면 필터가 없느니만 못합니다.**
모델 파일이 없거나 로드에 실패하면 조용히 PassThrough(전량 승인)로 폴백하므로 매매가 멈추지는 않습니다.

---

## 7. 백테스트 · 리포트

```bash
# 로직 검증 (네트워크·앱키 불필요)
python -m trading_bot.tools.backtest --source synth --days 4 --symbols 6

# 실제 분봉으로 파라미터 튜닝
python -m trading_bot.tools.backtest --source api --codes 005930,000660

# 봇이 저장해 둔 봉을 재생
python -m trading_bot.tools.backtest --source db --days 30
```

백테스터는 `SignalEngine` · `RiskManager` · `BarSeries` 를 실매매와 **같은 코드로** 돌립니다.
주문 집행만 시뮬레이터로 바꿔, 백테스트와 실매매 로직이 갈라지는 것을 막습니다.
수수료 0.015% + 거래세 0.18% 를 반영합니다.

```bash
python -m trading_bot.tools.report                # 오늘 요약
python -m trading_bot.tools.report --days 30      # 최근 30일 (MDD·샤프 포함)
python -m trading_bot.tools.report --trades       # 완결 매매 목록
python -m trading_bot.tools.report --signals      # 시그널·메타필터 판정 이력
```

---

## 8. 테스트

```bash
python -m pytest tests -q      # 78 passed
```

- `test_core.py` — 응답 파싱(부호 접두어), 지표, 봉 집계, 사이징, 청산 규칙, 킬스위치, 계좌 복원, 삼중 장벽
- `test_execution.py` — 주문·부분체결·취소·재전송·대사, DRY-RUN 차단, 웹소켓 LOGIN/PING/REG/REAL 프로토콜
- `test_integration.py` — 틱 → 봉 → 시그널 → 주문 → 체결 → 청산 전 구간, 동시성 회귀
- 응답코드 처리(문자열 `"0"`, 본문 토큰만료, 유량제한), 주문 거부, 맨 문자열 PING

---

## 9. 배포 (Linux)

### systemd

```bash
sudo useradd -r -m -d /opt/kiwoom-bot trader
sudo cp -r trading_bot /opt/kiwoom-bot/
cd /opt/kiwoom-bot && sudo -u trader python3 -m venv .venv
sudo -u trader .venv/bin/pip install -r trading_bot/requirements.txt

sudo cp trading_bot/deploy/kiwoom-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now kiwoom-bot
journalctl -u kiwoom-bot -f
```

봇이 스케줄러를 내장하므로 **cron 은 필요 없습니다.** 24시간 상주시키세요.
`Restart=on-failure` 에 `StartLimitBurst=5` 를 걸어, 반복 실패 시 재시작 폭주를 막습니다.

### Docker

```bash
docker build -f trading_bot/deploy/Dockerfile -t kiwoom-bot .
docker run -d --name kiwoom-bot --restart unless-stopped \
    --env-file trading_bot/.env \
    -v "$PWD/data:/app/trading_bot/data" \
    -v "$PWD/logs:/app/trading_bot/logs" \
    -v "$PWD/models:/app/trading_bot/models" \
    kiwoom-bot
```

서버 시간대를 반드시 `Asia/Seoul` 로 맞추세요. 스케줄이 전부 한국 장 시간 기준입니다.

---

## 10. 실전 투입 전 체크리스트

- [ ] `--check` 통과 (토큰·예수금·잔고·시세·분봉)
- [ ] `--screen` 으로 유니버스가 상식적인지 눈으로 확인
- [ ] 모의투자에서 **최소 2~4주** 무중단 운용 — 동시호가, 상하한가, 거래정지, 장 마감 예외를 겪게 하세요
- [ ] 미체결·부분체결·취소가 로그와 `--trades` 리포트에서 정확히 맞아떨어지는지 확인
- [ ] 킬스위치를 `DAILY_LOSS_LIMIT_PCT=-0.001` 로 일부러 낮춰 실제로 발동·청산되는지 확인
- [ ] 봇을 강제 종료(SIGKILL)했다가 재기동해 포지션이 복원되는지 확인
- [ ] 실계좌 전환은 `DRY_RUN=true` 로 1주일 → 그 다음 `MAX_ORDER_AMOUNT` 를 아주 낮게 잡고 소액 시작

---

## 11. 공식 샘플 대조 결과

`official_reference/` (키움 공식 예제 저장소, 419개 파일 + 337개 API 스펙 JSON) 와 전면 대조했습니다.

**일치 확인**
- 사용하는 API 11개의 경로 전부 일치
- 응답 필드 60여 개 중 59개가 공식 스펙과 일치
- 주문 본문(`dmst_stex_tp`/`stk_cd`/`ord_qty`/`trde_tp`/`ord_uv`/`cond_uv`)과 `trde_tp` 코드값 일치
  - 단, **모의투자는 최유리(6/16/26)·최우선(7)·중간가(29/30/31) 지정가를 거부**한다(`RC4026`).
    공식 스펙에는 없는 환경 제약이라 `settings.MOCK_UNSUPPORTED_ORDER_TYPES` 로 대체표를 두고,
    기동 시 자동 치환 + 런타임 `RC4026` 수신 시 세션 매매구분을 낮춰 재전송한다.
- 웹소켓 `LOGIN`/`PING`/`REG` 규약과 `0B`·`00` 필드번호 일치

**대조로 찾아 고친 것**
| 문제 | 영향 |
|---|---|
| 토큰 만료를 HTTP 401 로만 판단 | 키움은 **HTTP 200 + `return_code` 8005**(또는 `return_msg` 안 `[8005:…]`)로 알린다. 401 은 오지 않으므로 자동 재발급이 영영 작동하지 않고, 토큰 수명 24시간이 끝나면 봇이 통째로 멎는다 |
| `return_code` 를 int 로만 비교 | 서버가 `"0"`·`"0000"` 문자열도 보낸다. 정상 응답을 오류로 오인 |
| 유량 제한 코드(1700~1702) 미처리 | 429 만 보고 있었다. 본문 코드로 오는 유량 제한에 백오프하지 않음 |
| 웹소켓 PING 을 JSON 으로만 처리 | 맨 문자열 `"PING"` 으로도 온다. 놓치면 서버가 연결을 끊는다 |
| 로그인 실패 시 같은 토큰으로 재접속 | 토큰이 원인이면 무한 재시도. 이제 재발급 후 재접속한다 |
| `00` 의 `919`(거부사유) 미사용 | 거부된 주문이 대사 때까지 자리를 붙잡아 다음 진입을 막았다 |
| `ka10080` 의 `acc_trde_qty` 에 의존 | 공식 응답 스펙에 없는 필드. 기준선이 0 이면 첫 봉에 하루치 거래량이 통째로 들어갔다 |
| 실전/모의 앱키가 같다고 가정 | 키움은 환경별로 키가 다르다. `KIWOOM_ENV` 만 바꾸면 8031(모드 불일치)이 난다 |

**남은 차이**
- `ka10075`(미체결) 의 `stk_cd` 는 선택 파라미터라 전체 조회 시 보내지 않도록 맞췄습니다.
- 공식 예제는 요청 간격을 `0.2초`(초당 5건)로 둡니다. 이 봇의 기본 토큰 버킷은 초당 4건으로 더 보수적입니다.

---

## 12. 한계와 주의

- **관리종목 필터는 `ka10099` 의 `state`/`auditInfo`/`orderWarning` 문자열 매칭**입니다.
  키움이 문자열을 바꾸면 걸러지지 않을 수 있으니 `--screen` 결과를 주기적으로 눈으로 확인하세요.
- 웹소켓 `LOGIN`/`PING` 규약은 공식 샘플(`official_reference/kiwoom/core/ws_client.py`)로 **검증 완료**했습니다.
- **`kt00001` 의 `ord_alow_amt`(주문가능금액)를 예수금으로 사용**합니다. 신용·미수를 쓰신다면 사이징 기준을 다시 보셔야 합니다.
- **Factor A 의 '전일 거래대금'은 스크리닝 패널의 20일 평균으로 근사**했습니다. 정확한 전일값을 쓰려면 `screener.build_panel` 결과에서 직전 영업일 값을 꺼내 `set_prev_turnover` 에 넣으세요.
- 백테스트의 체결은 종가 기준 낙관적 가정입니다. 실제 슬리피지는 더 큽니다.
- 이 봇은 **자동으로 실제 돈을 씁니다.** 손실 책임은 운용자에게 있습니다. 모의투자 검증을 건너뛰지 마세요.
