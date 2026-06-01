#!/usr/bin/env python3
"""Helpers for install-mori-antigravity.sh — MCP merge, hooks merge, skills, doctor."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HOOK_EVENTS = (
    "PostToolUse",
    "PostToolUseFailure",
    "UserPromptSubmit",
    "Stop",
    "PreCompact",
)


def _is_mori_hook(entry: dict[str, Any]) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("_mori_managed") is True:
        return True
    cmd = entry.get("command", "")
    return "mori-ship-event" in cmd or "/api/events/raw" in cmd or "/api/precompact" in cmd


def _hook_command(shipper: str, url: str, client: str, api_key: str, mode: str) -> str:
    if shipper.lower().endswith(".ps1"):
        api_flag = f' -ApiKey "{api_key}"' if api_key else ""
        return (
            f'powershell -NoProfile -ExecutionPolicy Bypass -File "{shipper}" '
            f'-MoriUrl "{url}" -Client "{client}"{api_flag} -Mode {mode}'
        )
    parts = [f'"{shipper}"', f'--url "{url}"', f'--client "{client}"']
    if api_key:
        parts.append(f'--api-key "{api_key}"')
    parts.append(f"--mode {mode}")
    return " ".join(parts)


def _update_entry_command(entry: dict[str, Any], new_cmd: str) -> bool:
    if "hooks" in entry and isinstance(entry["hooks"], list):
        for h in entry["hooks"]:
            if isinstance(h, dict) and h.get("type") == "command" and _is_mori_hook(h):
                h["command"] = new_cmd
                h["_mori_managed"] = True
                return True
        entry["hooks"].insert(0, {"type": "command", "command": new_cmd, "_mori_managed": True})
        return True
    if entry.get("type") == "command" and _is_mori_hook(entry):
        entry["command"] = new_cmd
        entry["_mori_managed"] = True
        return True
    return False


def _merge_hook_list(existing: list[Any], new_cmd: str) -> list[Any]:
    if not isinstance(existing, list):
        existing = []

    updated = False
    for entry in existing:
        if isinstance(entry, dict):
            if _update_entry_command(entry, new_cmd):
                updated = True

    if not updated:
        existing.insert(0, {"type": "command", "command": new_cmd, "_mori_managed": True})
    return existing


def merge_hooks(
    hooks_path: Path,
    shipper: str,
    mori_url: str,
    client: str,
    api_key: str,
) -> None:
    """Merge Mori shipper hooks into hooks.json."""
    raw_cmd = _hook_command(shipper, mori_url, client, api_key, "raw")
    precompact_cmd = _hook_command(shipper, mori_url, client, api_key, "precompact")

    if hooks_path.is_file() and hooks_path.stat().st_size > 0:
        with hooks_path.open(encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}

    hooks = data.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        cmd = precompact_cmd if event == "PreCompact" else raw_cmd
        hooks[event] = _merge_hook_list(hooks.get(event, []), cmd)

    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    with hooks_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def merge_mcp(mcp_path: Path, mori_url: str, api_key: str = "") -> None:
    mori_server = {"type": "http", "serverUrl": f"{mori_url.rstrip('/')}/mcp"}
    if api_key:
        mori_server["headers"] = {"X-Api-Key": api_key}
    if mcp_path.is_file() and mcp_path.stat().st_size > 0:
        with mcp_path.open(encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}
    servers = data.setdefault("mcpServers", {})
    servers["mori"] = mori_server
    mcp_path.parent.mkdir(parents=True, exist_ok=True)
    with mcp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _parse_skill_md(path: Path) -> tuple[str, str, list[str]]:
    content = path.read_text(encoding="utf-8")
    name = ""
    desc = ""
    body_lines: list[str] = []

    lines = content.splitlines()
    for line in lines:
        name_match = re.match(r"^(?:-\s+)?name:\s*(.*)$", line)
        desc_match = re.match(r"^(?:-\s+)?description:\s*(.*)$", line)
        if name_match and not name:
            name = name_match.group(1).strip()
        elif desc_match and not desc:
            desc = desc_match.group(1).strip().strip('"')

    if len(lines) > 0 and lines[0].strip() == "---":
        end_idx = -1
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_idx = i
                break
        if end_idx != -1:
            body_lines = lines[end_idx + 1 :]
        else:
            body_lines = lines
    else:
        in_body = False
        for line in lines:
            if not in_body:
                if re.match(r"^(?:-\s+)?(?:name|description):\s*", line) or line.strip() == "---":
                    continue
                if not line.strip():
                    continue
                in_body = True
            body_lines.append(line)

    if not name:
        if path.stem in ("SKILL", "skill"):
            name = path.parent.name
        else:
            name = path.stem.replace(".skill", "")

    return name, desc, body_lines


def deploy_skills(source_dir: Path, dest_dir: Path, upgrade: bool) -> int:
    if not source_dir.is_dir():
        print(f"  Warning: skills source not found: {source_dir}", file=sys.stderr)
        return 0

    dest_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    paths = list(source_dir.glob("**/SKILL.md")) + list(source_dir.glob("**/*.skill.md"))
    unique_paths = []
    seen = set()
    for p in paths:
        if p.is_file():
            resolved = p.resolve()
            if resolved not in seen:
                seen.add(resolved)
                unique_paths.append(p)
    for path in sorted(unique_paths):
        name, desc, body_lines = _parse_skill_md(path)
        folder = dest_dir / f"mori-{name}"
        skill_file = folder / "SKILL.md"
        exists = skill_file.exists()
        if exists and not upgrade:
            print(f"  Skipped existing skill: mori-{name} (use --upgrade-skills to overwrite)")
            continue
        folder.mkdir(parents=True, exist_ok=True)
        escaped = desc.replace('"', '\\"')
        content = "\n".join(body_lines).rstrip() + "\n"
        skill_file.write_text(
            f'---\nname: mori-{name}\ndescription: "{escaped}"\n---\n\n{content}',
            encoding="utf-8",
        )
        if exists:
            print(f"  Overwrote existing skill: mori-{name}")
        else:
            print(f"  Deployed new skill: mori-{name}")
        count += 1
    return count


def _http_get(url: str, timeout: int = 5) -> tuple[int, str]:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")[:500]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")[:500]
    except OSError as e:
        return 0, str(e)


def doctor(mori_url: str | None, client: str) -> int:
    home = Path.home()
    mcp_path = home / ".gemini" / "antigravity" / "mcp_config.json"
    hooks_path = home / ".gemini" / "config" / "hooks.json"
    skills_dir = home / ".gemini" / "config" / "plugins" / "mori-bridge" / "skills"
    errors = 0

    print("--- Mori Antigravity IDE doctor ---\n")

    if mcp_path.is_file():
        print(f"OK  MCP config: {mcp_path}")
        try:
            data = json.loads(mcp_path.read_text(encoding="utf-8"))
            url = data.get("mcpServers", {}).get("mori", {}).get("serverUrl", "") or data.get(
                "mcpServers", {}
            ).get("mori", {}).get("url", "")
            if url:
                print(f"    mori URL: {url}")
                mori_url = mori_url or url.removesuffix("/mcp")
            else:
                print("FAIL  mcp_config.json missing mcpServers.mori.serverUrl")
                errors += 1
        except Exception as e:
            print(f"FAIL  Could not parse mcp_config.json: {e}")
            errors += 1
    else:
        print(f"FAIL  MCP config missing: {mcp_path}")
        errors += 1

    if mori_url:
        code, body = _http_get(f"{mori_url.rstrip('/')}/health")
        if code == 200 and "ok" in body:
            print(f"OK  Server health: {mori_url}/health")
        else:
            print(f"FAIL  Server health ({code}): {mori_url}/health — {body[:120]}")
            errors += 1

        code, body = _http_get(f"{mori_url.rstrip('/')}/api/events/health")
        if code == 200:
            print(f"OK  Events endpoint: {body.strip()[:80]}")
        else:
            print(f"WARN  Events health ({code})")
    else:
        print("SKIP  Server checks (no URL)")
        errors += 1

    if hooks_path.is_file():
        text = hooks_path.read_text(encoding="utf-8")
        if "mori-ship-event" in text or "/api/events/raw" in text:
            print(f"OK  Event hooks present in {hooks_path}")
        else:
            print(f"WARN  No Mori hooks in {hooks_path}")
            errors += 1
    else:
        print(f"FAIL  hooks.json missing: {hooks_path}")
        errors += 1

    mori_skills = list(skills_dir.glob("mori-*/SKILL.md")) if skills_dir.is_dir() else []
    if mori_skills:
        print(f"OK  Skills: {len(mori_skills)} mori-* skills deployed under {skills_dir}")
    else:
        print(f"WARN  No mori-* skills under {skills_dir}")

    print(f"\nClient tag for events: {client}")
    print("\nReminder: shared memory lives on the Mori server (GCE/homelab), not on this machine.")
    if errors:
        print(f"\nDoctor: {errors} check(s) failed.")
        return 1
    print("\nDoctor: all critical checks passed. Restart/reload your IDE if needed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Mori Antigravity install helpers")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_mcp = sub.add_parser("merge-mcp")
    p_mcp.add_argument("--mcp-path", required=True)
    p_mcp.add_argument("--url", required=True)
    p_mcp.add_argument("--api-key", default="")

    p_set = sub.add_parser("merge-hooks")
    p_set.add_argument("--hooks-path", required=True)
    p_set.add_argument("--shipper", required=True)
    p_set.add_argument("--url", required=True)
    p_set.add_argument("--client", required=True)
    p_set.add_argument("--api-key", default="")

    p_sk = sub.add_parser("deploy-skills")
    p_sk.add_argument("--source", required=True)
    p_sk.add_argument("--dest", required=True)
    p_sk.add_argument("--upgrade", action="store_true")

    p_doc = sub.add_parser("doctor")
    p_doc.add_argument("--url", default="")
    p_doc.add_argument("--client", default="antigravity-ide")

    args = parser.parse_args()

    if args.cmd == "merge-mcp":
        merge_mcp(Path(args.mcp_path), args.url, args.api_key)
        return 0
    if args.cmd == "merge-hooks":
        merge_hooks(
            Path(args.hooks_path),
            args.shipper,
            args.url,
            args.client,
            args.api_key,
        )
        return 0
    if args.cmd == "deploy-skills":
        n = deploy_skills(Path(args.source), Path(args.dest), args.upgrade)
        if n == 0 and not args.upgrade:
            print(
                "  No new skills deployed (all present skills skipped; use --upgrade-skills to overwrite/update existing skills)"
            )
        return 0
    if args.cmd == "doctor":
        url = args.url or None
        return doctor(url, args.client)
    return 1


if __name__ == "__main__":
    sys.exit(main())
