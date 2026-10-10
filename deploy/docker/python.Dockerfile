# syntax=docker/dockerfile:1.7
# One image for every Python service and tool in the workspace; each container picks its entry point
# (stream-processor, risk-service, care-api, agent-service, rpm-sim, rpm-seed, rpm-eval, ...).
FROM ghcr.io/astral-sh/uv:0.9.28 AS uv

FROM python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY libs libs
COPY services services
COPY simulator simulator
COPY tools tools
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --all-packages

FROM python:3.12-slim
LABEL org.opencontainers.image.title="rpm-platform" org.opencontainers.image.source="rpm-platform"
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home app && install -d -o app /home/app/data
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001
WORKDIR /home/app
