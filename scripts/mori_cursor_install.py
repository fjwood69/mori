#!/usr/bin/env python3
"""Helpers for install-mori-cursor.sh — MCP merge, settings merge, skills, doctor."""

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
    "PostCompact",
)

# Claude-compat events wired by plugin --parity (native hooks.json covers the rest).
COMPAT_HOOK_EVENTS = ("PreCompact", "PostCompact")

# Legacy overlap removed when plugin native hooks are active (avoid duplicate telemetry).
NATIVE_COVERED_EVENTS = (
    "PostToolUse",
    "PostToolUseFailure",
    "UserPromptSubmit",
    "Stop",
)

PLUGIN_SKILL_NAMES = {
    "brief",
    "consult",
    "dream",
    "ingest",
    "msg",
    "nats",
    "pensieve",
    "req",
    "wrap",
}

MORI_MCP_ALLOW = [
    # Core session tools
    "mcp__mori__brief",
    "mcp__mori__pensieve",
    "mcp__mori__consult_advisor",
    "mcp__mori__consult_status",
    "mcp__mori__update",
    "mcp__mori__standards_reload",
    # Memory CRUD
    "mcp__mori__memory_list",
    "mcp__mori__memory_read",
    "mcp__mori__memory_search",
    "mcp__mori__memory_write",
    "mcp__mori__memory_req",
    "mcp__mori__memory_delete",
    # Memory management
    "mcp__mori__memory_export",
    "mcp__mori__memory_export_all",
    "mcp__mori__memory_import",
    "mcp__mori__memory_history",
    "mcp__mori__memory_diff",
    "mcp__mori__memory_rollback",
    "mcp__mori__memory_review",
    "mcp__mori__memory_session_summary",
    "mcp__mori__memory_pending_list",
    "mcp__mori__memory_approve",
    "mcp__mori__memory_reject",
    "mcp__mori__memory_protect",
    # Dream pipeline
    "mcp__mori__dream_run",
    "mcp__mori__dream_status",
    # Ingest
    "mcp__mori__mori_ingest",
    "mcp__mori__mori_ingest_status",
    "mcp__mori__mori_ingest_preview",
    "mcp__mori__mori_ingest_content",
    # NATS
    "mcp__mori__nats_pub",
    "mcp__mori__nats_sub",
    "mcp__mori__nats_ping",
    # Messaging
    "mcp__mori__msg_send",
    "mcp__mori__msg_recv",
    "mcp__mori__msg_thread",
]


def _is_mori_command(cmd: str) -> bool:
    return (
        "mori-ship-event" in cmd
        or "/api/events/raw" in cmd
        or "/api/precompact" in cmd
        or "mori-post-compact-brief" in cmd
    )


def _is_mori_hook(entry: dict[str, Any]) -> bool:
    if entry.get("_mori_managed") is True:
        return True
    return _is_mori_command(entry.get("command", ""))


def _new_hook_entry(cmd: str) -> dict[str, Any]:
    return {"type": "command", "command": cmd, "_mori_managed": True}


def _upgrade_hook_entry(entry: dict[str, Any], new_cmd: str) -> None:
    entry["command"] = new_cmd
    entry["_mori_managed"] = True


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


def _postcompact_command(brief_shipper: str) -> str:
    """PostCompact runs mori-post-compact-brief only (no URL/client/mode args)."""
    if brief_shipper.lower().endswith(".ps1"):
        return f'powershell -NoProfile -ExecutionPolicy Bypass -File "{brief_shipper}"'
    return f'"{brief_shipper}"'


def _update_entry_command(entry: dict[str, Any], new_cmd: str) -> bool:
    if "hooks" in entry and isinstance(entry["hooks"], list):
        for h in entry["hooks"]:
            if isinstance(h, dict) and h.get("type") == "command" and _is_mori_hook(h):
                _upgrade_hook_entry(h, new_cmd)
                return True
        entry["hooks"].insert(0, _new_hook_entry(new_cmd))
        return True
    if entry.get("type") == "command" and _is_mori_hook(entry):
        _upgrade_hook_entry(entry, new_cmd)
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
        existing.insert(0, _new_hook_entry(new_cmd))
    return existing


