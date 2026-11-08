#!/usr/bin/env python3
"""Convert an env file's MORI_API_KEYS to hashed entries, in place — optional (v2.3.17).

    python3 scripts/mori-hash-keys.py [--include-first] /data/mori-advisor/.env

Hashing is supported and recommended for production deployments; it is an operator's choice, and nothing in the
deploy runs this script.

**The first entry stays plain by default.** It is the deploy contract's identity: the GCE CD template reads the
first entry's secret from the env file (``grep | cut``), so a hashed first entry makes every contract probe fail
(the cut yields the literal ``sha256``). The output names the entry it left plain. ``--include-first`` converts it
too, with a warning: any deploy that reads the first entry from the env file will then fail.

Every other ``name:secret`` becomes ``name:sha256:<64 lowercase hex>`` (sha256 of the secret). Entries already
hashed are kept, so a second run changes nothing. Every other line is kept byte for byte. The file is replaced
atomically, keeping its owner and group, with mode 600; no plain backup is written. v2.3.16 and earlier can't read
hashes, so keep an off-host copy of the plain table if you may roll back.

Refuses (exit 2, file unchanged) if the legacy single mori key MORI_ADVISOR_API_KEY is set, or another line still
holds a plain key table (commented out, renamed or copied). MORI_API_KEY is NOT a mori client key — it is the LLM
provider's key in direct mode (bifrost_client.py) — and is left alone.

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


def convert_line(
    value: str, include_first: bool = False
) -> tuple[str, list[str], list[str], str | None]:
    """Return (new value, names converted, names already hashed, the first entry left plain or None)."""
    out, converted, kept = [], [], []
    left_plain: str | None = None
    for idx, entry in enumerate(e.strip() for e in value.split(",") if e.strip()):
        if not entry:
            continue
        if ":" not in entry:
            raise ValueError("an entry without ':' (name:secret) — refusing to guess")
        name, secret = entry.split(":", 1)
        name, secret = name.strip(), secret.strip()
        if HASHED.fullmatch(secret):
            out.append(f"{name}:{secret}")
            kept.append(name)
        elif idx == 0 and not include_first:
            out.append(f"{name}:{secret}")  # the deploy contract's identity stays plain
            left_plain = name
        else:
            out.append(f"{name}:sha256:{hashlib.sha256(secret.encode('utf-8')).hexdigest()}")
            converted.append(name)
    return ",".join(out), converted, kept, left_plain


def main(argv: list[str]) -> int:
    args = argv[1:]
    include_first = "--include-first" in args
    args = [a for a in args if a != "--include-first"]
    if len(args) != 1 or args[0].startswith("-"):
        print("usage: mori-hash-keys.py [--include-first] <env file>", file=sys.stderr)
        return 2
    path = args[0]
    with open(path, encoding="utf-8") as f:
        text = f.read()
    lines = text.split("\n")

    legacy = sorted({m.group(1) for ln in lines if (m := LEGACY.match(ln)) and m.group(2)})
    if legacy:
        print(
            f"refused: legacy mori key variable set: {', '.join(legacy)}. Remove it first "
            "(it would stay a plain key). Nothing changed.",
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
        new_value, converted, kept, left_plain = convert_line(value, include_first)
    except ValueError as e:
        print(f"refused: {e}. Nothing changed.", file=sys.stderr)
        return 2
    if left_plain:
        print(f"kept plain (the deploy contract's identity, the first entry): {left_plain}")
    if include_first and converted:
        print(
            "warning: --include-first hashed the first entry too — any deploy that reads the first "
            "MORI_API_KEYS entry from the env file (the GCE CD template does) will now fail.",
            file=sys.stderr,
        )
    if not converted:
        os.chmod(path, 0o600)
        print(f"nothing to convert ({len(kept)} already hashed); no change.")
        return 0
    lines[i] = head + new_value

    directory = os.path.dirname(os.path.abspath(path))
    st = os.stat(path)
    fd, tmp = tempfile.mkstemp(prefix=".mori-hash-keys.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        # Keep the owner and group: run under sudo, a root-owned file would stop rootless units reading it.
        os.fchown(fd, st.st_uid, st.st_gid)
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
