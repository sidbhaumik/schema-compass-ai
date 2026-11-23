FROM python:3.12-slim

LABEL org.opencontainers.image.source=https://github.com/sidbhaumik/schema-compass-ai \
      org.opencontainers.image.licenses=MIT

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SCHEMA_COMPASS_HOME=/data HOST=0.0.0.0 PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY schema_compass ./schema_compass
COPY demo ./demo

RUN useradd --create-home --uid 10001 compass && mkdir -p /data && chown compass /data
USER compass
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz')"

# Set ANTHROPIC_API_KEY to enable AI review; SCHEMA_COMPASS_EMBEDDINGS for dense embeddings.
CMD ["python", "-m", "schema_compass.cli", "serve"]
