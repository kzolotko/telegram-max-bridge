FROM python:3.12-slim

# gcc + libc6-dev are required to compile tgcrypto (C extension)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/ ./src/
COPY diagnose.py ./

# Sessions directory is mounted at runtime — never baked into the image
VOLUME ["/app/sessions"]

CMD ["python", "-u", "-m", "src"]
