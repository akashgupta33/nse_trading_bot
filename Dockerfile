FROM python:3.12-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Create logs directory
RUN mkdir -p logs config

# Persist Fyers token and runtime config outside the container filesystem
VOLUME ["/app/config"]

# Copy entrypoint script
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Entrypoint: auto-auth then start agent
ENTRYPOINT ["/app/entrypoint.sh"]