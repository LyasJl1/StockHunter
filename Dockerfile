FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml alembic.ini ./
COPY stockhunter ./stockhunter
COPY migrations ./migrations
RUN pip install --no-cache-dir -e ".[postgres]"

RUN useradd -m app && mkdir -p /app/var && chown -R app /app
USER app

# Railway : la commande de démarrage du service web est dans railway.json
# (migrations puis dashboard + lecteur de commandes Telegram). Un seul processus, peu de mémoire.
EXPOSE 8000
CMD ["sh", "-c", "stockhunter init-db && stockhunter dashboard serve --host 0.0.0.0 --port ${PORT:-8000} --telegram-worker"]
