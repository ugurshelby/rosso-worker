# Rosso worker — konteyner imajı (7/24 servis DEĞİL; ağır işler on-demand GitHub Actions'ta)
FROM python:3.13-slim

WORKDIR /app

# Bağımlılıklar (önce metadata kopyala — katman cache)
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY . .

EXPOSE 8000

# Health servisi (web). Cron işleri `python -m app.cron.<is>` ile ayrıca çalıştırılır
# (Supabase pg_cron / GitHub Actions tetikler).
CMD ["sh", "-c", "uvicorn app.health:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log --log-level warning"]
