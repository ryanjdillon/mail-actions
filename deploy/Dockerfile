# Build tooling is pinned: base image and uv by digest, dependencies by
# python/mail_actions/uv.lock, the build backend in pyproject.toml.
ARG PYTHON_IMAGE=python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

FROM ghcr.io/astral-sh/uv:0.12.3@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY python/mail_actions/pyproject.toml python/mail_actions/uv.lock ./
# Dependencies first, exactly as locked, so this layer caches across code changes.
RUN uv sync --frozen --no-dev --no-install-project
COPY python/mail_actions/src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE}
ENV PYTHONUNBUFFERED=1 PATH=/app/.venv/bin:$PATH
RUN useradd --create-home --uid 1000 mail-actions
COPY --from=build /app/.venv /app/.venv
USER 1000
EXPOSE 9000
ENTRYPOINT ["mail-actions"]
CMD ["serve"]
