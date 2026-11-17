FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY moonbot ./moonbot

# The user is set by docker-compose (PUID/PGID) so files in ./data belong to the host user.
RUN mkdir /data

CMD ["python", "-m", "moonbot.bot"]
