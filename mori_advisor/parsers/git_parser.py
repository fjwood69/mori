"""Git history parser — extracts commit messages and diffs for distillation.

Uses subprocess to call git directly — avoids the GitPython dependency.
Each batch of commits becomes a chunk.

Content mode: receives pre-collected `git log --patch` output as text/x-git-log.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from mori_advisor.parsers import BaseParser, Chunk, register_parser

logger = logging.getLogger(__name__)

# ~50K tokens ≈ 200K characters
CHUNK_CHAR_LIMIT = 200_000
# Max commits per chunk (to keep individual diffs readable)
MAX_COMMITS_PER_CHUNK = 50

# Patterns for sanitizing commit metadata (PII/secret removal)
_SENSITIVE_PATTERNS = [
    re.compile(r"\b[\w._%+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),  # email addresses
    re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),  # IPv4
    re.compile(r"\b[0-9a-f]{40}\b"),  # full SHA hashes (keep short refs)
    re.compile(r"(?i)(sk-ant-|ghp_|gho_|ghu_|ghs_|ghr_)[\w-]+"),  # API keys
    re.compile(r"(?i)(?:https?://|git@)[^\s]+github\.com[^\s]*"),  # GitHub URLs
]


def _sanitize_commit_text(text: str) -> str:
    """Remove PII and sensitive information from commit log text.

    Strips: email addresses, IP addresses, full SHA hashes, API keys,
    GitHub URLs. Short SHAs (7-12 chars) are preserved.
    """
    for pattern in _SENSITIVE_PATTERNS:
        text = pattern.sub("<redacted>", text)
    return text


def _parse_raw_log(content: str) -> list[dict]:
    """Parse raw `git log --patch` text into commit dicts.

    Splits by 'commit <sha>' boundaries and extracts metadata and diff.
    """
    commits: list[dict] = []
    # Split on commit boundary: "commit <full_sha>"
    blocks = re.split(r"\n(?=commit [0-9a-f]{40})", content.strip())
    for block in blocks:
        if not block.strip():
            continue
        lines = block.split("\n")
        first = lines[0]
        if not first.startswith("commit "):
            continue
        sha = first[7:].strip()

        commit: dict = {
            "hash": sha,
            "author": "",
            "date": "",
            "message": "",
            "diff": "",
        }

        in_diff = False
        msg_lines: list[str] = []
        for line in lines[1:]:
            if line.startswith("Author:"):
                commit["author"] = line[7:].strip()
            elif line.startswith("Date:"):
                commit["date"] = line[5:].strip()
            elif line.startswith("    ") and not in_diff:
                msg_lines.append(line.strip())
            elif line.startswith("diff --git"):
                in_diff = True
                commit["diff"] += line + "\n"
            elif in_diff:
                commit["diff"] += line + "\n"

        commit["message"] = " ".join(msg_lines) if msg_lines else ""
        sanitized = _sanitize_commit_text(block)
        commit["sanitized_block"] = sanitized

        # Update fields after sanitization
        for pattern in _SENSITIVE_PATTERNS:
            commit["author"] = pattern.sub("<redacted>", commit["author"])

        commits.append(commit)

    return commits


@register_parser("git")
class GitParser(BaseParser):
    """Parse git history — commit messages, authors, dates, and diffs.

    Uses `git log` and `git diff` via subprocess. No GitPython dependency.

    `--since`: passed directly to `git log --since` (e.g. "30 days ago").
    """

    @classmethod
    def can_handle(cls, source: Path) -> bool:
        if not source.exists():
            return False
        # Check if path is inside a git repo
        try:
            result = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "--git-dir"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def parse(self, source: Path, **kwargs) -> list[Chunk]:
        # Find the repo root
        try:
            result = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                logger.warning("Not a git repository: %s", source)
                return []
            repo_root = Path(result.stdout.strip())
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning("Git not available: %s", e)
            return []

        since_str = kwargs.get("since", "")
        since_arg = self._build_since_arg(since_str)

        commits = self._get_commits(repo_root, since_arg)
        if not commits:
            return []

        return self._chunk_commits(commits, repo_root)

    def parse_content(self, name: str, content_bytes: bytes, mime_type: str) -> list[Chunk]:
        """Parse git log content from raw text.

        Client sends pre-collected `git log --patch` output as text/x-git-log.
        No --since filtering — clients should pre-filter before capture.

        Args:
            name: Source name/identifier.
            content_bytes: Raw git log --patch text bytes.
            mime_type: MIME type (should be text/x-git-log).

        Returns:
            List of Chunks.
        """
        content = content_bytes.decode("utf-8", errors="replace")
        commits = _parse_raw_log(content)
        if not commits:
            return []

        # Convert to the same format expected by _chunk_commits
        normalized = []
        for c in commits:
            normalized.append(
                {
                    "hash": c["hash"],
                    "date": c["date"],
                    "author": c["author"],
                    "message": c["message"],
                    "sanitized_block": c["sanitized_block"],
                }
            )

        # Use a placeholder source name since we don't have a real repo path
        return self._chunk_commits_content(normalized, name)

    def _chunk_commits(self, commits: list[dict], repo_root: Path) -> list[Chunk]:
        """Group commits into token-sized chunks."""
        chunks: list[Chunk] = []
        batch: list[dict] = []
        batch_text = ""
        part = 1

        for commit in commits:
            text = self._format_commit(repo_root, commit)
            if (
                len(batch_text) + len(text) > CHUNK_CHAR_LIMIT
                or len(batch) >= MAX_COMMITS_PER_CHUNK
            ):
                if batch_text.strip():
                    chunks.append(self._make_chunk(batch_text, batch, repo_root, part))
                    part += 1
                batch = []
                batch_text = ""

            batch.append(commit)
            if batch_text:
                batch_text += "\n\n"
            batch_text += text

        if batch_text.strip():
            chunks.append(self._make_chunk(batch_text, batch, repo_root, part))

        return chunks

    def _chunk_commits_content(self, commits: list[dict], source_name: str) -> list[Chunk]:
        """Group commits from content mode into token-sized chunks."""
        chunks: list[Chunk] = []
        batch: list[dict] = []
        batch_text = ""
        part = 1

        for commit in commits:
            text = commit.get("sanitized_block", "")
            if (
                len(batch_text) + len(text) > CHUNK_CHAR_LIMIT
                or len(batch) >= MAX_COMMITS_PER_CHUNK
            ):
                if batch_text.strip():
                    chunks.append(self._make_content_chunk(batch_text, batch, source_name, part))
                    part += 1
                batch = []
                batch_text = ""

            batch.append(commit)
            if batch_text:
                batch_text += "\n\n"
            batch_text += text

        if batch_text.strip():
            chunks.append(self._make_content_chunk(batch_text, batch, source_name, part))

        return chunks

    def _make_content_chunk(
        self, text: str, commits: list[dict], source_name: str, part: int
    ) -> Chunk:
        dates = [c["date"][:10] for c in commits if c.get("date")]
        date_range = f"{dates[0]}–{dates[-1]}" if dates else "?"
        return Chunk(
            content=text,
            metadata={
                "source_path": source_name,
                "type": "git",
                "repo": source_name,
                "commit_count": len(commits),
                "commits": [c["hash"][:8] for c in commits],
                "date_range": date_range,
                "part": part,
            },
        )

    def _build_since_arg(self, since: str) -> str:
        """Convert --since shorthand to a git-log-compatible date string."""
        if not since:
            return ""

        since = since.strip().lower()
        if since.endswith("d"):
            try:
                days = int(since[:-1])
                return f"{days} days ago"
            except ValueError:
                pass
        if since.endswith("w"):
            try:
                weeks = int(since[:-1])
                return f"{weeks} weeks ago"
            except ValueError:
                pass
        # Pass through as-is (might be a git-compatible date already)
        return since

    def _get_commits(self, repo_root: Path, since: str) -> list[dict]:
        """Get commit log as a list of dicts."""
        args = [
            "git",
            "-C",
            str(repo_root),
            "log",
            "--format=%H|%ai|%an|%s",
            "--no-merges",
        ]
        if since:
            args.extend(["--since", since])

        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.warning("git log failed: %s", result.stderr)
                return []
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning("git log error: %s", e)
            return []

        commits = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("|", 3)
            if len(parts) >= 4:
                commits.append(
                    {
                        "hash": parts[0],
                        "date": parts[1],
                        "author": parts[2],
                        "message": parts[3],
                    }
                )
        return commits

    def _format_commit(self, repo_root: Path, commit: dict) -> str:
        """Format a single commit with its diff."""
        lines = [
            f"Commit: {commit['hash'][:8]}",
            f"Date: {commit['date']}",
            f"Author: {commit['author']}",
            f"Message: {commit['message']}",
        ]

        # Get stat and diff for this commit
        try:
            stat = subprocess.run(
                ["git", "-C", str(repo_root), "show", "--stat", "--format=", commit["hash"]],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if stat.stdout.strip():
                lines.append(f"Changed files:\n{stat.stdout.strip()}")
        except Exception:
            pass

        try:
            diff = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "diff",
                    f"{commit['hash']}^!",
                    "--",
                    ":(exclude)*.lock",
                    ":(exclude)*.json",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if diff.stdout.strip():
                # Truncate huge diffs
                diff_text = diff.stdout.strip()
                if len(diff_text) > 10000:
                    diff_text = diff_text[:10000] + "\n... (diff truncated)"
                lines.append(f"Diff:\n{diff_text}")
        except Exception:
            pass

        return "\n".join(lines)

    def _make_chunk(self, text: str, commits: list[dict], repo_root: Path, part: int) -> Chunk:
        dates = [c["date"][:10] for c in commits if c.get("date")]
        date_range = f"{dates[0]}–{dates[-1]}" if dates else "?"
        return Chunk(
            content=text,
            metadata={
                "source_path": str(repo_root),
                "type": "git",
                "repo": repo_root.name,
                "commit_count": len(commits),
                "commits": [c["hash"][:8] for c in commits],
                "date_range": date_range,
                "part": part,
            },
        )
