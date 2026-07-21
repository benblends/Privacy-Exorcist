"""
Configuration loader for PrivacyExorcist.

SPEC-004 §3.5–§3.6 + §5 Phase 2: Loads and validates .env, profile.json,
and playbook.json. Produces an AppConfig ready for Orchestrator creation.

Validation rules:
  - OPENAI_API_KEY is REQUIRED
  - IMAP_* variables are all-or-nothing
  - HEADLESS defaults to True
  - profile.json fields validated by Profile.from_dict()
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from privacy_exorcist.loaders import (
    PlaybookValidationError,
    ProfileValidationError,
    load_playbook,
    load_profile,
)
from privacy_exorcist.models import Playbook, Profile


# ── Ensure .env is loaded before this module is imported ───────────────────
# Called by main.py before any other imports. Here as fallback.
load_dotenv(Path.home() / ".hermes" / ".env")


# ── AppConfig ───────────────────────────────────────────────────────────────

@dataclass
class AppConfig:
    """Validated application configuration."""
    profile: Optional[Profile] = None
    playbook: Optional[Playbook] = None
    openai_key: Optional[str] = None
    capsolver_key: Optional[str] = None
    headless: bool = True
    log_level: str = "INFO"
    imap_server: Optional[str] = None
    imap_port: Optional[int] = None
    imap_username: Optional[str] = None
    imap_password: Optional[str] = None
    errors: list[str] = field(default_factory=list)

    @property
    def imap_configured(self) -> bool:
        """True if all IMAP variables are set and valid."""
        return all([
            self.imap_server,
            self.imap_port,
            self.imap_username,
            self.imap_password,
        ])

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0


# ── Default Paths ──────────────────────────────────────────────────────────

def _default_profile_path() -> Path:
    return Path.cwd() / "profile.json"


def _default_playbook_path() -> Path:
    return Path.cwd() / "playbook.json"


# ── Loader ──────────────────────────────────────────────────────────────────

def load_config(
    profile: "Profile | None" = None,
    playbook_path: str | Path | None = None,
) -> AppConfig:
    """Load and validate all configuration.

    Args:
        profile: Pre-loaded Profile dataclass (from vault or wizard).
                 If None, falls back to loading from ./profile.json.
        playbook_path: Path to playbook.json. Defaults to ./playbook.json.

    Returns:
        AppConfig with profile, playbook, keys, flags, and any errors.
    """
    config = AppConfig()
    playbook_p = Path(playbook_path) if playbook_path else _default_playbook_path()

    # ── .env validation ────────────────────────────────────────────────
    _validate_env(config)

    # ── Profile ─────────────────────────────────────────────────────────
    if profile is not None:
        config.profile = profile
    else:
        # Fallback: try loading from plaintext (for backward compat)
        profile_p = _default_profile_path()
        try:
            config.profile = load_profile(profile_p)
        except FileNotFoundError:
            config.errors.append(f"Profile file not found: {profile_p}")
        except ProfileValidationError as e:
            config.errors.extend(e.errors)
        except Exception as e:
            config.errors.append(f"Profile load error: {e}")

    # ── Playbook validation ────────────────────────────────────────────
    try:
        config.playbook = load_playbook(playbook_p)
    except FileNotFoundError:
        config.errors.append(f"Playbook file not found: {playbook_p}")
    except PlaybookValidationError as e:
        config.errors.extend(e.errors)
    except Exception as e:
        config.errors.append(f"Playbook load error: {e}")

    return config


# ── .env Validation ────────────────────────────────────────────────────────

def _validate_env(config: AppConfig) -> None:
    """Validate .env variables and populate AppConfig fields."""

    # Required
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not openai_key:
        config.errors.append("OPENAI_API_KEY is required")
    config.openai_key = openai_key or None

    # Optional: CapSolver
    capsolver_key = os.environ.get("CAPSOLVER_API_KEY", "").strip()
    config.capsolver_key = capsolver_key or None

    # Optional: Headless
    headless_raw = os.environ.get("HEADLESS", "true").strip().lower()
    config.headless = headless_raw not in ("false", "0", "no")

    # Optional: Log level
    log_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    if log_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        config.log_level = log_level

    # Optional: IMAP (all-or-nothing)
    imap_server = os.environ.get("IMAP_SERVER", "").strip()
    imap_port = os.environ.get("IMAP_PORT", "").strip()
    imap_username = os.environ.get("IMAP_USERNAME", "").strip()
    imap_password = os.environ.get("IMAP_PASSWORD", "").strip()

    imap_fields = {
        "IMAP_SERVER": imap_server,
        "IMAP_PORT": imap_port,
        "IMAP_USERNAME": imap_username,
        "IMAP_PASSWORD": imap_password,
    }
    set_fields = [k for k, v in imap_fields.items() if v]
    if set_fields and len(set_fields) < 4:
        missing = [k for k in imap_fields if not imap_fields[k]]
        config.errors.append(
            f"IMAP partially configured. Missing: {', '.join(missing)}. "
            f"All four IMAP_* variables are required when any is set."
        )
    elif len(set_fields) == 4:
        try:
            config.imap_port = int(imap_port)
        except ValueError:
            config.errors.append("IMAP_PORT must be an integer")
        config.imap_server = imap_server
        config.imap_username = imap_username
        config.imap_password = imap_password
