#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# PrivacyExorcist — one-command launcher
# SPEC-005 §3.4
#
# Usage:
#   ./start.sh              Normal run (vault unlock → engine)
#   ./start.sh --status     Show broker ledger (no passphrase needed)
#   ./start.sh --setup      Force re-run profile wizard
#   ./start.sh --help       Show usage
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

# Resolve project root (works when called from any directory)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Virtual environment ────────────────────────────────────────────────────
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
else
    echo "❌ Virtual environment not found at .venv/"
    echo "   Run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

# ── Dependency check ───────────────────────────────────────────────────────
python3 -c "import cryptography" 2>/dev/null || {
    echo "❌ 'cryptography' package not installed."
    echo "   Run: pip install cryptography"
    exit 1
}

python3 -c "import rich" 2>/dev/null || {
    echo "❌ 'rich' package not installed."
    echo "   Run: pip install rich"
    exit 1
}

# ── Run ────────────────────────────────────────────────────────────────────
exec python3 main.py "$@"
