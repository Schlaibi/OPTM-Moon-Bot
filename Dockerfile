FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY moonbot ./moonbot

RUN useradd -m bot && mkdir /data && chown bot /data
USER bot
VOLUME /data

CMD ["python", "-m", "moonbot.bot"]