def _hook_list_has_mori(entries: list[Any]) -> bool:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if _is_mori_hook(entry):
            return True
        nested = entry.get("hooks")
        if isinstance(nested, list) and _hook_list_has_mori(nested):
            return True
    return False


def _settings_has_mori_hooks(settings: dict[str, Any]) -> bool:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for event_list in hooks.values():
        if isinstance(event_list, list) and _hook_list_has_mori(event_list):
            return True
    return False


def _prune_mori_hooks_for_events(hooks: dict[str, Any], events: tuple[str, ...]) -> None:
    """Remove _mori_managed (or mori-command) entries for the given hook events."""
    for event in events:
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        pruned: list[Any] = []
        for entry in entries:
            if not isinstance(entry, dict):
                pruned.append(entry)
                continue
            if "hooks" in entry and isinstance(entry["hooks"], list):
                nested = [
                    h
                    for h in entry["hooks"]
                    if not (_is_mori_hook(h) if isinstance(h, dict) else False)
                ]
                if nested:
                    entry = {**entry, "hooks": nested}
                    pruned.append(entry)
            elif not _is_mori_hook(entry):
                pruned.append(entry)
        if pruned:
            hooks[event] = pruned
        elif event in hooks:
            del hooks[event]


def merge_settings_compat(
    settings_path: Path,
    shipper: str,
    mori_url: str,
    client: str,
    api_key: str,
    *,
    prune_native_overlap: bool = True,
) -> None:
    """Merge PreCompact/PostCompact + permissions only (plugin native hooks cover telemetry)."""
    precompact_cmd = _hook_command(shipper, mori_url, client, api_key, "precompact")
    brief_ext = ".ps1" if shipper.lower().endswith(".ps1") else ".sh"
    brief_shipper = str(Path(shipper).parent / f"mori-post-compact-brief{brief_ext}")
    postcompact_cmd = _postcompact_command(brief_shipper)

    if settings_path.is_file():
        with settings_path.open(encoding="utf-8") as f:
            settings: dict[str, Any] = json.load(f)
    else:
        settings = {}

    hooks = settings.setdefault("hooks", {})
    if prune_native_overlap:
        _prune_mori_hooks_for_events(hooks, NATIVE_COVERED_EVENTS)

    for event in COMPAT_HOOK_EVENTS:
        if event == "PreCompact":
            cmd = precompact_cmd
        else:
            cmd = postcompact_cmd
        hooks[event] = _merge_hook_list(hooks.get(event, []), cmd)

    perms = settings.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    if not isinstance(allow, list):
        allow = []
        perms["allow"] = allow
    for tool in MORI_MCP_ALLOW:
        if tool not in allow:
            allow.append(tool)

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with settings_path.open("w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")


def merge_plugin_mcp(mcp_path: Path, mori_url: str, api_key: str = "") -> None:
    """Merge mori MCP server into the plugin-bundled mcp.json."""
    mori_server: dict[str, Any] = {
        "type": "http",
        "url": f"{mori_url.rstrip('/')}/mcp",
    }
    if api_key:
        mori_server["headers"] = {"x-api-key": api_key}
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


