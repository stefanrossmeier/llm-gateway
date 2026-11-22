FROM ghcr.io/astral-sh/uv:0.12.13@sha256:b485bd65cc2cf1c9a93b3554012c9c3778cf7b1b5fd3d3096ce9e1226c97e1e6 AS uv
FROM python:3.12.16-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
# Install only locked wheels before building our own authored package.
RUN uv sync --frozen --extra service --no-install-project --no-build \
    && uv sync --frozen --extra service --no-editable --no-build-isolation \
    && uv sync --frozen --extra service --no-dev --no-editable --no-build-isolation

FROM python:3.12.16-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
LABEL org.opencontainers.image.source="https://github.com/stefanrossmeier/llm-gateway" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.title="llm-gateway"
COPY deploy/install_security_updates.sh /tmp/install-security-updates.sh
RUN sh /tmp/install-security-updates.sh && rm /tmp/install-security-updates.sh
RUN rm -rf /usr/local/lib/python3.12/site-packages/pip \
      /usr/local/lib/python3.12/site-packages/pip-*.dist-info \
      /usr/local/lib/python3.12/ensurepip /usr/local/bin/pip* \
    && groupadd --gid 10001 gateway \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /state gateway \
    && mkdir /state /config /run/gateway \
    && chown gateway:gateway /state /run/gateway \
    && chmod 700 /state /run/gateway
COPY --from=build /app/.venv /app/.venv
COPY LICENSE /app/LICENSE
ENV PATH=/app/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOME=/state TMPDIR=/state
USER 10001:10001
WORKDIR /state
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-I", "-m", "llm_gateway.service.cli"]
CMD ["serve", "--project-config", "/config/project.yaml", "--state-dir", "/state", "--unix-socket", "/run/gateway/gateway.sock"]
