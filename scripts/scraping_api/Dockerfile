FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Minimal deps: fastapi for the framework, uvicorn[standard] for the ASGI server,
# requests for proxy-aware HTTP, paramiko for the (optional) regen helper.
COPY scripts/scraping_api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy package + sibling __init__ so `scripts.scraping_api.app` resolves
COPY scripts/scraping_api /app/scripts/scraping_api
RUN touch /app/scripts/__init__.py

EXPOSE 8443

# Health: pool reports >=1 available proxy
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,json,sys; r=urllib.request.urlopen('http://127.0.0.1:8443/v1/health',timeout=4); d=json.loads(r.read()); sys.exit(0 if d.get('ok') else 1)"

CMD ["uvicorn", "scripts.scraping_api.app:app", "--host", "0.0.0.0", "--port", "8443", "--log-level", "info"]
