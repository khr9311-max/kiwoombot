# 빌드:  docker build -f trading_bot/deploy/Dockerfile -t kiwoom-bot .
# 실행:  docker run -d --name kiwoom-bot --restart unless-stopped \
#            --env-file trading_bot/.env \
#            -v "$PWD/data:/app/trading_bot/data" \
#            -v "$PWD/logs:/app/trading_bot/logs" \
#            -v "$PWD/models:/app/trading_bot/models" \
#            kiwoom-bot
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Seoul \
    PIP_NO_CACHE_DIR=1

# lightgbm 은 libgomp 가 필요하다
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 tzdata ca-certificates \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY trading_bot/requirements.txt /app/trading_bot/requirements.txt
RUN pip install --no-cache-dir -r /app/trading_bot/requirements.txt

COPY trading_bot /app/trading_bot

RUN useradd -m -u 10001 trader \
 && mkdir -p /app/trading_bot/data /app/trading_bot/logs /app/trading_bot/models \
 && chown -R trader:trader /app
USER trader

# 접속·계좌 점검 (컨테이너가 살아 있는지가 아니라 API 가 살아 있는지를 본다)
HEALTHCHECK --interval=5m --timeout=30s --start-period=1m --retries=3 \
    CMD python -m trading_bot.main --check || exit 1

CMD ["python", "-m", "trading_bot.main"]
