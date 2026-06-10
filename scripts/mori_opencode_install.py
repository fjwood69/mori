#!/usr/bin/env python3
"""
mori_opencode_install.py — Python helper for install-mori-opencode.sh/.ps1

Subcommands:
  merge-config   Merge mori MCP server entry into opencode.json
  deploy-skills  Deploy mori skills to the OpenCode skills directory
  doctor         Check existing OpenCode + Mori install health
"""

import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path

# ── Utilities ──────────────────────────────────────────────────────────────────


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"  Warning: could not parse {path} as JSON — treating as empty", file=sys.stderr)
        return {}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def opencode_config_dir() -> Path:
    """Return the platform-appropriate global OpenCode config directory."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "opencode"
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            return Path(appdata) / "opencode"
    return Path.home() / ".config" / "opencode"


# ── Subcommands ───────────────────────────────────────────────────────────────


def cmd_merge_config(args: argparse.Namespace) -> int:
    """Merge mori MCP server entry into opencode.json."""
    config_path = (
        Path(args.config_path) if args.config_path else opencode_config_dir() / "opencode.json"
    )
    url = args.url.rstrip("/")

    mori_entry: dict = {
        "type": "remote",
        "url": f"{url}/mcp",
        "headers": {"x-api-key": args.api_key or "YOUR-64-CHAR-BARE-SECRET"},
    }
    if not args.api_key:
        print("  Note: no API key provided — set x-api-key in opencode.json before connecting")

    config = read_json(config_path)
    config.setdefault("mcpServers", {})["mori"] = mori_entry

    write_json(config_path, config)
    print(f"  Updated {config_path}")
    return 0


def cmd_deploy_skills(args: argparse.Namespace) -> int:
    """Deploy mori skills to the OpenCode skills directory."""
    source_dir = Path(args.source)
    dest_dir = Path(args.dest) if args.dest else opencode_config_dir() / "skills"
    upgrade = args.upgrade

    if not source_dir.exists():
        print(f"  Warning: skills source not found: {source_dir}", file=sys.stderr)
        return 1

    dest_dir.mkdir(parents=True, exist_ok=True)
    deployed = 0

    for skill_dir in source_dir.iterdir():
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue

        dest_skill_dir = dest_dir / skill_dir.name
        dest_skill_file = dest_skill_dir / "SKILL.md"

        if dest_skill_file.exists() and not upgrade:
            print(f"  Skipped existing skill: {skill_dir.name} (use --upgrade to refresh)")
            continue

        dest_skill_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_file, dest_skill_file)
        action = "Updated" if dest_skill_file.exists() else "Deployed"
        print(f"  {action} skill: {skill_dir.name}")
        deployed += 1

    if deployed == 0 and not upgrade:
        print("  No new skills deployed (all present; use --upgrade to refresh)")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check existing OpenCode + Mori install."""
    import urllib.error
    import urllib.request

    url = args.url.rstrip("/")
    errors = 0
    warnings = 0

    def ok(msg):
        print(f"  OK   {msg}")

    def warn(msg):
        nonlocal warnings
        warnings += 1
        print(f"  WARN {msg}")

    def fail(msg):
        nonlocal errors
        errors += 1
        print(f"  FAIL {msg}")

    config_dir = opencode_config_dir()
    global_plugin_dir = config_dir / "plugins" / "mori"
    project_plugin_dir = Path(".opencode") / "plugins" / "mori"

    print("Mori OpenCode doctor")
    print("─" * 40)

    # Plugin presence
    if global_plugin_dir.exists():
        ok(f"Global plugin: {global_plugin_dir}")
    elif project_plugin_dir.exists():
        ok(f"Project plugin: {project_plugin_dir}")
    else:
        fail(f"Plugin not found in {global_plugin_dir} or {project_plugin_dir}")

    # opencode.json + MCP entry
    config_path = config_dir / "opencode.json"
    if config_path.exists():
        config = read_json(config_path)
        if config.get("mcpServers", {}).get("mori"):
            ok(f"MCP entry in {config_path}")
            mori = config["mcpServers"]["mori"]
            detected_url = mori.get("url", "").replace("/mcp", "")
            if detected_url:
                url = detected_url
                print(f"       mori URL: {url}/mcp")
        else:
            fail(f"No 'mori' in mcpServers in {config_path}")
    else:
        warn(f"opencode.json not found at {config_path} — MCP config may be project-scoped")

    # Env vars
    if os.environ.get("MORI_SERVER_URL"):
        ok(f"MORI_SERVER_URL = {os.environ['MORI_SERVER_URL']}")
    else:
        warn("MORI_SERVER_URL not set — set it in your shell profile")

    if os.environ.get("MORI_API_KEY"):
        ok("MORI_API_KEY set")
    else:
        warn("MORI_API_KEY not set — set it in your shell profile")

    # Server health
    if url:
        print(f"  →    health check {url}/health ... ", end="", flush=True)
        try:
            req = urllib.request.Request(url + "/health", headers={"x-api-key": args.api_key or ""})
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    print("OK")
                else:
                    print(f"FAIL ({resp.status})")
                    errors += 1
        except urllib.error.URLError as e:
            print(f"FAIL ({e})")
            errors += 1

    # Skills
    skills_dir = config_dir / "skills"
    skill_names = ["brief", "dream", "pensieve", "consult", "wrap"]
    if skills_dir.exists():
        found = [s for s in skill_names if (skills_dir / s / "SKILL.md").exists()]
        if found:
            ok(f"Skills ({len(found)}/{len(skill_names)}): {', '.join(found)}")
        else:
            warn(f"No mori skills in {skills_dir} (check .claude/skills/ as fallback)")
    else:
        # .claude/skills is also valid
        claude_skills = Path.home() / ".claude" / "skills"
        if claude_skills.exists():
            found = [s for s in skill_names if (claude_skills / s / "SKILL.md").exists()]
            if found:
                ok(
                    f"Skills via .claude/skills/ ({len(found)}/{len(skill_names)}): {', '.join(found)}"
                )
            else:
                warn("No mori skills found in .claude/skills/ either")
        else:
            warn("Skills directory not found — run with --upgrade-skills to deploy")

    print()
    if errors:
        print(f"Doctor: {errors} error(s), {warnings} warning(s) — see above")
        return 1
    elif warnings:
        print(
            f"Doctor: all critical checks passed ({warnings} warning(s)). Restart OpenCode if MCP was just installed."
        )
    else:
        print("Doctor: all checks passed. Restart OpenCode if MCP was just installed.")
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Mori OpenCode installer helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_cfg = sub.add_parser("merge-config", help="Merge mori MCP entry into opencode.json")
    p_cfg.add_argument("--config-path", help="Path to opencode.json (default: global)")
    p_cfg.add_argument("--url", required=True, help="Mori server base URL")
    p_cfg.add_argument("--api-key", default="", help="Bare API key")

    p_skills = sub.add_parser("deploy-skills", help="Deploy skills to OpenCode skills dir")
    p_skills.add_argument("--source", required=True, help="skills/ source directory")
    p_skills.add_argument(
        "--dest", help="Destination skills directory (default: global OpenCode skills)"
    )
    p_skills.add_argument("--upgrade", action="store_true", help="Overwrite existing skills")

    p_doc = sub.add_parser("doctor", help="Check install health")
    p_doc.add_argument("--url", default="", help="Mori server base URL")
    p_doc.add_argument("--api-key", default="", help="Bare API key for health check")

    args = parser.parse_args()
    dispatch = {
        "merge-config": cmd_merge_config,
        "deploy-skills": cmd_deploy_skills,
        "doctor": cmd_doctor,
    }
    return dispatch[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
