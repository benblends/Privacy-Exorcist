#!/usr/bin/env python3
"""
PrivacyExorcist — local-first data broker opt-out engine.

Usage:
    ./start.sh              Normal run (vault unlock → engine)
    ./start.sh --status     Show broker ledger (no passphrase needed)
    ./start.sh --setup      Force re-run profile wizard
    ./start.sh --help       Show this message

Configuration:
    ~/.hermes/.env     — API keys, IMAP credentials, runtime flags
    profile.json.enc   — encrypted identity vault (created on first run)
    playbook.json      — broker opt-out instructions
"""

import asyncio
import getpass
import sys
from pathlib import Path

# Load .env BEFORE any imports that touch os.environ
from dotenv import load_dotenv

load_dotenv(Path.home() / ".hermes" / ".env")

from privacy_exorcist.browser_operator.operator import BrowserOperator
from privacy_exorcist.cli.config import load_config
from privacy_exorcist.cli.formatter import CLIFormatter
from privacy_exorcist.cli.signals import SignalHandler
from privacy_exorcist.cli.wizard import run_profile_wizard
from privacy_exorcist.database import Database
from privacy_exorcist.engine import Orchestrator
from privacy_exorcist.models import Profile
from privacy_exorcist.vault import InvalidPassphraseError, Vault


# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

VAULT_PATH = Path("profile.json.enc")
PLAINTEXT_PROFILE_PATH = Path("profile.json")
MAX_PASSPHRASE_ATTEMPTS = 3


# ═══════════════════════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════════════════════

async def main() -> int:
    args = sys.argv[1:]

    # ── --help ───────────────────────────────────────────────────────────
    if "--help" in args or "-h" in args:
        print(__doc__.strip())
        return 0

    # ── --status ─────────────────────────────────────────────────────────
    if "--status" in args:
        return _cmd_status()

    # ── --setup ──────────────────────────────────────────────────────────
    if "--setup" in args:
        return _cmd_setup()

    # ── Normal run: resolve profile ──────────────────────────────────────
    profile = _resolve_profile()

    # ── Load remaining config ────────────────────────────────────────────
    config = load_config(profile=profile)
    if not config.is_valid:
        for err in config.errors:
            print(f"❌ {err}", file=sys.stderr)
        return 1

    # ── CLI ──────────────────────────────────────────────────────────────
    cli = CLIFormatter()
    cli.print_header()
    cli.print_config_summary(
        profile,
        config.playbook,  # type: ignore[arg-type]
        capsolver_key=config.capsolver_key,
        headless=config.headless,
    )

    # ── Database ─────────────────────────────────────────────────────────
    db = Database(str(Path.cwd() / "privacy_exorcist.db"))
    db.migrate()

    # ── Orchestrator ─────────────────────────────────────────────────────
    orch = Orchestrator(
        profile=profile,
        playbook=config.playbook,  # type: ignore[arg-type]
        database=db,
        capsolver_key=config.capsolver_key,
        headless=config.headless,
    )
    orch.on_state_change = cli.on_state_change
    orch.on_broker_start = cli.on_broker_start
    orch.on_broker_complete = cli.on_broker_complete
    orch.on_hitl_prompt = cli.on_hitl_prompt

    # ── Browser Operator ─────────────────────────────────────────────────
    if not config.openai_key:
        print("❌ OPENAI_API_KEY is required", file=sys.stderr)
        return 1

    browser_op = BrowserOperator(
        openai_key=config.openai_key,
        capsolver_key=config.capsolver_key,
        headless=config.headless,
        ollama_model=os.environ.get("OLLAMA_MODEL"),
        ollama_base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
    )

    # ── Signal handler ───────────────────────────────────────────────────
    signal_handler = SignalHandler()
    signal_handler.install(orch)

    # ── Broker loop ──────────────────────────────────────────────────────
    try:
        for broker_id in orch.get_pending_brokers():
            if orch._shutdown_flag:
                break

            while True:
                try:
                    ctx = orch.start_broker(broker_id)
                    result = await browser_op.execute(ctx)
                    orch.finish_broker(broker_id, result)

                    if not orch.should_retry(broker_id):
                        break

                    backoff = orch.get_backoff(broker_id)
                    cli.console.print(
                        f"  ⏳ Retrying {broker_id} in {backoff}s...",
                        style="dim",
                    )
                    await asyncio.sleep(backoff)
                    orch.requeue_broker(broker_id)

                except asyncio.CancelledError:
                    orch.shutdown()
                    break

    finally:
        signal_handler.restore()

    # ── Summary ──────────────────────────────────────────────────────────
    summary = orch.get_summary()
    cli.print_run_summary(summary)
    return 0 if summary.get("failed", 0) == 0 and summary.get("permanently_failed", 0) == 0 else 1


