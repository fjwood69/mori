# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12.8

# =============================================================================
# Stage 1: Builder — Install dependencies into virtual env
# =============================================================================
FROM python:${PYTHON_VERSION}-alpine AS builder

ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# =============================================================================
# Stage 2: Runtime — Final lean image
# =============================================================================
FROM python:${PYTHON_VERSION}-alpine AS runtime

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH="/app:$PYTHONPATH"

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Create non-privileged user
ARG UID=10001
RUN adduser \
    --disabled-password \
    --gecos "" \
    --home "/nonexistent" \
    --shell "/sbin/nologin" \
    --no-create-home \
    --uid "${UID}" \
    appuser

# Copy source code
COPY mori_advisor/ ./mori_advisor/
COPY scripts/ ./scripts/

# Data directory mounted from host, create for ownership
RUN mkdir -p /data/mori-advisor && chown -R appuser:appuser /data/mori-advisor

# Switch to non-privileged user
USER appuser

EXPOSE 8968

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8968/health')" || exit 1

CMD ["python", "-m", "mori_advisor.main"]