# syntax=docker/dockerfile:1.7
# Image unique pour api, worker, migrate et simulateurs : seul le CMD change.

ARG PYTHON_VERSION=3.12

# --------------------------------------------------------------------- builder
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# 1) Dépendances seules : couche mise en cache tant que uv.lock ne change pas.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project

# 2) Le projet lui-même, installé en mode non éditable.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# --------------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 10001 xaalis \
 && useradd --system --uid 10001 --gid xaalis --home-dir /app --shell /usr/sbin/nologin xaalis

WORKDIR /app
COPY --from=builder --chown=root:root /opt/venv /opt/venv
COPY --chown=root:root alembic.ini ./
COPY --chown=root:root migrations ./migrations

USER xaalis:xaalis
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-m", "xaalis.infra.healthcheck"]

CMD ["uvicorn", "xaalis.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
