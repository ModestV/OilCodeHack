# One image: built React UI + FastAPI backend. Node is only needed at build time.
FROM node:22-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ARG REVISION=docker
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OILCODE_STORAGE=/app/storage \
    OILCODE_DATA_DIR=/data
# bsdtar unpacks the organisers' data.rar/zip when it is mounted unextracted.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libarchive-tools curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY backend backend
COPY tools/modeling tools/modeling
COPY reports/modeling reports/modeling
COPY scripts scripts
COPY docs/reference docs/reference
COPY docker/entrypoint.sh docker/entrypoint.sh
COPY --from=frontend /app/frontend/dist frontend/dist
RUN echo "$REVISION" > build_revision.txt && chmod +x docker/entrypoint.sh
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=20 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1
ENTRYPOINT ["/app/docker/entrypoint.sh"]
