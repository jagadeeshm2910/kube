FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi==0.110.0 "uvicorn[standard]==0.27.1" httpx==0.26.0

COPY app.py .

RUN groupadd -r kara && useradd -r -g kara kara && chown -R kara:kara /app
USER kara

EXPOSE 8083

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8083/health')" || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8083"]