def merge_settings(
    settings_path: Path,
    shipper: str,
    mori_url: str,
    client: str,
    api_key: str,
) -> None:
    """Merge Mori shipper hooks and MCP permissions into settings.json."""
    raw_cmd = _hook_command(shipper, mori_url, client, api_key, "raw")
    precompact_cmd = _hook_command(shipper, mori_url, client, api_key, "precompact")

    # PostCompact runs the brief-shipper (no mode args) — it lives alongside the
    # ship-event shipper, deployed by the .sh/.ps1 installer.
    brief_ext = ".ps1" if shipper.lower().endswith(".ps1") else ".sh"
    brief_shipper = str(Path(shipper).parent / f"mori-post-compact-brief{brief_ext}")
    postcompact_cmd = _postcompact_command(brief_shipper)

    if settings_path.is_file():
        with settings_path.open(encoding="utf-8") as f:
            settings: dict[str, Any] = json.load(f)
    else:
        settings = {}

    hooks = settings.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        if event == "PreCompact":
            cmd = precompact_cmd
        elif event == "PostCompact":
            cmd = postcompact_cmd
        else:
            cmd = raw_cmd
        hooks[event] = _merge_hook_list(hooks.get(event, []), cmd)

    perms = settings.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    if not isinstance(allow, list):
        allow = []
        perms["allow"] = allow
    for tool in MORI_MCP_ALLOW:
        if tool not in allow:
            allow.append(tool)

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with settings_path.open("w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")


def merge_mcp(mcp_path: Path, mori_url: str) -> None:
    mori_server = {"type": "http", "url": f"{mori_url.rstrip('/')}/mcp"}
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


def _parse_skill_md(skill_dir: Path, skill_file: Path) -> tuple[str, str, str | None]:
    """Return (name, description, body). body=None means copy SKILL.md verbatim."""
    text = skill_file.read_text(encoding="utf-8")
    default_name = skill_dir.name

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm = parts[1]
            name = default_name
            desc = ""
            for line in fm.splitlines():
                line = line.strip()
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("description:"):
                    desc = line.split(":", 1)[1].strip().strip('"')
            return name, desc, None

    name = ""
    desc = ""
    body_lines: list[str] = []
    in_body = False
    for line in text.splitlines():
        if re.match(r"^-\s+name:\s*", line):
            name = re.sub(r"^-\s+name:\s*", "", line).strip()
        elif re.match(r"^-\s+description:\s*", line):
            desc = re.sub(r"^-\s+description:\s*", "", line).strip()
        elif not in_body and not name and not desc and not line.strip():
            continue
        else:
            in_body = True
            body_lines.append(line)
    if not name:
        name = default_name
    return name, desc, "\n".join(body_lines).rstrip() + "\n"


