# ── Stage 1: Build React frontend ──────────────────────────────────────────────
FROM node:20-alpine AS frontend-builder

WORKDIR /app/frontend
# Copy only package.json first (no package-lock.json — use npm install)
COPY frontend/package.json ./
RUN npm install --legacy-peer-deps
COPY frontend/ ./
RUN npm run build

# ── Stage 2: Runtime ────────────────────────────────────────────────────────────
# Use the current multi-arch HA base-python image (supports amd64 + aarch64).
# build.yaml is NOT used; this FROM is the single source of truth (HA 2026.04+).
FROM ghcr.io/home-assistant/base-python:3.12-alpine3.23

LABEL \
    io.hass.name="HawaAI AC Optimizer" \
    io.hass.description="AI-powered AC optimization using presence, temperature, and energy data" \
    io.hass.type="addon" \
    org.opencontainers.image.source="https://github.com/srobysingh-bot/Smart-Ac" \
    org.opencontainers.image.licenses="MIT"

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# gcc + musl-dev needed for aiosqlite/aiohttp C extensions on Alpine.
# openssl-dev + libffi-dev for cryptographic packages.
RUN apk add --no-cache \
    gcc \
    musl-dev \
    libffi-dev \
    openssl-dev

WORKDIR /app

# Install Python dependencies first (layer cache)
COPY backend/requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy backend source
COPY backend/ ./backend/

# Copy built React frontend
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

# Entry point
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
