FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY kzbonds/ kzbonds/
COPY app.py .
COPY scripts/ scripts/
COPY docker/scheduler_loop.py docker/scheduler_loop.py

RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8501

# Overridden by docker-compose.yml's `command:` for the scheduler service.
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
