# Stage 1: build C extensions (tgcrypto)
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: production image (no compiler)
FROM python:3.12-slim

COPY --from=builder /install /usr/local

# Run as non-root for security
RUN useradd --create-home --shell /bin/bash bridge
WORKDIR /app

# Copy application source
COPY src/ ./src/

# Sessions directory is mounted at runtime — never baked into the image
RUN mkdir -p /app/sessions && chown bridge:bridge /app/sessions
VOLUME ["/app/sessions"]

USER bridge

# Default: run the bridge. Override with docker compose run for setup/auth.
CMD ["python", "-u", "-m", "src"]
