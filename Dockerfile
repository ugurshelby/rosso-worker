# Rosso worker — Railway/Render/Fly.io için
FROM python:3.13-slim

WORKDIR /app

# Bağımlılıklar (önce metadata kopyala — katman cache)
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY . .

EXPOSE 8000

# Health servisi (web). Cron dispatcher'lar Railway'de ayrı tanımlanır:
#   worker-fast:   python -m app.cron.fast      (*/5 * * * *)
#   worker-nightly: python -m app.cron.nightly  (0 3 * * *)
# bkz. worker/RAILWAY.md
CMD ["sh", "-c", "uvicorn app.health:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log --log-level warning"]
