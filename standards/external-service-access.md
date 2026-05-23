# External Service Access

Standard patterns for accessing external services. Always use these exact
patterns — they avoid the multiple-try loop that happens when guessing.

## Secrets

All credentials are in `~/.claude/.secrets`. Read via:

```bash
VALUE=$(~/bin/get-secret.sh KEY_NAME)
```

`get-secret.sh` reads from `.secrets` with a fallback to `~/.claude/.secrets`.
It writes nothing to stdout other than the value. No quoting issues.

## GitHub

**Wrong:** `gh` CLI, SSH keys, guessing token var names.

**Right:** Read the token, construct the push URL, push:

```bash
GITHUB_TOKEN=$(~/bin/get-secret.sh GITHUB_TOKEN)
git push https://fjwood69:${GITHUB_TOKEN}@github.com/fjwood69/<repo>.git <branch>
```

The token is in `.secrets` as `GITHUB_TOKEN`. It works for all fjwood69 repos.
Use branch name, not just `main` — confirm current branch first.

## cc-share

```bash
KEY=$(~/bin/get-secret.sh CC_SHARE_API_KEY)
curl -s http://100.90.219.111:8999/cc-share/?prefix= -H "X-Api-Key: $KEY"
curl -s -X POST http://100.90.219.111:8999/cc-share/<key> \
  -H "X-Api-Key: $KEY" -d '<value>'
```

## NATS

Use MCP tools, not the nats CLI:

- `mori-nats_ping` — check connectivity
- `mori-nats_sub` — read recent messages (pass `replay=true` for history)
- `mori-nats_pub` — publish a message

## Grafana (local)

```bash
PASS=$(~/bin/get-secret.sh GF_SECURITY_ADMIN_PASSWORD)
curl -u "admin:$PASS" http://10.1.2.202:3000/api/...
```

Use `10.1.2.202` not `localhost` (pasta networking quirk).

## Prometheus

```bash
PASS=$(~/bin/get-secret.sh PROMETHEUS_ADMIN_PASSWORD)
curl -u "admin:$PASS" http://10.1.2.202:9090/api/v1/...
```

## Remote SSH

All Pi credentials are prefixed by hostname in `.secrets`. Pattern:

| Host | `.secrets` prefix | Host IP |
|------|--------------------|---------|
| ca-ws-raspi5 | `REMOTE_RASPI5B_` | 100.117.216.45 |
| uk-ga-raspi5 | `REMOTE_UKGA_` | 100.119.3.81 |
| uk-smr-raspi4b | `REMOTE_RASPI4B_` | 10.1.2.222 |
| uk-smr-jetson | `REMOTE_JETSON_` | 10.1.2.214 |

Example:
```bash
PASS=$(~/bin/get-secret.sh REMOTE_RASPI5B_PASSWORD)
sshpass -p "$PASS" ssh piadmin@100.117.216.45 <command>
```

## Bifrost DB (sqlite3 on GCE)

```bash
ssh -t jadmin@100.77.207.77 "sqlite3 /data/bifrost/config.db \"SELECT ...\""
```
