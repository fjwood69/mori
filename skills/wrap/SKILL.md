---
name: wrap
description: Session wrap — summarise session, publish to cc-share and NATS, then dream
---

## 1. Identify

Read `~/.config/mori/identity.yaml` for the `name` field. Note current hostname.

## 2. Summarise

Write a concise session summary covering:
- What was done (key changes, decisions, fixes)
- What's pending / next steps
- Any gotchas or notes for the next session

Keep it brief — bullet points, not prose.

## 3. Publish to cc-share

Read the URL and key from secrets. Skip this step silently if either is absent.

```bash
CC_SHARE_URL=$(~/bin/get-secret.sh CC_SHARE_URL 2>/dev/null)
KEY=$(~/bin/get-secret.sh CC_SHARE_API_KEY 2>/dev/null)
DATE=$(date +%Y-%m-%d)
if [ -n "$CC_SHARE_URL" ] && [ -n "$KEY" ]; then
  curl -s -X POST "${CC_SHARE_URL}/cc-share/session-${DATE}" \
    -H "X-Api-Key: $KEY" \
    -H "Content-Type: application/json" \
    -d "{\"key\":\"session-${DATE}\",\"value\":\"<summary from step 2>\",\"ttl_seconds\":604800}"
fi
```

Set TTL to 604800 (7 days) so it persists across the week.

## 4. Publish to NATS

Publish a one-line wrap message: `mori_nats_pub` with message `"<name> on <hostname>: <one-line summary of session>"`.

## 5. Dream

Call `mori_dream_run` to flush any undreamed events.

## 6. Sign off

Report what was written and where. No autonomous actions after.
