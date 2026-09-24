# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build

COPY pyproject.toml README.md LICENSE ./
COPY certs ./certs
COPY src ./src

RUN python -m pip install build==1.3.0 \
    && python -m build --wheel --outdir /dist


FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VMANAGE_WEB_STATE_DIR=/var/lib/cisco-vmanage-mcp

RUN groupadd --gid 10001 vmanage \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin vmanage \
    && install -d -o 10001 -g 10001 -m 0700 /var/lib/cisco-vmanage-mcp

COPY --from=builder /dist/*.whl /tmp/
RUN python -m pip install /tmp/*.whl \
    && rm -f /tmp/*.whl

USER 10001:10001
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health/live', timeout=2).read()"]

ENTRYPOINT ["vmanage-web"]
CMD ["--host", "0.0.0.0", "--port", "8765"]