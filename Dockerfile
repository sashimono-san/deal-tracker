FROM node:24-bookworm-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM node:24-bookworm-slim AS harness
RUN npm install --global @earendil-works/pi-coding-agent@1.4.0

FROM node:24-bookworm-slim AS browser
WORKDIR /browser
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DEAL_TRACKER_DB=/data/tracker.sqlite3 \
    DEAL_TRACKER_PI_DIRECTORY=/pi \
    PI_OFFLINE=1 PI_TELEMETRY=0 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libstdc++6 libatomic1 xvfb xauth \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --user-group --create-home app \
    && mkdir /data /pi && chown app:app /data /pi
COPY --from=harness /usr/local/bin/node /usr/local/bin/node
COPY --from=harness /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js /usr/local/bin/pi
WORKDIR /app
COPY --from=browser /browser/node_modules node_modules/
COPY package.json package-lock.json ./
RUN node node_modules/playwright/cli.js install --with-deps chromium \
    && chmod -R a+rX /ms-playwright
COPY pyproject.toml ./
COPY deal_tracker/ deal_tracker/
COPY --from=frontend /frontend/dist/ deal_tracker/static/
RUN python -m pip install --no-cache-dir . && pi --version
USER app
EXPOSE 8765
CMD ["python", "-m", "uvicorn", "deal_tracker.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765"]

FROM base AS test
USER root
RUN python -m pip install --no-cache-dir '.[dev]'
COPY tests/ tests/
USER app
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

FROM base AS runtime