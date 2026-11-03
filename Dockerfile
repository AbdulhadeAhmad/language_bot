FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH" UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY app ./app
RUN uv sync --locked --no-dev --no-editable
COPY alembic.ini ./
COPY migrations ./migrations
RUN useradd --uid 10001 --create-home bot
USER bot
EXPOSE 8060
CMD ["language-bot-api"]
