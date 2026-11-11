FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml alembic.ini ./
COPY stockhunter ./stockhunter
COPY migrations ./migrations
RUN pip install --no-cache-dir -e ".[postgres]"

RUN useradd -m app && mkdir -p /app/var && chown -R app /app
USER app

# Conteneur « job » : exécute une commande puis s'arrête (pas de service 24h/24).
# Railway Cron : définir la commande par service, ex. `stockhunter ingest all`.
ENTRYPOINT ["stockhunter"]
CMD ["status"]
