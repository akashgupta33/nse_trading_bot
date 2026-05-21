FROM python:3.12-slim

WORKDIR /app

# Set exact timezone to IST so the OS scheduler aligns with the NSE
ENV TZ="Asia/Kolkata"

# System deps (including tzdata for timezone configuration)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ curl tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Create essential directories
RUN mkdir -p logs config

# Persist both the auth token (config) AND the SQLite Database (logs)
VOLUME ["/app/config", "/app/logs"]

# Copy entrypoint script
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Entrypoint: auto-auth then start agent
ENTRYPOINT ["/app/entrypoint.sh"]