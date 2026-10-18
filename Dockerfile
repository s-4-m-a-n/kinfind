# Kinfind on Cloud Run.
#
# Data lives in /data: the SQLite database is restored from Cloud Storage at start-up and
# replicated back continuously by Litestream; uploaded photos are a Cloud Storage volume
# mounted at /data/uploads. Run a single instance (one writer to the database).
FROM python:3.12-slim

ARG LITESTREAM_VERSION=0.3.13
ADD https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-v${LITESTREAM_VERSION}-linux-amd64.tar.gz /tmp/litestream.tar.gz
RUN tar -C /usr/local/bin -xzf /tmp/litestream.tar.gz && rm /tmp/litestream.tar.gz

# DejaVu fonts: illustration headers and footers (app/illustrate.py) need a real font.
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DISASTERLENS_DATA=/data PORT=8080
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY tools tools
COPY deploy/litestream.yml /etc/litestream.yml
COPY deploy/start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh && mkdir -p /data/uploads

CMD ["start.sh"]
