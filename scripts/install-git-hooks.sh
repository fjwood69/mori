#!/usr/bin/env bash
# Install Mori git hooks into a repository.
# Usage: ./scripts/install-git-hooks.sh [--repo /path/to/repo]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="."

while [[ $# -gt 0 ]]; do
  case $1 in
    --repo|-r) REPO_DIR="$2"; shift 2 ;;
    *) shift ;;
  esac
done

HOOKS_DIR="$REPO_DIR/.git/hooks"

if [ ! -d "$HOOKS_DIR" ]; then
  echo "Error: $HOOKS_DIR not found. Run from a git repository or pass --repo <path>."
  exit 1
fi

cp "$SCRIPT_DIR/post-push.sh" "$HOOKS_DIR/post-push"
chmod +x "$HOOKS_DIR/post-push"

echo "Installed post-push hook to $HOOKS_DIR/post-push"
echo ""
echo "Optional: set in ~/.bashrc or ~/.zshrc:"
echo "  export MORI_URL=http://localhost:8968   # default"
echo "  export MORI_CLIENT=\$(hostname)          # default — override if needed"
echo ""
echo "API key (required for commit ingestion):"
echo "  Add to ~/.claude/.secrets:"
echo "    MORI_API_KEY_<HOSTNAME_UPPER>=your-key"
echo "  e.g. MORI_API_KEY_LAPTOP=your-key-here"
echo "  The hook reads this automatically at push time."
echo ""
echo "Docs: docs/reference/git-hooks.md"
