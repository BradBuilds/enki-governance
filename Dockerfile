FROM python:3.12-slim

WORKDIR /app

# System deps:
#   libpq-dev / gcc / postgresql-client — psycopg2 + migrate runner
#   poppler-utils                       — pdf2image (renders PDF pages to PNG)
#   tesseract-ocr                       — primary OCR engine (Phase 2.5)
#   libgl1 / libglib2.0-0               — optional cv2/paddleocr runtime libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc postgresql-client \
    poppler-utils tesseract-ocr libgl1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY services/api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY core/ /app/core/
COPY storage/ /app/storage/
COPY services/ /app/services/
COPY libraries/ /app/libraries/
COPY schemas/ /app/schemas/
COPY contracts/ /app/contracts/
COPY scripts/ /app/scripts/
COPY active_set_v4.json /app/active_set_v4.json
COPY active_set_v5.json /app/active_set_v5.json

# Clean stale bytecode cache
RUN find /app -name '*.pyc' -delete && find /app -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# Non-root user
RUN addgroup --system enki && adduser --system --ingroup enki enki
RUN chown -R enki:enki /app
USER enki

ENV PYTHONPATH=/app:/app/services/api/src
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["uvicorn", "services.api.src.main_dev:app", "--host", "0.0.0.0", "--port", "8000"]
