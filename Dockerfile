# Arvada RAG Agent — runtime container (API only; scraping/ingest run separately).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install runtime deps first for better layer caching.
COPY requirements-agent.txt .
RUN pip install --no-cache-dir -r requirements-agent.txt

# App code (config, db, api, agent package, entrypoint).
COPY config.py db.py api.py run_agent.py ./
COPY agent ./agent

EXPOSE 8001

# Readiness probe checks DB + Redis + OpenAI key.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8001/ready', timeout=4); sys.exit(0 if r.status==200 else 1)" || exit 1

CMD ["python", "run_agent.py"]
