# syntax=docker/dockerfile:1.7
# One Dockerfile for every Python service; choose the workspace package with --build-arg PACKAGE=<name>.
FROM ghcr.io/astral-sh/uv:0.9.28 AS uv

FROM python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY libs libs
COPY services services
COPY simulator simulator
ARG PACKAGE
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package "${PACKAGE}"

FROM python:3.12-slim
ARG PACKAGE
LABEL org.opencontainers.image.title="rpm-${PACKAGE}" org.opencontainers.image.source="rpm-platform"
RUN useradd --uid 10001 --create-home app
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001
WORKDIR /home/app
