FROM node:22.16.0-bookworm-slim AS ui
WORKDIR /build/web_ui/collection
COPY web_ui/collection/package.json web_ui/collection/package-lock.json ./
RUN npm ci --include=optional
COPY web_ui/collection/ ./
RUN npm run build

FROM python:3.13.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
WORKDIR /app
COPY LICENSE README-COLLECTOR.md ./
COPY requirements-collector.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-collector.lock \
    && python -m playwright install --with-deps chromium \
    && useradd --uid 10001 --create-home collector \
    && mkdir -p /app/data && chown collector:collector /app/data
COPY core/collection/ ./core/collection/
COPY migrations_collector/ ./migrations_collector/
COPY --from=ui /build/static/collector/ ./static/collector/
USER collector
EXPOSE 8001
CMD ["python", "-m", "core.collection.cli", "serve", "--host", "0.0.0.0"]
