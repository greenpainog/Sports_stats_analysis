# The stats API (FastAPI + the LLM question parser) in one image.
# The database is not in the image: docker-compose.yml mounts ./data read-only.
FROM python:3.13-slim

# No .pyc files in the image, and logs appear right away in `docker logs`.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SPORTS_DB_PATH=/app/data/sports_analytics.db

WORKDIR /app

# Packages first: this layer is reused until requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY analysis/ analysis/
COPY api/ api/
COPY database/ database/
COPY llm/ llm/

# Run as a normal user, not root.
RUN useradd --create-home app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
