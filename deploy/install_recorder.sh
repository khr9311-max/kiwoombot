#!/usr/bin/env bash
# 하이닉스 실시간 기록기 설치 — Lightsail 서버에서 실행한다.
#
#   ssh -i LightsailDefaultKey-ap-northeast-2.pem ubuntu@3.35.170.226
#   cd ~/kiwoom-bot && git pull && bash deploy/install_recorder.sh
#
# 하는 일
#   1) 기존 매매 봇(kiwoom-bot) 정지·비활성화
#      — 같은 앱키로 WebSocket 세션이 충돌할 수 있어 기록기와 공존시키지 않는다.
#   2) 의존성 설치 (yfinance)
#   3) hynix-record.service / .timer 설치 후 평일 08:31 자동 기동 등록
#   4) 과거 데이터 백필 (일봉/분봉/수급/외부지표)
#
# 되돌리려면:
#   sudo systemctl disable --now hynix-record.timer
#   sudo systemctl enable --now kiwoom-bot
set -euo pipefail

REPO=/home/ubuntu/kiwoom-bot
PY="$REPO/.venv/bin/python"
PIP="$REPO/.venv/bin/pip"
cd "$REPO"

echo "=== 1) 기존 매매 봇 정지 ==="
if systemctl is-active --quiet kiwoom-bot; then
    sudo systemctl stop kiwoom-bot
    echo "  kiwoom-bot 정지됨"
else
    echo "  kiwoom-bot 이미 정지 상태"
fi
sudo systemctl disable kiwoom-bot >/dev/null 2>&1 || true
echo "  자동 기동 해제 완료 (되돌리기: sudo systemctl enable --now kiwoom-bot)"

echo
echo "=== 2) 의존성 ==="
"$PIP" install -q yfinance
"$PY" -c 'import yfinance, websockets; print("  yfinance", yfinance.__version__, "/ websockets OK")'

echo
echo "=== 3) systemd 등록 ==="
sudo cp deploy/hynix-record.service /etc/systemd/system/
sudo cp deploy/hynix-record.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hynix-record.timer
systemctl list-timers hynix-record.timer --no-pager | head -3

echo
echo "=== 4) 과거 데이터 백필 ==="
"$PY" -m hynix backfill

echo
echo "=== 완료 ==="
echo "  다음 기동: 평일 08:31 (자동)"
echo "  수동 확인 : sudo systemctl start hynix-record && journalctl -u hynix-record -f"
echo "  수집 현황 : $PY -m hynix summary"
