# Imagen oficial de Playwright: ya trae Chromium + todas las libs del sistema.
# Base noble = Ubuntu 24.04 LTS (soporte hasta 2029), Python 3.12.
# Política de versión: la última patch del minor ANTERIOR al más nuevo (probada
# por la comunidad varias semanas), fijada por tag + digest para builds reproducibles.
# El tag DEBE coincidir con playwright== en requirements.txt: lo valida
# tests/test_playwright_pin.py en CI. Procedimiento de upgrade en el README.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble@sha256:330d401ae23c905896a0c26c66c36fc971b621519e7b11b435a8328d7bf6e031

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Estado + screenshots persistentes.
VOLUME ["/app/data"]

# Dashboard LAN.
EXPOSE 8099

# Swarm/Dokploy reinicia la tarea si el dashboard deja de responder.
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "app.py"]
