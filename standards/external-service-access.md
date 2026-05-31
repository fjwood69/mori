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
git push https://<user>:${GITHUB_TOKEN}@github.com/<user>/<repo>.git <branch>
```

Use branch name, not just `main` — confirm current branch first.

## cc-share

```bash
KEY=$(~/bin/get-secret.sh CC_SHARE_API_KEY)
CC_SHARE_URL=$(~/bin/get-secret.sh CC_SHARE_URL)
curl -s "${CC_SHARE_URL}/cc-share/?prefix=" -H "X-Api-Key: $KEY"
curl -s -X POST "${CC_SHARE_URL}/cc-share/<key>" \
  -H "X-Api-Key: $KEY" -d '<value>'
```

## NATS

Use MCP tools, not the nats CLI:

- `mori-nats_ping` — check connectivity
- `mori-nats_sub` — read recent messages (pass `replay=true` for history)
- `mori-nats_pub` — publish a message

## Remote SSH

Credentials are in `~/.claude/.secrets`. Read the host, user, and password,
then connect:

```bash
PASS=$(~/bin/get-secret.sh REMOTE_<HOST>_PASSWORD)
USER=$(~/bin/get-secret.sh REMOTE_<HOST>_USER)
HOST=$(~/bin/get-secret.sh REMOTE_<HOST>_HOST)
sshpass -p "$PASS" ssh "${USER}@${HOST}" <command>
```