# ═══════════════════════════════════════════════════════════════════════════════
# Profile Resolution
# ═══════════════════════════════════════════════════════════════════════════════

def _resolve_profile() -> Profile:
    """Determine the profile source: vault, plaintext migration, or wizard.

    Returns a validated Profile dataclass.
    Exits the process on unrecoverable errors.
    """
    vault = Vault(VAULT_PATH)
    plaintext_exists = PLAINTEXT_PROFILE_PATH.is_file()
    vault_exists = vault.exists()

    # ── Both exist: vault wins ──────────────────────────────────────────
    if vault_exists and plaintext_exists:
        print("⚠️  Both profile.json and profile.json.enc exist.")
        print("   Using encrypted vault. Delete profile.json if it's no longer needed.")
        print()
        return _unlock_vault(vault)

    # ── Vault exists (normal path) ──────────────────────────────────────
    if vault_exists:
        return _unlock_vault(vault)

    # ── Plaintext only: migration mode ──────────────────────────────────
    if plaintext_exists:
        return _migrate_plaintext(vault)

    # ── Neither: first run wizard ──────────────────────────────────────
    return _first_run_wizard(vault)


def _unlock_vault(vault: Vault) -> Profile:
    """Prompt for passphrase and unlock the vault."""
    for attempt in range(1, MAX_PASSPHRASE_ATTEMPTS + 1):
        try:
            passphrase = getpass.getpass("🔐 Vault passphrase: ")
            if not passphrase.strip():
                print("❌ Passphrase cannot be empty.")
                continue
            return vault.unlock(passphrase)
        except InvalidPassphraseError:
            remaining = MAX_PASSPHRASE_ATTEMPTS - attempt
            if remaining > 0:
                print(f"❌ Wrong passphrase. {remaining} attempt(s) remaining.")
            else:
                print("❌ Too many attempts.")
                sys.exit(1)
        except EOFError:
            print("\n❌ Cannot prompt for passphrase in non-interactive mode.")
            print("   Use --status for read-only access.")
            sys.exit(1)
    sys.exit(1)


def _migrate_plaintext(vault: Vault) -> Profile:
    """Offer to encrypt existing plaintext profile.json."""
    print("⚠️  Plaintext profile.json detected.")
    print("   Your PII is currently stored unencrypted on disk.")
    print()

    from privacy_exorcist.loaders import load_profile as load_plaintext

    try:
        profile = load_plaintext(PLAINTEXT_PROFILE_PATH)
    except Exception as e:
        print(f"❌ Cannot read profile.json: {e}")
        sys.exit(1)

    print(f"   Profile: {profile.first_name} {profile.last_name}")
    print()

    try:
        response = input("Encrypt it with a passphrase? [Y/n] ").strip().lower()
    except EOFError:
        response = "n"

    if response and response != "y":
        print("❌ Refusing to run with plaintext PII on disk. Exiting.")
        print("   Run again and choose 'y' to encrypt, or delete profile.json.")
        sys.exit(1)

    # Encrypt
    print()
    print("Choose a passphrase for your vault:")
    passphrase = _prompt_new_passphrase()

    profile_dict = {
        "first_name": profile.first_name,
        "last_name": profile.last_name,
        "middle_name": profile.middle_name,
        "aliases": profile.aliases,
        "current_street": profile.current_street,
        "current_city": profile.current_city,
        "current_state": profile.current_state,
        "current_zip": profile.current_zip,
        "current_phone": profile.current_phone,
        "past_zips": profile.past_zips,
        "birth_year": profile.birth_year,
        "sentinel_email": profile.sentinel_email,
    }

    vault.create(profile_dict, passphrase)
    PLAINTEXT_PROFILE_PATH.unlink()
    print("🗑️  Plaintext profile.json deleted.")
    print()
    return profile


