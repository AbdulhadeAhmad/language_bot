FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS base
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH" UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable
COPY alembic.ini ./
COPY migrations ./migrations
RUN useradd --uid 10001 --create-home bot

FROM base AS inference
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable --extra inference
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=1
USER bot
EXPOSE 8080
CMD ["language-bot-model"]

FROM base AS api
USER bot
EXPOSE 8060
CMD ["language-bot-api"]
