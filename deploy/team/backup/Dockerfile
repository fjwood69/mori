# Postgres 16 + WAL-G
# Extends the official Postgres image with WAL-G for continuous WAL archiving.
# Used by both the `postgres` service (archive_command) and the `wal-g-cron`
# base-backup sidecar in docker-compose.yml.

FROM postgres:16

ARG WALG_VERSION=v3.0.3
ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN ARCH=$([ "$TARGETARCH" = "arm64" ] && echo "arm64" || echo "amd64") \
    && curl -fsSL \
       "https://github.com/wal-g/wal-g/releases/download/${WALG_VERSION}/wal-g-pg-ubuntu-20.04-${ARCH}.tar.gz" \
       | tar -xz -C /usr/local/bin/ \
    && chmod +x /usr/local/bin/wal-g-pg \
    && ln -s /usr/local/bin/wal-g-pg /usr/local/bin/wal-g

COPY wal-archive.sh /usr/local/bin/wal-archive
RUN chmod +x /usr/local/bin/wal-archive
