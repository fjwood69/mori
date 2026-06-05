# Privacy page (moriapp.dev/privacy)

Static privacy policy for the Mori plugin, served by a Cloudflare Worker.

## Deploy
```bash
# Requires: CLOUDFLARE_API_KEY (Workers scope), CLOUDFLARE_ACCOUNT_ID, ZONE_ID for moriapp.dev
curl -X PUT \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/workers/scripts/mori-privacy" \
  -H "Authorization: Bearer $CLOUDFLARE_API_KEY" \
  -H "Content-Type: application/javascript" \
  --data-binary @worker.js

# One-time: route the path to the worker
curl -X POST \
  "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/workers/routes" \
  -H "Authorization: Bearer $CLOUDFLARE_API_KEY" \
  -H "Content-Type: application/json" \
  --data '{"pattern":"moriapp.dev/privacy*","script":"mori-privacy"}'
```
