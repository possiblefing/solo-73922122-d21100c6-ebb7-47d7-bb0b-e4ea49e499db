FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# 容器启动时 uvicorn 触发 startup 事件建表，PostgreSQL 未就绪会自动重试等待。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
