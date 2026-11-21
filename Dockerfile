FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Для OCR в контейнере раскомментируйте (увеличивает образ ~на 200 МБ):
# RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus \
#     && rm -rf /var/lib/apt/lists/*
# Для PDF-сканов дополнительно: pip install --no-cache-dir pypdfium2

COPY app ./app
COPY scripts/healthcheck.py ./scripts/healthcheck.py
COPY alembic.ini ./
COPY migrations ./migrations

RUN useradd --create-home appuser \
    && mkdir -p /app/data /app/backups \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "scripts/healthcheck.py", "http://127.0.0.1:8000/health"]

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