def _first_run_wizard(vault: Vault) -> Profile:
    """Run the interactive profile builder for first-time setup."""
    print()
    print("🔒 First run detected. Let's create your encrypted identity vault.")
    print()

    profile_dict = run_profile_wizard()
    passphrase = profile_dict.pop("_passphrase", None)
    if not passphrase:
        # Wizard didn't return passphrase (shouldn't happen)
        passphrase = _prompt_new_passphrase()

    profile = vault.create(profile_dict, passphrase)
    print("🔐 Vault encrypted and saved to profile.json.enc")
    print()
    return profile


def _cmd_setup() -> int:
    """Force re-run the profile wizard."""
    vault = Vault(VAULT_PATH)

    if vault.exists():
        print("⚠️  A vault already exists at profile.json.enc.")
        print("   This will overwrite it with a new profile.")
        try:
            response = input("Continue? [y/N] ").strip().lower()
        except EOFError:
            response = "n"
        if response != "y":
            print("Cancelled.")
            return 0

        # Load existing as defaults
        existing = None
        try:
            passphrase = getpass.getpass("Current passphrase (to load defaults): ")
            existing = vault.unlock(passphrase)
        except (InvalidPassphraseError, EOFError):
            print("⚠️  Could not unlock existing vault. Starting fresh.")

        profile_dict = run_profile_wizard(existing_profile=existing)
    else:
        profile_dict = run_profile_wizard()

    passphrase = _prompt_new_passphrase()
    vault.create(profile_dict, passphrase)
    print("🔐 Vault saved to profile.json.enc")
    print("Run './start.sh' to begin scrubbing.")
    return 0


def _cmd_status() -> int:
    """Display broker ledger without requiring vault unlock."""
    from rich.console import Console
    from rich.table import Table

    db_path = Path.cwd() / "privacy_exorcist.db"
    if not db_path.is_file():
        print("📊 No ledger found. Run the engine first.")
        return 0

    db = Database(str(db_path))
    db.migrate()

    from privacy_exorcist.cli.formatter import STATE_ICONS

    console = Console()
    console.print()
    console.rule("[bold]📊 PrivacyExorcist — Broker Status[/bold]")

    table = Table()
    table.add_column("Broker", style="bold")
    table.add_column("Status")
    table.add_column("Retries", justify="right")
    table.add_column("CAPTCHA Solves", justify="right")
    table.add_column("Last Run")

    brokers = db.get_all_brokers()
    if not brokers:
        console.print("  No brokers in ledger. Run the engine first.", style="dim")
        return 0

    for rec in brokers:
        icon = STATE_ICONS.get(rec.current_status.value, "•")
        table.add_row(
            rec.broker_id,
            f"{icon} {rec.current_status.value}",
            str(rec.retry_count),
            str(rec.captcha_solves),
            rec.last_run_timestamp or "never",
        )

    console.print(table)
    console.print()
    return 0


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _prompt_new_passphrase() -> str:
    """Prompt for a new passphrase with confirmation."""
    while True:
        import getpass as gp
        pw = gp.getpass("  Passphrase: ").strip()
        if len(pw) < 8:
            print("    ⚠️  Short passphrase (< 8 chars). Consider a stronger one.")
        confirm = gp.getpass("  Confirm:    ").strip()
        if pw == confirm:
            return pw
        print("    ❌ Passphrases do not match. Try again.")


# ═══════════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
