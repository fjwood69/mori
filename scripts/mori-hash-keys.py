#!/usr/bin/env python3
"""Convert an env file's MORI_API_KEYS to hashed entries, in place (v2.3.17, H2).

    python3 scripts/mori-hash-keys.py /data/mori-advisor/.env

Each ``name:secret`` becomes ``name:sha256:<64 lowercase hex>`` (sha256 of the secret). Entries that
are already hashed are kept as they are, so a second run changes nothing. Every other line is kept
byte for byte. The file is replaced atomically with mode 600, and NO plain backup is written: the
point is that no copy of the plain table stays on the host. Fred keeps the one off-host copy that a
rollback to v2.3.16 needs.

Refuses (exit 2, file unchanged) if the legacy single mori key MORI_ADVISOR_API_KEY is set in the
file: v2.3.17 in api mode would refuse to start on it, and it would stay a plain key. (MORI_API_KEY is
NOT a mori client key — it is the LLM provider's key in direct mode, bifrost_client.py — and is left
alone.)

Prints names and counts only, never a secret. Standard library only.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile

KEYS_LINE = re.compile(r"^(\s*(?:export\s+)?MORI_API_KEYS\s*=)(.*)$")
LEGACY = re.compile(r"^\s*(?:export\s+)?(MORI_ADVISOR_API_KEY)\s*=\s*(\S.*)?$")
HASHED = re.compile(r"sha256:[0-9a-f]{64}")
# Any OTHER line naming a mori key table (commented out, renamed, exported copy) that still holds a
# plain 64-hex secret would leave a usable key in the file after conversion (consult C17, P2-1).
# MORI_API_KEY (the LLM provider key) is not a mori key and is not matched.
STRAY = re.compile(
    r"^\s*(?:#\s*)?(?:export\s+)?(MORI_API_KEYS\w*|MORI_ADVISOR_API_KEY\w*)\s*=(.*)$"
)
PLAIN_HEX = re.compile(r"(?<!sha256:)\b[0-9a-f]{64}\b")


def convert_line(value: str) -> tuple[str, list[str], list[str]]:
    """Return (new value, names converted, names already hashed)."""
    out, converted, kept = [], [], []
    for entry in (e.strip() for e in value.split(",")):
        if not entry:
            continue
        if ":" not in entry:
            raise ValueError("an entry without ':' (name:secret) — refusing to guess")
        name, secret = entry.split(":", 1)
        name, secret = name.strip(), secret.strip()
        if HASHED.fullmatch(secret):
            out.append(f"{name}:{secret}")
            kept.append(name)
        else:
            out.append(f"{name}:sha256:{hashlib.sha256(secret.encode('utf-8')).hexdigest()}")
            converted.append(name)
    return ",".join(out), converted, kept


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: mori-hash-keys.py <env file>", file=sys.stderr)
        return 2
    path = argv[1]
    with open(path, encoding="utf-8") as f:
        text = f.read()
    lines = text.split("\n")

    legacy = sorted({m.group(1) for ln in lines if (m := LEGACY.match(ln)) and m.group(2)})
    if legacy:
        print(
            f"refused: legacy mori key variable set: {', '.join(legacy)}. Remove it first "
            "(v2.3.17 in api mode refuses a plain key). Nothing changed.",
            file=sys.stderr,
        )
        return 2

    hits = [i for i, ln in enumerate(lines) if KEYS_LINE.match(ln)]
    if len(hits) != 1:
        print(f"refused: expected one MORI_API_KEYS line, found {len(hits)}.", file=sys.stderr)
        return 2
    i = hits[0]
    stray = sorted(
        {
            m.group(1)
            for j, ln in enumerate(lines)
            if j != i and (m := STRAY.match(ln)) and PLAIN_HEX.search(m.group(2))
        }
    )
    if stray:
        print(
            f"refused: another line holds a plain key table ({', '.join(stray)} — commented out, "
            "renamed or copied). Remove it first: it would stay a usable key. Nothing changed.",
            file=sys.stderr,
        )
        return 2
    head, value = KEYS_LINE.match(lines[i]).groups()  # type: ignore[union-attr]
    try:
        new_value, converted, kept = convert_line(value)
    except ValueError as e:
        print(f"refused: {e}. Nothing changed.", file=sys.stderr)
        return 2
    if not converted:
        os.chmod(path, 0o600)
        print(f"already hashed: {len(kept)} entr{'y' if len(kept) == 1 else 'ies'}; no change.")
        return 0
    lines[i] = head + new_value

    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".mori-hash-keys.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    print(
        f"hashed: {', '.join(converted)}" + (f"; already hashed: {', '.join(kept)}" if kept else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
