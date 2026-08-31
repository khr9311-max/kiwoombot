# AWS Lightsail 배포 가이드 (모의투자용)

키움 REST API는 IPv4 전용이라 예전 OpenAPI+(ActiveX)와 달리 **Windows 서버가 필요 없습니다.**
가볍게 돌아가는 봇이라 저사양 리눅스 서버로 충분하고, 컴맹이어도 따라 하기 쉬운 **AWS Lightsail**(고정 요금, 브라우저 SSH) 기준으로 정리했습니다.

---

## 1. 인스턴스 생성 옵션

| 항목 | 선택 | 이유 |
|---|---|---|
| 리전 | 서울 (ap-northeast-2) | 키움 서버·거래시간이 모두 한국 기준이라 지연시간 최소화 |
| 플랫폼 | Linux/Unix | OpenAPI+ 아니라 REST API라 Windows 불필요 |
| 블루프린트 | OS Only → Ubuntu 22.04 | |
| 네트워크 타입 | **Dual-stack** (기본값 유지) | 키움 API(`api.kiwoom.com`, `mockapi.kiwoom.com`)는 진짜 IPv6 주소가 없는 IPv4 전용 서버. IPv6 only로 하면 접속 자체가 안 됨 |
| 플랜 | **$7/월 (1GB RAM, 2 vCPU, 40GB SSD, 2TB 전송)** | pandas+lightgbm 구동엔 512MB($5 플랜)는 여유가 없어 장중 OOM 위험. $12/$24는 이 봇 용도엔 과함 |

### 왜 Dual-stack인가
DNS로 확인해보면:
```
api.kiwoom.com     A    → 112.175.65.17          (진짜 IPv4)
api.kiwoom.com     AAAA → 64:ff9b::3ae5:889e      (NAT64가 합성한 가짜 IPv6, 진짜 아님)
```
`64:ff9b::/96`은 IPv4 주소를 IPv6처럼 흉내 낸 합성 주소라는 뜻으로, 키움 서버는 실제 IPv6가 없는 IPv4 전용입니다. IPv6 only로 서버를 만들면 키움 API·웹소켓에 접속이 안 되므로 **Dual-stack 필수**입니다. (텔레그램은 진짜 IPv6를 지원하지만, 어차피 Dual-stack이면 둘 다 문제없이 붙습니다.)

---

## 2. 서버 접속

Lightsail 콘솔 → 생성한 인스턴스 클릭 → **"연결"** 탭 → **"브라우저 기반 SSH로 연결"**
(별도 프로그램 설치 없이 브라우저에서 바로 터미널 사용)

---

## 3. 서버 기본 설정

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-venv python3-pip git
sudo timedatectl set-timezone Asia/Seoul
```

---

## 4. 봇 설치

```bash
git clone <본인 저장소 주소> ~/kiwoom-bot
cd ~/kiwoom-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r trading_bot/requirements.txt
cp trading_bot/.env.example trading_bot/.env
nano trading_bot/.env
```

`.env`에 채워야 할 값: `KIWOOM_APP_KEY_MOCK`, `KIWOOM_APP_SECRET_MOCK`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
nano 저장: `Ctrl+O` → Enter, 나가기: `Ctrl+X`.

---

## 5. 연동 확인

```bash
python -m trading_bot.main --check
```

토큰 발급 → 예수금 → 잔고 → 현재가 → 분봉까지 정상 조회되면 통과.

---

## 6. 24시간 자동 실행 (systemd)

```bash
sudo cp trading_bot/deploy/kiwoom-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kiwoom-bot
journalctl -u kiwoom-bot -f      # 실시간 로그, Ctrl+C로 종료
```

서버 재부팅 시에도 자동 재시작되고, 평일 08:10부터 스케줄대로 동작합니다. 이후 매매 결과·오류는 텔레그램으로 실시간 전송됩니다.

---

## 7. 예상 운용비용 (서울 리전, $7 플랜 기준)

| 항목 | 월 비용 |
|---|---|
| Lightsail $7 플랜 (1GB RAM, 40GB SSD, 2TB 전송 포함) | 약 $7 (~1만 원) |
| 고정 IPv4 (인스턴스에 연결된 상태 유지 시) | 무료 |
| 데이터 전송 초과분 | 이 봇 트래픽으로는 거의 발생 안 함 |
| **합계** | **월 약 1만 원 내외 (연 약 12만 원)** |

---

## 8. AI API는 필요 없음

- 진입/청산 로직: 규칙 기반(이동평균, RSI, 체결강도 등) — 서버 내부 계산, 외부 API 불필요
- 메타 라벨링 필터(선택 기능): LightGBM 로컬 학습·추론 — OpenAI/Claude 등 외부 AI API 호출 없음, 비용 없음
- 실제로 쓰는 외부 API는 키움 REST API(매매)와 텔레그램 봇 API(알림) 뿐이며 둘 다 무료

---

## 9. 실전 투입 전 체크리스트

- [ ] `--check` 통과
- [ ] `--screen`으로 유니버스 확인
- [ ] 모의투자 최소 2~4주 무중단 운용
- [ ] 미체결·부분체결·취소가 로그/`--trades` 리포트와 일치하는지 확인
- [ ] 킬스위치(`DAILY_LOSS_LIMIT_PCT`)를 낮게 잡아 실제 발동·청산 테스트
- [ ] 봇 강제 종료(SIGKILL) 후 재기동해 포지션 복원 확인
- [ ] 실계좌 전환 시 `DRY_RUN=true`로 1주 → `MAX_ORDER_AMOUNT` 아주 낮게 잡고 소액 시작