def deploy_skills(source_dir: Path, dest_dir: Path, upgrade: bool, prefix: str = "") -> int:
    if not source_dir.is_dir():
        print(f"  Warning: skills source not found: {source_dir}", file=sys.stderr)
        return 0

    dest_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for skill_dir in sorted(source_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            continue
        name, desc, body = _parse_skill_md(skill_dir, skill_file)
        folder = dest_dir / f"{prefix}{name}"
        skill_file_out = folder / "SKILL.md"
        if skill_file_out.exists() and not upgrade:
            continue
        folder.mkdir(parents=True, exist_ok=True)
        if body is None:
            skill_file_out.write_text(skill_file.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            escaped = desc.replace('"', '\\"')
            skill_file_out.write_text(
                f'---\nname: {prefix}{name}\ndescription: "{escaped}"\n---\n\n{body}\n',
                encoding="utf-8",
            )
        print(f"  Deployed skill: {prefix}{name}")
        count += 1
    return count


def _mcp_paths() -> tuple[Path, Path]:
    home = Path.home()
    if sys.platform == "darwin":
        mcp = home / "Library/Application Support/Cursor/mcp.json"
    elif sys.platform == "win32":
        mcp = home / ".cursor" / "mcp.json"
    else:
        mcp = home / ".cursor/mcp.json"
    settings = home / ".claude" / "settings.json"
    return mcp, settings


def _read_mori_url(mcp_path: Path) -> str | None:
    if not mcp_path.is_file():
        return None
    try:
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        url = data.get("mcpServers", {}).get("mori", {}).get("url", "")
        return url.removesuffix("/mcp") if url else None
    except (json.JSONDecodeError, AttributeError):
        return None


def _http_get(url: str, timeout: int = 5) -> tuple[int, str]:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")[:500]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")[:500]
    except OSError as e:
        return 0, str(e)


def _plugin_paths() -> tuple[Path, Path, Path]:
    home = Path.home()
    plugin_dir = home / ".cursor" / "plugins" / "local" / "mori"
    return plugin_dir, plugin_dir / "mcp.json", home / ".cursor" / "hooks.json"


def _hooks_json_has_mori(hooks_path: Path, min_events: int = 1) -> tuple[bool, set[str]]:
    if not hooks_path.is_file():
        return False, set()
    try:
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False, set()
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        return False, set()
    found: set[str] = set()
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            cmd = ""
            if isinstance(entry, dict):
                cmd = entry.get("command", "")
                if not cmd and isinstance(entry.get("hooks"), list):
                    for h in entry["hooks"]:
                        if isinstance(h, dict):
                            cmd = h.get("command", "")
            if "mori-" in cmd:
                found.add(event)
    return len(found) >= min_events, found


def _settings_has_postcompact(settings_path: Path) -> bool:
    if not settings_path.is_file():
        return False
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    entries = data.get("hooks", {}).get("PostCompact", [])
    if not isinstance(entries, list):
        return False
    return _hook_list_has_mori(entries)


def doctor_plugin(mori_url: str | None, client: str, *, parity: bool = False) -> int:
    plugin_dir, plugin_mcp, hooks_json = _plugin_paths()
    settings_path = Path.home() / ".claude" / "settings.json"
    errors = 0
    warns = 0

    print("--- Mori Cursor plugin doctor ---\n")
    mode = "parity" if parity else "minimal"
    print(f"Mode: {mode}\n")

    caps: list[tuple[str, str, str]] = []  # name, status, note

    if plugin_dir.is_dir():
        print(f"OK  Plugin directory: {plugin_dir}")
        caps.append(("Plugin installed", "OK", str(plugin_dir)))
    else:
        print(f"FAIL  Plugin directory missing: {plugin_dir}")
        caps.append(("Plugin installed", "FAIL", "run install-mori-cursor-plugin.sh"))
        errors += 1

    if plugin_mcp.is_file():
        print(f"OK  Plugin MCP: {plugin_mcp}")
        url = None
        try:
            data = json.loads(plugin_mcp.read_text(encoding="utf-8"))
            url = data.get("mcpServers", {}).get("mori", {}).get("url", "")
            url = url.removesuffix("/mcp") if url else None
        except json.JSONDecodeError:
            pass
        if url:
            print(f"    mori URL: {url}")
            mori_url = mori_url or url
            caps.append(("MCP configured", "OK", url))
        else:
            print("FAIL  plugin mcp.json missing mcpServers.mori.url")
            caps.append(("MCP configured", "FAIL", ""))
            errors += 1
    else:
        print(f"FAIL  Plugin MCP missing: {plugin_mcp}")
        errors += 1

    skills_in_plugin = (
        [p.parent.name for p in plugin_dir.glob("skills/*/SKILL.md")] if plugin_dir.is_dir() else []
    )
    deployed = set(skills_in_plugin) & PLUGIN_SKILL_NAMES
    if len(deployed) >= len(PLUGIN_SKILL_NAMES):
        print(f"OK  Plugin skills: {len(deployed)}/{len(PLUGIN_SKILL_NAMES)}")
        caps.append(("Skills in plugin", "OK", f"{len(deployed)} skills"))
    elif deployed:
        print(f"WARN  Plugin skills: {len(deployed)}/{len(PLUGIN_SKILL_NAMES)}")
        caps.append(("Skills in plugin", "WARN", ", ".join(sorted(deployed))))
        warns += 1
    else:
        print(f"WARN  No skills under {plugin_dir}/skills/")
        warns += 1

    native_ok, native_events = _hooks_json_has_mori(hooks_json, min_events=3)
    if native_ok:
        print(f"OK  Native hooks (~/.cursor/hooks.json): {', '.join(sorted(native_events))}")
        caps.append(("Native hooks", "OK", ", ".join(sorted(native_events))))
    else:
        print(f"WARN  Native hooks missing or incomplete in {hooks_json}")
        caps.append(("Native hooks", "WARN", "run install-hooks-cursor.mjs"))
        warns += 1

    if parity:
        expected_native = {
            "sessionStart",
            "postToolUse",
            "stop",
            "beforeSubmitPrompt",
            "postToolUseFailure",
        }
        missing_native = expected_native - native_events
        if missing_native:
            print(f"WARN  Parity native events missing: {', '.join(sorted(missing_native))}")
            caps.append(("Parity native events", "WARN", ", ".join(sorted(missing_native))))
            warns += 1
        else:
            caps.append(("Parity native events", "OK", "all wired"))

        if _settings_has_postcompact(settings_path):
            print(f"OK  Compat PostCompact in {settings_path}")
            caps.append(("Post-compact re-ground", "OK", "PostCompact hook"))
        else:
            print(f"WARN  PostCompact not in {settings_path}")
            caps.append(("Post-compact re-ground", "WARN", "re-run with --parity"))
            warns += 1

        text = settings_path.read_text(encoding="utf-8") if settings_path.is_file() else ""
        if "PreCompact" in text and "mori-ship-event" in text:
            caps.append(("Pre-compact dream", "OK", "PreCompact compat hook"))
        else:
            caps.append(("Pre-compact dream", "WARN", "PreCompact compat hook missing"))
            warns += 1

    if mori_url:
        code, body = _http_get(f"{mori_url.rstrip('/')}/health")
        if code == 200 and "ok" in body:
            print(f"OK  Server health: {mori_url}/health")
            caps.append(("Server reachable", "OK", ""))
        else:
            print(f"FAIL  Server health ({code})")
            caps.append(("Server reachable", "FAIL", body[:80]))
            errors += 1
    else:
        errors += 1

    print("\n--- Capability matrix ---\n")
    for name, status, note in caps:
        suffix = f" — {note}" if note else ""
        print(f"  {status:4}  {name}{suffix}")

    print(f"\nClient tag for events: {client}")
    if errors:
        print(f"\nDoctor: {errors} check(s) failed, {warns} warning(s).")
        return 1
    print(f"\nDoctor: passed ({warns} warning(s)). Reload Cursor after changes.")
    return 0 if warns == 0 else 0


def doctor(mori_url: str | None, client: str) -> int:
    mcp_path, settings_path = _mcp_paths()
    skills_dir = Path.home() / ".claude" / "skills"
    errors = 0

    print("--- Mori Cursor doctor ---\n")

    if mcp_path.is_file():
        print(f"OK  MCP config: {mcp_path}")
        url = _read_mori_url(mcp_path)
        if url:
            print(f"    mori URL: {url}")
            mori_url = mori_url or url
        else:
            print("FAIL  mcp.json missing mcpServers.mori.url")
            errors += 1
    else:
        print(f"FAIL  MCP config missing: {mcp_path}")
        print("      Run: ./scripts/install-mori-cursor.sh --url <server>")
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

    if settings_path.is_file():
        text = settings_path.read_text(encoding="utf-8")
        hooks_ok = False
        try:
            hooks_ok = _settings_has_mori_hooks(json.loads(text))
        except json.JSONDecodeError:
            pass
        if not hooks_ok:
            hooks_ok = (
                "mori-ship-event" in text
                or "/api/events/raw" in text
                or '"_mori_managed": true' in text
                or '"_mori_managed":true' in text
            )
        if hooks_ok:
            print(f"OK  Event hooks present in {settings_path} (_mori_managed or legacy)")
        else:
            print(f"WARN  No Mori hooks in {settings_path}")
            errors += 1
        if any(t in text for t in MORI_MCP_ALLOW[:3]):
            print("OK  MCP tool permissions seeded")
        else:
            print("WARN  permissions.allow may be missing Mori MCP tools")
    else:
        print(f"FAIL  settings.json missing: {settings_path}")
        errors += 1

    mori_skills = (
        [p.parent.name for p in skills_dir.glob("*/SKILL.md")] if skills_dir.is_dir() else []
    )
    expected = {"brief", "consult", "dream", "ingest", "msg", "nats", "pensieve", "req", "wrap"}
    deployed = set(mori_skills) & expected
    if deployed:
        print(
            f"OK  Skills: {len(deployed)}/{len(expected)} mori skills deployed ({', '.join(sorted(deployed))})"
        )
    else:
        print(f"WARN  No mori skills under {skills_dir}")

    print(f"\nClient tag for events: {client}")
    print("\nReminder: shared memory lives on the Mori server (GCE/homelab), not on this machine.")
    if errors:
        print(f"\nDoctor: {errors} check(s) failed.")
        return 1
    print("\nDoctor: all critical checks passed. Reload Cursor window if MCP was just installed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Mori Cursor install helpers")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_mcp = sub.add_parser("merge-mcp")
    p_mcp.add_argument("--mcp-path", required=True)
    p_mcp.add_argument("--url", required=True)

    p_set = sub.add_parser("merge-settings")
    p_set.add_argument("--settings-path", required=True)
    p_set.add_argument("--shipper", required=True)
    p_set.add_argument("--url", required=True)
    p_set.add_argument("--client", required=True)
    p_set.add_argument("--api-key", default="")

    p_sk = sub.add_parser("deploy-skills")
    p_sk.add_argument("--source", required=True)
    p_sk.add_argument("--dest", required=True)
    p_sk.add_argument("--upgrade", action="store_true")
    p_sk.add_argument("--prefix", default="")

    p_doc = sub.add_parser("doctor")
    p_doc.add_argument("--url", default="")
    p_doc.add_argument("--client", default="cursor")

    p_compat = sub.add_parser("merge-settings-compat")
    p_compat.add_argument("--settings-path", required=True)
    p_compat.add_argument("--shipper", required=True)
    p_compat.add_argument("--url", required=True)
    p_compat.add_argument("--client", required=True)
    p_compat.add_argument("--api-key", default="")
    p_compat.add_argument("--no-prune", action="store_true")

    p_pmcp = sub.add_parser("merge-plugin-mcp")
    p_pmcp.add_argument("--mcp-path", required=True)
    p_pmcp.add_argument("--url", required=True)
    p_pmcp.add_argument("--api-key", default="")

    p_pdoc = sub.add_parser("doctor-plugin")
    p_pdoc.add_argument("--url", default="")
    p_pdoc.add_argument("--client", default="cursor")
    p_pdoc.add_argument("--parity", action="store_true")

    args = parser.parse_args()

    if args.cmd == "merge-mcp":
        merge_mcp(Path(args.mcp_path), args.url)
        return 0
    if args.cmd == "merge-settings":
        merge_settings(
            Path(args.settings_path),
            args.shipper,
            args.url,
            args.client,
            args.api_key,
        )
        return 0
    if args.cmd == "deploy-skills":
        n = deploy_skills(Path(args.source), Path(args.dest), args.upgrade, args.prefix)
        if n == 0:
            print("  No skills deployed (already present; use --upgrade-skills to refresh)")
        return 0
    if args.cmd == "doctor":
        url = args.url or None
        return doctor(url, args.client)
    if args.cmd == "merge-settings-compat":
        merge_settings_compat(
            Path(args.settings_path),
            args.shipper,
            args.url,
            args.client,
            args.api_key,
            prune_native_overlap=not args.no_prune,
        )
        return 0
    if args.cmd == "merge-plugin-mcp":
        merge_plugin_mcp(Path(args.mcp_path), args.url, args.api_key)
        return 0
    if args.cmd == "doctor-plugin":
        url = args.url or None
        return doctor_plugin(url, args.client, parity=args.parity)
    return 1


if __name__ == "__main__":
    sys.exit(main())
