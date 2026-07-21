"""
Interactive profile builder for PrivacyExorcist.

SPEC-005 §3.3: Rich-based wizard for first-run profile creation.
Prompts for all required fields, validates input, and returns a dict
ready for Vault.create() or Profile.from_dict().
"""

from __future__ import annotations

import re
from typing import Optional

from rich.console import Console
from rich.prompt import Confirm, Prompt


# ── Validation ──────────────────────────────────────────────────────────────

def _validate_required(value: str) -> bool:
    return len(value.strip()) > 0


def _validate_state(value: str) -> bool:
    return bool(re.match(r"^[A-Z]{2}$", value.strip()))


def _validate_zip(value: str) -> bool:
    return bool(re.match(r"^\d{5}(-\d{4})?$", value.strip()))


def _validate_phone(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    return len(digits) >= 10


def _validate_email(value: str) -> bool:
    parts = value.split("@")
    return len(parts) == 2 and len(parts[0]) > 0 and "." in parts[1]


def _validate_birth_year(value: str) -> bool:
    if not value.strip():
        return True
    try:
        year = int(value.strip())
        return 1900 <= year <= 2010
    except ValueError:
        return False


# ── Prompt helpers ──────────────────────────────────────────────────────────

def _prompt_required(
    label: str,
    default: str = "",
    validator=None,
    error_msg: str = "Invalid input.",
) -> str:
    """Prompt for a required field. Loops until valid."""
    while True:
        value = Prompt.ask(f"  {label}", default=default).strip()
        if validator is None:
            if _validate_required(value):
                return value
        elif validator(value):
            return value
        print(f"    ❌ {error_msg}")


def _prompt_optional(label: str, default: str = "") -> str:
    """Prompt for an optional field."""
    return Prompt.ask(f"  {label}", default=default).strip()


def _prompt_passphrase() -> str:
    """Prompt for a passphrase with confirmation. Loops until match."""
    while True:
        pw = Prompt.ask("  Passphrase", password=True).strip()
        if len(pw) < 8:
            print("    ⚠️  Passphrase is short (< 8 chars). Consider a stronger one.")
        confirm = Prompt.ask("  Confirm   ", password=True).strip()
        if pw == confirm:
            return pw
        print("    ❌ Passphrases do not match. Try again.")


# ── Main wizard ─────────────────────────────────────────────────────────────

def run_profile_wizard(existing_profile=None) -> dict:
    """Run the interactive profile builder.

    Args:
        existing_profile: Optional Profile dataclass whose values are used
                          as defaults (for --setup overwrite mode).

    Returns:
        Dict ready for Vault.create() or Profile.from_dict().
    """
    console = Console()
    defaults = _extract_defaults(existing_profile)

    console.print()
    console.rule("[bold]PrivacyExorcist — Profile Setup[/bold]")
    console.print(
        "Your information never leaves this machine. "
        "The vault is protected by a passphrase you choose.",
        style="dim",
    )
    console.print()

    console.print("[bold]── Profile ──[/bold]")
    first_name = _prompt_required(
        "First name:", defaults.get("first_name", ""),
        error_msg="First name is required.",
    )
    last_name = _prompt_required(
        "Last name:", defaults.get("last_name", ""),
        error_msg="Last name is required.",
    )
    middle_name = _prompt_optional("Middle name:", defaults.get("middle_name", ""))

    console.print()
    console.print("[bold]── Address ──[/bold]")
    street = _prompt_required(
        "Street:", defaults.get("current_street", ""),
        error_msg="Street address is required.",
    )
    city = _prompt_required(
        "City:", defaults.get("current_city", ""),
        error_msg="City is required.",
    )
    state = _prompt_required(
        "State (2-letter, e.g. TX):", defaults.get("current_state", ""),
        validator=_validate_state,
        error_msg="State must be exactly 2 uppercase letters (e.g. TX).",
    ).upper()
    zip_code = _prompt_required(
        "ZIP code:", defaults.get("current_zip", ""),
        validator=_validate_zip,
        error_msg="ZIP must be 5 digits (or 5+4 format).",
    )
    phone = _prompt_required(
        "Phone:", defaults.get("current_phone", ""),
        validator=_validate_phone,
        error_msg="Phone must have at least 10 digits.",
    )

    console.print()
    console.print("[bold]── Contact ──[/bold]")
    email = _prompt_required(
        "Email (for verification links):", defaults.get("sentinel_email", ""),
        validator=_validate_email,
        error_msg="Email must contain '@' and a domain.",
    )

    console.print()
    console.print("[bold]── Optional ──[/bold]")
    birth_year = _prompt_optional("Birth year:", defaults.get("birth_year", ""))
    if birth_year and not _validate_birth_year(birth_year):
        print("    ⚠️  Invalid birth year. Skipping.")
        birth_year = ""

    past_zips_raw = _prompt_optional(
        "Past ZIPs (comma-separated):",
        ", ".join(defaults.get("past_zips", [])),
    )
    past_zips = [z.strip() for z in past_zips_raw.split(",") if z.strip()]

    aliases_raw = _prompt_optional(
        "Aliases (comma-separated):",
        ", ".join(defaults.get("aliases", [])),
    )
    aliases = [a.strip() for a in aliases_raw.split(",") if a.strip()]

    console.print()
    console.print("[bold]── Vault Passphrase ──[/bold]")
    console.print(
        "Choose a passphrase to encrypt your identity vault.",
        style="dim",
    )
    passphrase = _prompt_passphrase()

    console.print()
    console.print(
        "[bold yellow]⚠️  IMPORTANT:[/bold yellow] This passphrase cannot be recovered. "
        "Write it down or store it in a password manager."
    )

    return {
        "first_name": first_name,
        "last_name": last_name,
        "middle_name": middle_name,
        "current_street": street,
        "current_city": city,
        "current_state": state,
        "current_zip": zip_code,
        "current_phone": phone,
        "sentinel_email": email,
        "birth_year": birth_year,
        "past_zips": past_zips,
        "aliases": aliases,
    }


def _extract_defaults(profile) -> dict:
    """Convert a Profile dataclass to a defaults dict. Returns empty dict if None."""
    if profile is None:
        return {}
    return {
        "first_name": profile.first_name,
        "last_name": profile.last_name,
        "middle_name": profile.middle_name,
        "current_street": profile.current_street,
        "current_city": profile.current_city,
        "current_state": profile.current_state,
        "current_zip": profile.current_zip,
        "current_phone": profile.current_phone,
        "sentinel_email": profile.sentinel_email,
        "birth_year": profile.birth_year,
        "past_zips": list(profile.past_zips),
        "aliases": list(profile.aliases),
    }
