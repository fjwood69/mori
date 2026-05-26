"""Git history parser — extracts commit messages and diffs for distillation.

Uses subprocess to call git directly — avoids the GitPython dependency.
Each batch of commits becomes a chunk.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from mori_advisor.parsers import BaseParser, Chunk, register_parser

logger = logging.getLogger(__name__)

# ~50K tokens ≈ 200K characters
CHUNK_CHAR_LIMIT = 200_000
# Max commits per chunk (to keep individual diffs readable)
MAX_COMMITS_PER_CHUNK = 50


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

        # Group commits into chunks
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
