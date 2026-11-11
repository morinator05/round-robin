FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . .
# The release tag (e.g. v1.3.0), shown on the admin page. After the dependencies, so a new
# version doesn't invalidate their cached layer.
ARG APP_VERSION=""
ENV APP_VERSION=$APP_VERSION
# Not as root: a bug in the app can't change the image's files.
RUN useradd --system --no-create-home app
USER app
ENV PATH="/app/.venv/bin:$PATH"
# `exec`: the server becomes PID 1 and gets the stop signal, so it shuts down cleanly.
CMD ["sh", "-c", "alembic upgrade head && exec fastapi run app/main.py --port 8000"]
