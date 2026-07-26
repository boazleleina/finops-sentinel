# Stage 1: Build dependencies
FROM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir .

# Stage 2: Runtime
FROM python:3.11-slim

RUN useradd -m -U sentineluser
USER sentineluser

WORKDIR /app

COPY --from=builder --chown=sentineluser:sentineluser /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["sentinel", "serve", "--host", "0.0.0.0"]
