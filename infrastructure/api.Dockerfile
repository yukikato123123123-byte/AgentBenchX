FROM python:3.12-slim
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources && apt-get update && apt-get install -y --no-install-recommends git openssh-client && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY apps/api ./apps/api
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps .
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
CMD ["sh", "-c", "alembic upgrade head && python scripts/seed.py && uvicorn agentbenchx.main:app --host 0.0.0.0 --port 8000"]
