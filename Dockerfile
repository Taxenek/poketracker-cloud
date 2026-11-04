FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir "curl_cffi>=0.7,<1.0" "aiohttp>=3.9,<4.0"

WORKDIR /app
COPY gateway.py /app/gateway.py

EXPOSE 8000
CMD ["python", "/app/gateway.py"]
