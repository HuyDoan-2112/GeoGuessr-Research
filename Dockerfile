FROM mcr.microsoft.com/playwright:v1.57.0-jammy

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN python3 -m pip install --no-cache-dir -r /app/requirements.txt

COPY adapters/streetview_js/package.json adapters/streetview_js/package-lock.json /app/adapters/streetview_js/
WORKDIR /app/adapters/streetview_js
RUN npm ci --omit=dev
WORKDIR /app

COPY . /app

RUN mkdir -p /data/images && chown -R pwuser:pwuser /data

USER pwuser

ENV IMAGE_OUTPUT_DIR=/data/images

CMD ["python3", "-m", "apps.geoguessr_server"]