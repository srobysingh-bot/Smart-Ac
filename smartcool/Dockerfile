# ── Stage 1: Build React frontend ──────────────────────────────────────────────
FROM node:18-alpine AS frontend-builder

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --legacy-peer-deps
COPY frontend/ ./
RUN npm run build

# ── Stage 2: Runtime ────────────────────────────────────────────────────────────
ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Only add C-extension build deps not already in the base-python image.
# Do NOT reinstall python3/py3-pip (already in base) or cargo (Rust is huge
# and causes ARM build timeouts).
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
