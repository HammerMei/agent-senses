#!/usr/bin/env bash
# install.sh — install agent-senses skills into ~/.claude/skills/
set -euo pipefail

SKILLS_DIR="$HOME/.claude/skills"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$SKILLS_DIR"

install_skill() {
    local skill="$1"
    local target="$SKILLS_DIR/$skill"

    if [ -L "$target" ]; then
        echo "  ✓ $skill already linked, skipping"
    elif [ -e "$target" ]; then
        echo "  ⚠ $target exists and is not a symlink — skipping (remove manually to reinstall)"
    else
        ln -s "$REPO_DIR/$skill" "$target"
        echo "  ✓ $skill linked"
    fi
}

echo "Installing agent-senses skills to $SKILLS_DIR"
echo ""

for skill_dir in "$REPO_DIR"/*/; do
    skill="$(basename "$skill_dir")"
    [ -f "$skill_dir/SKILL.md" ] && install_skill "$skill"
done

echo ""
echo "Checking dependencies..."
if command -v codex &>/dev/null; then
    echo "  ✓ codex CLI found: $(which codex)"
else
    echo "  ✗ codex not found — install with: npm install -g @openai/codex"
fi

echo ""
echo "Done! Restart your Claude Code session to load the new skills."
