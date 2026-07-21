"""
PrivacyExorcist profile.json and playbook.json loaders.

SPEC-001 §5 Phase 3: Validation, deduplication, error messages.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from privacy_exorcist.models import Playbook, PlaybookEntry, Profile


# ── Custom Exceptions ───────────────────────────────────────────────────────

class ProfileValidationError(ValueError):
    """Raised when profile.json fails validation."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


class PlaybookValidationError(ValueError):
    """Raised when playbook.json fails validation."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


# ── Profile Loader ──────────────────────────────────────────────────────────

def load_profile(path: str | Path) -> Profile:
    """Load and validate profile.json.

    Args:
        path: Absolute or relative path to profile.json.

    Returns:
        Validated Profile dataclass.

    Raises:
        ProfileValidationError: Required fields missing or empty.
        FileNotFoundError: Path does not exist.
        json.JSONDecodeError: Invalid JSON.
    """
    path = Path(path).expanduser().resolve()

    if not path.exists():
        raise FileNotFoundError(f"Profile file not found: {path}")

    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    if not isinstance(data, dict):
        raise ProfileValidationError(["profile.json must be a JSON object"])

    return Profile.from_dict(data)


# ── Playbook Loader ─────────────────────────────────────────────────────────

def load_playbook(path: str | Path) -> Playbook:
    """Load and validate playbook.json.

    Args:
        path: Absolute or relative path to playbook.json.

    Returns:
        Validated Playbook with deduplicated entries (last wins).

    Raises:
        PlaybookValidationError: Missing required fields in any entry.
        FileNotFoundError: Path does not exist.
        json.JSONDecodeError: Invalid JSON.
    """
    path = Path(path).expanduser().resolve()

    if not path.exists():
        raise FileNotFoundError(f"Playbook file not found: {path}")

    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    if not isinstance(data, dict):
        raise PlaybookValidationError(["playbook.json must be a JSON object"])

    raw_entries: list[dict] = data.get("brokers", [])
    if not raw_entries:
        raise PlaybookValidationError(
            ["playbook.json must contain a non-empty 'brokers' array"]
        )

    # Deduplicate — last entry wins, warn on stderr (SPEC-001 §5 Phase 3)
    seen: dict[str, int] = {}  # broker_id → index
    entries: list[PlaybookEntry] = []
    for i, raw in enumerate(raw_entries):
        broker_id = raw.get("broker_id", "").strip()
        if not broker_id:
            raise PlaybookValidationError(
                [f"Entry {i}: missing 'broker_id'"]
            )
        if broker_id in seen:
            print(
                f"⚠️  WARNING: Duplicate broker_id '{broker_id}' "
                f"at indices {seen[broker_id]} and {i}. "
                f"Last entry wins.",
                file=sys.stderr,
            )
            # Remove the earlier entry
            entries = [e for e in entries if e.broker_id != broker_id]
        seen[broker_id] = i
        entries.append(PlaybookEntry.from_dict(raw))

    return Playbook(brokers=entries)
