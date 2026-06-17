FROM python:3.13-slim

WORKDIR /app

# Install curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p data/by_group data/logos data/sources

EXPOSE 8008

# Default: start web server. Override with "run" or "schedule"
CMD ["python3", "src/main.py", "web"]
