FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY . .

RUN python -m pip install --no-cache-dir .

CMD ["python", "-m", "kalshi_bot.execution.engine", "--mode", "paper", "--interval", "60"]
