# The public service: refreshes the data on schedule and serves the page.
# Amvera builds this file (see amvera.yaml). The older docker/Dockerfile runs
# the Streamlit dashboard instead.
FROM python:3.11-slim

# pdftotext and tesseract read the bond prospectuses.
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-site.txt .
RUN pip install --no-cache-dir -r requirements-site.txt

COPY kzbonds/ kzbonds/

# /data is the persistent storage: the snapshot database, the caches and the
# built page live there and survive a redeploy.
ENV PYTHONUNBUFFERED=1 \
    KZBONDS_DATA_DIR=/data \
    PORT=80

EXPOSE 80

CMD ["python", "-m", "kzbonds.serve"]
