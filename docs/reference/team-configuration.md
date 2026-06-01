# Team Configuration — PostgreSQL Backend

Mori ships with SQLite as the default backend. For solo use and single-pod
deployments SQLite is the right choice — zero dependencies, WAL mode, fast.

PostgreSQL activates when `MORI_DATABASE_URL` is set. Use it when you need:
- Concurrent dream runs (multiple pods writing simultaneously)
- PITR backups via WAL-G
- Multi-region read replicas

If you're running one pod per team, stay on SQLite.

---

## Switching to PostgreSQL

### 1. Start PostgreSQL and pgBouncer

```bash
# Minimal — no pgBouncer (single pod)
docker run -d --name mori-postgres \
  -e POSTGRES_DB=mori \
  -e POSTGRES_USER=mori \
  -e POSTGRES_PASSWORD=<password> \
  postgres:16

# With pgBouncer (recommended for multi-pod)
# See deploy/team/docker-compose.yml
docker compose -f deploy/team/docker-compose.yml up -d postgres pgbouncer
```

### 2. Export from SQLite

```bash
python -m mori_advisor.cli.export \
  --db /data/mori-advisor/memories.db \
  --output /tmp/mori-export.jsonl \
  --dry-run    # preview row counts first

python -m mori_advisor.cli.export \
  --db /data/mori-advisor/memories.db \
  --output /tmp/mori-export.jsonl
```

Session events default to the last 90 days. Use `--all` to include full history.

### 3. Import to PostgreSQL

```bash
MORI_DATABASE_URL=postgresql://mori:<password>@localhost:5432/mori \
  python -m mori_advisor.cli.import_ /tmp/mori-export.jsonl
```

Import is idempotent — `ON CONFLICT DO NOTHING` — safe to re-run if interrupted.

### 4. Verify counts

The script below connects directly to each backend for verification — this is a
one-off check, not how the app normally connects (which uses `get_store()`).

```python
import asyncio, sqlite3
from mori_advisor.store.postgres_store import PostgresStore

sqlite_count = sqlite3.connect("/data/mori-advisor/memories.db") \
    .execute("SELECT COUNT(*) FROM memories").fetchone()[0]

pg = PostgresStore("postgresql://mori:<password>@localhost:5432/mori")
asyncio.run(pg.connect())
pg_count = asyncio.run(pg.count())

print(f"SQLite: {sqlite_count}  Postgres: {pg_count}")
assert sqlite_count == pg_count, "count mismatch — do not cut over"
```

### 5. Start Mori with PostgreSQL

Add to your `.env`:

```bash
MORI_DATABASE_URL=postgresql://mori:<password>@pgbouncer:5433/mori
```

Or if connecting directly (no pgBouncer):

```bash
MORI_DATABASE_URL=postgresql://mori:<password>@localhost:5432/mori
```

Restart the container. Mori will use PostgreSQL automatically.

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MORI_DATABASE_URL` | *(unset)* | PostgreSQL DSN. Unset or empty = SQLiteStore. |
| `MORI_ADVISOR_DATA` | `/data/mori-advisor` | SQLite data directory (ignored when Postgres active). |

---

## pgBouncer

Use pgBouncer in **session mode** when connecting from Mori. Mori uses asyncpg
with `statement_cache_size=0`, which is required for pgBouncer compatibility.

```ini
; pgbouncer.ini
[databases]
mori = host=postgres port=5432 dbname=mori

[pgbouncer]
listen_port = 5433
pool_mode = session
max_client_conn = 100
default_pool_size = 20
```

Transaction mode is not supported — Mori's dream pipeline wraps multiple
statements in a single transaction that must stay on one connection.

---

## WAL-G backups (optional)

For PITR, configure WAL-G with a GCS or S3 bucket:

```bash
# In your Postgres container environment
WALG_GS_PREFIX=gs://<bucket>/mori-advisor
GOOGLE_APPLICATION_CREDENTIALS=/secrets/gcp-key.json
```

```bash
# Manual backup
wal-g backup-push /var/lib/postgresql/data

# List backups
wal-g backup-list

# Restore
wal-g backup-fetch /var/lib/postgresql/data LATEST
```

Set the GCS prefix to match your deployment name so backup continuity is
maintained if you rename the data directory.

---

## Rollback to SQLite

If Postgres is unhealthy, remove or comment out `MORI_DATABASE_URL` from `.env`
and restart. Mori falls back to SQLiteStore immediately — the SQLite file is
untouched throughout the Postgres migration. Removing the line and leaving it
unset are equivalent; there is no broken intermediate state.

```bash
# Remove the variable and restart
sed -i '/MORI_DATABASE_URL/d' /data/mori-advisor/.env
docker restart mori-advisor
curl http://localhost:8968/ready   # should be 200
```

---

## asyncpg dependency

asyncpg is not installed by default. Add it when deploying with PostgreSQL:

```bash
pip install asyncpg>=0.29.0
```

Or uncomment the line in `requirements.txt` before building your image.
