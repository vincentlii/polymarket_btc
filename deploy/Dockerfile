# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.11.19 AS uv

FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=uv /uv /uvx /bin/

RUN apt-get update \
    && apt-get install --yes --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-editable

COPY . ./
RUN uv sync --locked --no-dev --no-editable \
    && chown -R app:app /app

ARG BTC_CODE_REVISION
ENV BTC_CODE_REVISION=${BTC_CODE_REVISION}
LABEL org.opencontainers.image.revision=${BTC_CODE_REVISION}

USER app
