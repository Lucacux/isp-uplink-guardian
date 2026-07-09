# Imagen oficial de Playwright: ya trae Chromium + todas las libs del sistema.
# La versión del tag DEBE coincidir con playwright== en requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Estado + screenshots persistentes.
VOLUME ["/app/data"]

# Dashboard LAN.
EXPOSE 8090

CMD ["python", "app.py"]
