#!/usr/bin/env bash
# Add an API key to a mori server — v2.3.17, replacing the retired key_generate MCP tool.
#
#   scripts/mori-key-add.sh --host <user@server> [--env-file PATH] <name> <read|write|dreamer>
#   scripts/mori-key-add.sh --dry-run <name> <role>     # print the remote payload; nothing is sent
#
# Run it yourself, in your own terminal. An agent must never see a key it could use (R9), so:
#   - it refuses to run under Claude Code (CLAUDECODE set) or without a terminal (except --dry-run);
#   - the secret is generated here; the SERVER receives only name:sha256:<hex> and name:<role>, on
#     ssh's stdin (never on a command line);
#   - the secret is shown once on this terminal, then the screen and scrollback are cleared.
# The server's env file is replaced atomically (mode 600); no backup is written (it would hold the
# rest of the table). The key is live after the next restart of mori-advisor, mori-ingestion and
# mori-msg (or the next deploy).
set -euo pipefail

HOST="${MORI_KEY_ADD_HOST:-}"
ENVFILE=/data/mori-advisor/.env
DRY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --env-file) ENVFILE="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    --*) echo "unknown option $1" >&2; exit 2 ;;
    *) break ;;
  esac
done
NAME="${1:-}"; ROLE="${2:-}"
if [[ ! "$NAME" =~ ^[a-z0-9][a-z0-9-]{1,40}$ ]]; then
  echo "refused: the name must be lowercase letters, digits and hyphens" >&2; exit 2
fi
case "$ROLE" in read|write|dreamer) ;; *) echo "refused: role must be read, write or dreamer" >&2; exit 2 ;; esac
# The path reaches the remote command line: plain path characters only (consult C17, P3-5).
[[ "$ENVFILE" =~ ^[A-Za-z0-9/._-]+$ ]] || { echo "refused: --env-file must be a plain path" >&2; exit 2; }

if [[ -n "${CLAUDECODE:-}" ]]; then
  echo "Refusing: run this in your own terminal, not through Claude Code." >&2; exit 1
fi
if [[ $DRY -eq 0 ]] && { [[ ! -t 0 ]] || [[ ! -t 1 ]]; }; then
  echo "Refusing: run this in your own interactive terminal." >&2; exit 1
fi

SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
HASH=$(printf '%s' "$SECRET" | python3 -c "import hashlib,sys; print('sha256:' + hashlib.sha256(sys.stdin.read().encode()).hexdigest())")
PAYLOAD="$NAME:$HASH $NAME:$ROLE"

if [[ $DRY -eq 1 ]]; then
  unset SECRET
  printf '%s\n' "$PAYLOAD"
  exit 0
fi
[[ -n "$HOST" ]] || { echo "refused: --host user@server (or MORI_KEY_ADD_HOST) is required" >&2; exit 2; }

read -r -d '' PROG <<'PY' || true
import os, re, sys, tempfile
path = sys.argv[1]
entry, role_entry = sys.stdin.readline().split()
name = entry.split(":", 1)[0]
if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}:sha256:[0-9a-f]{64}", entry):
    sys.exit("bad payload; nothing changed")
lines = open(path).read().split("\n")
def idx(var):
    hits = [i for i, l in enumerate(lines) if re.match(rf"\s*{var}\s*=", l)]
    if len(hits) != 1:
        sys.exit(f"expected one {var} line, found {len(hits)}; nothing changed")
    return hits[0]
def names(line):
    return [e.split(":", 1)[0].strip() for e in line.split("=", 1)[1].split(",") if e.strip()]
ki, ri = idx("MORI_API_KEYS"), idx("MORI_API_KEY_ROLES")
if name in names(lines[ki]) or name in names(lines[ri]):
    sys.exit(f"{name} is already present; nothing changed")
sep = lambda l: "" if l.rstrip().endswith("=") else ","
lines[ki] = lines[ki].rstrip() + sep(lines[ki]) + entry
lines[ri] = lines[ri].rstrip() + sep(lines[ri]) + role_entry
st = os.stat(path)
fd, tmp = tempfile.mkstemp(prefix=".mori-key-add.", dir=os.path.dirname(os.path.abspath(path)))
os.fchmod(fd, 0o600)
with os.fdopen(fd, "w") as f:
    f.write("\n".join(lines))
os.chown(tmp, st.st_uid, st.st_gid)
os.replace(tmp, path)
print("added:", name, "| keys:", ", ".join(names(lines[ki])))
PY
B64=$(printf '%s' "$PROG" | base64 -w0)
printf '%s\n' "$PAYLOAD" | ssh "$HOST" "sudo -n python3 -c \"\$(echo $B64 | base64 -d)\" '$ENVFILE'"

echo
echo "The secret for '$NAME' (shown once; save it now):"
echo
echo "    $SECRET"
echo
read -r -p "Press Enter once it is saved; the screen and scrollback will be cleared. " _
unset SECRET
clear
printf '\033[3J'
echo "Done. '$NAME' ($ROLE) is live after the next restart or deploy."
