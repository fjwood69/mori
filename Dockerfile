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
COPY moku_advisor/ ./moku_advisor/

# Data directory mounted from host, create for ownership
RUN mkdir -p /data/moku-advisor && chown -R appuser:appuser /data/moku-advisor

# Switch to non-privileged user
USER appuser

EXPOSE 8968

CMD ["python", "-m", "moku_advisor.main"]