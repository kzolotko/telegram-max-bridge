FROM python:3.12-slim

# gcc + libc6-dev are required to compile tgcrypto (C extension)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# Run as non-root for security
RUN useradd --create-home --shell /bin/bash bridge
WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/ ./src/

# Sessions directory is mounted at runtime — never baked into the image
RUN mkdir -p /app/sessions && chown bridge:bridge /app/sessions
VOLUME ["/app/sessions"]

USER bridge

# Default: run the bridge. Override with docker compose run for setup/auth.
CMD ["python", "-u", "-m", "src"]
