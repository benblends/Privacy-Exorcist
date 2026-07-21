================================================================================
SPEC-005: IDENTITY VAULT, STARTUP SCRIPT & INTERACTIVE CLI
================================================================================
PrivacyExorcist — V1.0
Status: Draft for Review
Date: 2026-07-21
Depends On: SPEC-001 (Core Engine), SPEC-004 (CLI/HMI)
Build Phase: 5 (Developer Experience & Trust Hardening)

================================================================================
1. PURPOSE
================================================================================

This spec defines the developer-facing onboarding experience for PrivacyExorcist
V1.0. It addresses two critical gaps before the tool can be used by anyone other
than its authors:

  1. TRUST: Raw profile.json sitting unencrypted on disk is unacceptable for a
     privacy tool. The user's PII must be encrypted at rest with a passphrase
     only they know.

  2. UX: There is no startup script. Running the tool requires manual venv
     activation and Python invocation — friction that excludes non-developers.

Additionally, this spec defines an interactive CLI wizard for first-run profile
creation and a `--status` mode that displays the ledger without requiring vault
decryption.

================================================================================
2. CODEBASE GROUND TRUTH
================================================================================

2.1 What Exists Already

  SPEC-001 (Core Engine):
    - profile.json schema (Profile dataclass, loaders.py)
    - SQLite broker_ledger + run_history tables
    - Orchestrator lifecycle (start_broker → finish_broker)

  SPEC-004 (CLI/HMI):
    - CLIFormatter (rich-based terminal output)
    - AppConfig + load_config() (.env validation)
    - SignalHandler (double-Ctrl+C)
    - main.py entry point (loads profile.json → orchestrator → browser operator)

  Available libraries in .venv:
    - cryptography (installed as transitive dep of browser-use)
    - rich (already used by CLIFormatter)
    - getpass (stdlib — secure passphrase input without echo)

  Existing files that will be modified:
    - main.py — replaced with new entry point handling vault + flags
    - privacy_exorcist/cli/config.py — updated to read from vault instead of
      plaintext profile.json

  Files to migrate (user data):
    - profile.json — plaintext PII on disk (MUST be encrypted or deleted)

2.2 What Does NOT Exist

  - Encrypted vault for profile.json
  - Passphrase-based key derivation
  - Interactive profile creation wizard
  - Startup script (start.sh)
  - --status flag (read ledger without vault unlock)
  - --setup flag (force re-run profile wizard)
  - Plaintext → encrypted migration path

2.3 Dependencies

  cryptography.fernet — Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256)
  cryptography.hazmat.primitives.kdf.pbkdf2 — PBKDF2HMAC key derivation
  os.urandom — cryptographically secure random salt generation
  getpass — secure passphrase input (no terminal echo)
  rich.prompt — interactive prompts with validation

================================================================================
3. DESIGN
================================================================================

3.1 Architecture

  ┌──────────────────────────────────────────────────────────────┐
  │                       start.sh                                │
  │  source .venv/bin/activate                                    │
  │  python main.py "$@"                                          │
  └──────────────────────────┬───────────────────────────────────┘
                             │
  ┌──────────────────────────▼───────────────────────────────────┐
  │                       main.py                                 │
  │                                                               │
  │  ┌─────────────┐  ┌──────────────┐  ┌─────────────────────┐  │
  │  │ Flag Parser  │  │ Vault Layer  │  │ Interactive Wizard  │  │
  │  │              │  │              │  │                     │  │
  │  │ --status     │  │ unlock()     │  │ create_profile()   │  │
  │  │ --setup      │  │ create()     │  │                     │  │
  │  └──────┬───────┘  └──────┬───────┘  └──────────┬──────────┘  │
  │         │                 │                      │             │
  │  ┌──────┴─────────────────┴──────────────────────┴──────────┐ │
  │  │              Existing Engine (SPEC-001–004)               │ │
  │  │  Orchestrator → BrowserOperator → InboxSentinel → CLI    │ │
  │  └──────────────────────────────────────────────────────────┘ │
  └──────────────────────────────────────────────────────────────┘

3.2 Identity Vault (vault.py)

The vault replaces plaintext profile.json with an encrypted file. The file format
is self-describing — salt prepended to ciphertext, no external metadata needed.

3.2.1 File Format

  profile.json.enc:
  ┌──────────────────┬─────────────────────────────────────────┐
  │  16-byte salt    │  Fernet ciphertext (profile JSON bytes) │
  │  (random)        │  (AES-128-CBC + HMAC-SHA256)            │
  └──────────────────┴─────────────────────────────────────────┘

  Total size: 16 bytes + (Fernet overhead + plaintext size)
  Fernet overhead: ~57 bytes (version byte + IV + ciphertext padding + HMAC tag)

3.2.2 Key Derivation

  passphrase ──► PBKDF2HMAC ──► 256-bit key ──► Fernet
                    │
              salt (first 16 bytes of file)
              SHA-256
              600,000 iterations
              (OWASP 2023 recommendation)

  Rationale for 600K iterations: balances security against startup latency.
  On a modern CPU, ~200ms per derivation. Acceptable for a once-per-run cost.

3.2.3 Vault API

  class Vault:
      """
      Encrypted identity vault backed by a single file on disk.

      Usage:
          vault = Vault("profile.json.enc")

          # First run: create vault from profile dict
          vault.create(profile_dict, passphrase)

          # Normal run: unlock and return Profile dataclass
          profile = vault.unlock(passphrase)

          # Change passphrase
          vault.change_passphrase(old_passphrase, new_passphrase)
      """

      def __init__(self, path: str | Path) -> None:
          self._path = Path(path)

      def exists(self) -> bool:
          """True if the vault file already exists on disk."""

      def create(self, profile_dict: dict, passphrase: str) -> Profile:
          """
          Encrypt profile_dict with passphrase, write to self._path.
          Returns the Profile dataclass (validated).
          Raises ProfileValidationError if profile_dict is invalid.
          """

      def unlock(self, passphrase: str) -> Profile:
          """
          Decrypt the vault file with passphrase.
          Returns the Profile dataclass.
          Raises InvalidPassphraseError if passphrase is wrong.
          Raises FileNotFoundError if vault doesn't exist.
          """

      def change_passphrase(self, old_passphrase: str, new_passphrase: str) -> None:
          """
          Re-encrypt the vault with a new passphrase.
          Decrypts with old_passphrase, re-encrypts with new_passphrase.
          Raises InvalidPassphraseError if old_passphrase is wrong.
          """

  class InvalidPassphraseError(ValueError):
      """Raised when vault unlock fails due to wrong passphrase."""

3.2.4 Plaintext Migration

  When main.py starts and finds profile.json (plaintext) but no
  profile.json.enc, it enters migration mode:

    1. Print: "⚠️  Plaintext profile.json detected."
    2. Prompt: "Would you like to encrypt it with a passphrase? [Y/n]"
    3. If yes: enter passphrase → create vault → delete plaintext
    4. If no: exit with a warning (refuse to run with plaintext PII)

  This ensures no existing user data is left behind unencrypted.

3.3 Interactive CLI Wizard

  When no profile.json.enc exists AND --setup flag is passed (or first run
  detected), main.py launches an interactive profile builder using rich.prompt.

  Flow:

    🔒 PrivacyExorcist v1.0 — First Run Setup

    Let's create your encrypted identity vault. Your information never
    leaves this machine. The vault is protected by a passphrase you choose.

    ── Profile ──────────────────────────────────────────────
    First name:     █
    Last name:      █
    Middle name:    █ (optional)
    Street:         █
    City:           █
    State (2-char): █
    ZIP code:       █
    Phone:          █
    Email (for verification links): █
    Birth year:     █ (optional)
    Past ZIPs (comma-separated):    █ (optional)
    Aliases (comma-separated):      █ (optional)

    ── Vault Passphrase ─────────────────────────────────────
    Choose a passphrase to encrypt your vault:
    Passphrase:     █ (hidden)
    Confirm:        █ (hidden)

    ⚠️  IMPORTANT: This passphrase cannot be recovered.
        Write it down or store it in a password manager.
        Without it, you cannot use PrivacyExorcist.

    ✅ Profile validated.
    🔐 Vault encrypted and saved to profile.json.enc
    🗑️  Plaintext deleted.

    Press Enter to begin scrubbing, or Ctrl+C to exit.

  Validation rules (enforced interactively):
    - first_name, last_name, street, city, state, zip, phone, email: required
    - state: exactly 2 uppercase letters
    - zip: 5 digits (basic US format)
    - phone: at least 10 digits (accepts formatting characters)
    - email: contains '@' and '.'
    - birth_year: if provided, 1900–2010
    - Passphrase: min 8 characters, must match confirmation
    - Passphrase confirmation must match

3.4 Startup Script (start.sh)

  #!/usr/bin/env bash
  set -euo pipefail
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cd "$SCRIPT_DIR"

  # Activate virtual environment
  if [ -f .venv/bin/activate ]; then
      source .venv/bin/activate
  else
      echo "❌ Virtual environment not found. Run: python3 -m venv .venv"
      exit 1
  fi

  # Check for cryptography dependency
  python3 -c "import cryptography" 2>/dev/null || {
      echo "❌ cryptography not installed. Run: pip install cryptography"
      exit 1
  }

  # Run the engine, forwarding all arguments
  exec python3 main.py "$@"

  Key behaviors:
    - Works when called from any directory (resolves its own path)
    - Validates venv and dependencies before launching
    - Forwards all arguments (--status, --setup, etc.)
    - exec replaces the shell process (cleaner signal handling)

3.5 --status Flag

  When called with --status, main.py reads the SQLite ledger directly and
  displays a summary table WITHOUT prompting for the vault passphrase.

    $ ./start.sh --status

    📊 PrivacyExorcist — Broker Status
    ┌──────────────┬──────────────────────────┬────────┬────────┐
    │ Broker       │ Status                   │ Retries│ Solves │
    ├──────────────┼──────────────────────────┼────────┼────────┤
    │ thatsthem    │ ✅ SCRUBBED (68.4s)      │ 0      │ 1      │
    │ whitepages   │ 📭 NO_RECORD             │ 0      │ 0      │
    │ nuwber       │ 🚫 CAPTCHA_BLOCKED       │ 2      │ 2      │
    │ spokeo       │ ⏳ QUEUED                 │ 0      │ 0      │
    └──────────────┴──────────────────────────┴────────┴────────┘
    Last updated: 2026-07-21 14:32:01 UTC

  Implementation: reads broker_ledger table via Database class. No vault
  unlock. No profile loading. No orchestrator instantiation.

3.6 --setup Flag

  When called with --setup, main.py forces the interactive profile wizard
  even if a vault already exists.

    $ ./start.sh --setup

    ⚠️  A vault already exists at profile.json.enc.
    This will overwrite it with a new profile.

    Continue? [y/N] y

    [interactive wizard runs]

  If no vault exists, --setup behaves identically to first-run detection.

3.7 Vault Lock After Run

  After the engine completes (or is interrupted), the decrypted Profile
  object goes out of scope. Python's garbage collector handles memory
  cleanup. No explicit "lock" step is needed — the plaintext never touches
  disk between unlock and the process exiting.

  For defense-in-depth, the Vault.unlock() method does NOT cache the
  decrypted data — it returns a Profile that the caller is responsible for.

================================================================================
4. SCENARIO WALKTHROUGHS
================================================================================

4.1 First Run — No Vault, No Profile

  Given: fresh clone, no profile.json, no profile.json.enc
  When:  user runs ./start.sh
  Then:
    1. main.py detects no vault and no plaintext profile
    2. Interactive wizard launches
    3. User fills in all required fields + passphrase
    4. Profile validated via Profile.from_dict()
    5. Vault created: profile.json.enc written to disk
    6. Engine proceeds to broker scrubbing

4.2 Migration — Plaintext Profile Exists

  Given: profile.json exists from pre-vault development, no .enc file
  When:  user runs ./start.sh
  Then:
    1. main.py detects plaintext profile.json, no .enc
    2. Warning displayed: "⚠️  Plaintext profile.json detected."
    3. User prompted to encrypt
    4. User enters passphrase → vault.created from existing profile data
    5. Plaintext profile.json deleted
    6. Engine proceeds

  If user declines encryption: exit code 1 with message explaining risk.

4.3 Normal Run — Vault Exists

  Given: profile.json.enc exists
  When:  user runs ./start.sh
  Then:
    1. vault.unlock(passphrase) prompts for passphrase via getpass
    2. On success: Profile returned, engine starts
    3. On InvalidPassphraseError: "❌ Wrong passphrase. Try again." (max 3 attempts)
    4. After 3 failures: exit code 1

4.4 Status Check — No Passphrase Needed

  Given: profile.json.enc exists, broker_ledger has data
  When:  user runs ./start.sh --status
  Then:
    1. No passphrase prompt
    2. SQLite broker_ledger read directly
    3. Summary table printed to terminal
    4. Exit code 0

4.5 Re-setup — Overwrite Existing Vault

  Given: profile.json.enc exists
  When:  user runs ./start.sh --setup
  Then:
    1. Warning: "This will overwrite your existing vault."
    2. Confirmation prompt
    3. Interactive wizard runs with existing data as defaults
    4. New vault written, old vault replaced

4.6 Wrong Passphrase

  Given: vault exists, user enters wrong passphrase
  When:  vault.unlock(wrong_passphrase) is called
  Then:
    1. Fernet raises InvalidToken (HMAC verification fails)
    2. Vault catches it → raises InvalidPassphraseError("Wrong passphrase")
    3. CLI displays: "❌ Wrong passphrase."
    4. Retry counter incremented
    5. After 3 attempts: "❌ Too many attempts." → exit code 1

4.7 Edge Cases

  - Empty passphrase: Rejected by wizard (min 8 chars). If somehow bypassed,
    Fernet still works — but a warning is logged.
  - Corrupted vault file: Fernet raises InvalidToken. Treated same as wrong
    passphrase (indistinguishable by design — prevents oracle attacks).
  - profile.json AND profile.json.enc both exist: Vault takes priority.
    Plaintext is treated as stale. Warning printed: "⚠️  Both profile.json
    and profile.json.enc exist. Using encrypted vault. Delete profile.json
    if it's no longer needed."
  - start.sh called from outside project dir: Resolves its own path via
    BASH_SOURCE, so cd "$SCRIPT_DIR" always lands in the right place.
  - Non-interactive terminal (piped input, CI): getpass raises EOFError.
    main.py exits with message: "❌ Cannot prompt for passphrase in
    non-interactive mode. Use --status for read-only access."
  - Weak passphrase: Wizard warns if <8 chars but doesn't block. Vault is
    only as strong as the passphrase — documented in wizard text.

================================================================================
5. IMPLEMENTATION PLAN
================================================================================

All files live under the existing project structure.

Phase 1: Identity Vault (~120 lines)

  Files to create:
    privacy_exorcist/vault.py                        (~100 lines)

  vault.py contents:
    - InvalidPassphraseError (custom exception)
    - Vault class (__init__, exists, create, unlock, change_passphrase)
    - _derive_key(passphrase, salt) → bytes (PBKDF2HMAC, 600K iterations)
    - _encrypt(data, key) → bytes (Fernet)
    - _decrypt(ciphertext, key) → bytes (Fernet)

  Test verification:
    - Round-trip: create(profile_dict, pass) → unlock(pass) → same Profile
    - Wrong passphrase → InvalidPassphraseError
    - Corrupted file → InvalidPassphraseError
    - Change passphrase: unlock(old) → change(old, new) → unlock(new)
    - Salt is random (two creates with same passphrase → different files)
    - exists() returns True/False correctly
    - Empty dict → ProfileValidationError propagated

Phase 2: Startup Script (~25 lines)

  Files to create:
    start.sh                                          (~20 lines)

  Test verification:
    - ./start.sh --help prints usage
    - ./start.sh --status works (reads ledger, no passphrase)
    - Called from outside project dir → still works
    - Missing .venv → clear error message

Phase 3: Interactive CLI Wizard (~100 lines)

  Files to create:
    privacy_exorcist/cli/wizard.py                    (~80 lines)

  wizard.py contents:
    - run_profile_wizard(existing_profile=None) → dict
      - If existing_profile provided, use its values as defaults
    - _prompt_required(prompt, default=None, validator=None) → str
    - _prompt_optional(prompt, default=None) → str
    - _prompt_passphrase() → str
    - _validate_state(state) → bool (2-char uppercase)
    - _validate_zip(zip_code) → bool (5 digits)
    - _validate_phone(phone) → bool (10+ digits)
    - _validate_email(email) → bool (contains @ and .)
    - PROFILE_FIELDS ordered list for consistent prompt flow

  Test verification:
    - All required fields prompted and validated
    - Optional fields accept empty input
    - State validation rejects "Texas", accepts "TX"
    - ZIP validation rejects "abcde", accepts "78701"
    - Passphrase mismatch → re-prompt
    - Existing profile populates defaults
    - Returns valid dict that passes Profile.from_dict()

Phase 4: main.py Rewrite (~150 lines)

  Files to modify:
    main.py                                           (rewrite)

  New main.py flow:
    1. Parse args: --status, --setup, --help
    2. If --status: print status table from SQLite → exit 0
    3. Load .env (existing config.py)
    4. Detect vault vs plaintext state:
       a. vault.exists() → unlock(passphrase) → profile
       b. profile.json only → migration prompt
       c. Neither → wizard (create new vault)
       d. --setup flag → wizard (overwrite if exists)
    5. Load playbook (unchanged — playbook.json stays plaintext,
       it contains no PII)
    6. Validate .env config (unchanged)
    7. Print config summary (unchanged)
    8. Run engine loop (unchanged)

  Test verification:
    - First run (no files) → wizard → vault created → engine runs
    - Normal run (vault exists) → passphrase prompt → engine runs
    - --status → ledger table without passphrase
    - --setup → wizard with confirmation prompt
    - Plaintext migration → encrypt + delete plaintext
    - Wrong passphrase 3x → exit 1
    - --help → usage printed

================================================================================
6. TEST VECTORS
================================================================================

  ┌──────┬────────────────────────────────┬────────────────────────────────┐
  │ #    │ Input                          │ Expected Output                │
  ├──────┼────────────────────────────────┼────────────────────────────────┤
  │ TV01 │ Vault.create(valid_dict, pw)   │ profile.json.enc written,      │
  │      │                                │ Profile object returned         │
  │ TV02 │ Vault.unlock(correct_pw)       │ Profile dataclass returned,     │
  │      │ after TV01                     │ fields match original dict      │
  │ TV03 │ Vault.unlock(wrong_pw)         │ InvalidPassphraseError raised   │
  │ TV04 │ Vault.create twice, same       │ Files have different bytes      │
  │      │ profile, same passphrase       │ (different random salts)        │
  │ TV05 │ Vault.change_passphrase(old,   │ unlock(new) succeeds,           │
  │      │ new)                           │ unlock(old) raises error        │
  │ TV06 │ Wizard: all valid inputs       │ Returns dict → Profile.from_    │
  │      │                                │ dict() succeeds                 │
  │ TV07 │ Wizard: state="Texas"          │ Rejected, re-prompt             │
  │ TV08 │ Wizard: zip="abcde"            │ Rejected, re-prompt             │
  │ TV09 │ Wizard: passphrase mismatch    │ Re-prompt both fields           │
  │ TV10 │ Wizard: passphrase < 8 chars   │ Warning shown, accepted         │
  │ TV11 │ --status with populated ledger │ Table printed, no passphrase    │
  │ TV12 │ --status with empty ledger     │ Table printed with 0 rows       │
  │ TV13 │ start.sh from outside dir      │ cd to project root, runs main.py│
  │ TV14 │ start.sh with missing .venv    │ Clear error, exit 1             │
  │ TV15 │ Plaintext profile.json exists, │ Migration prompt, encrypt +     │
  │      │ no .enc                        │ delete if user accepts          │
  │ TV16 │ Both .json and .enc exist      │ Vault used, warning about stale │
  │      │                                │ plaintext file                  │
  │ TV17 │ Non-interactive terminal       │ EOFError → clear message,       │
  │      │ (piped stdin)                  │ suggests --status               │
  │ TV18 │ Corrupted .enc file            │ InvalidPassphraseError          │
  │      │ (flip one byte in ciphertext)  │ (HMAC verification fails)       │
  └──────┴────────────────────────────────┴────────────────────────────────┘

================================================================================
7. INTERFACES
================================================================================

7.1 Vault → Engine

  The Vault produces a Profile dataclass (SPEC-001 models.py). The Orchestrator
  and BrowserOperator are unchanged — they receive a Profile, not a file path.

  Before:
    profile = load_profile("profile.json")        # from loaders.py
    orch = Orchestrator(profile, playbook, db, ...)

  After:
    vault = Vault("profile.json.enc")
    profile = vault.unlock(getpass.getpass("Passphrase: "))
    orch = Orchestrator(profile, playbook, db, ...)

7.2 main.py → CLIFormatter

  Unchanged. CLIFormatter.on_state_change, on_broker_start, etc. receive the
  same objects they always did.

7.3 main.py → config.py

  config.py's load_config() currently calls load_profile() internally. This
  changes: the profile is loaded by main.py via the vault (or wizard), then
  passed into config as an already-validated Profile object.

  AppConfig gains:
    profile: Optional[Profile] = None  # already present
    vault_path: Path = Path("profile.json.enc")  # new

7.4 --status → SQLite

  Direct Database() instantiation reading broker_ledger. No Orchestrator,
  no browser, no vault.

7.5 System Requirements

  Python 3.11+ (unchanged)
  cryptography (already in venv via browser-use dependency chain)
  rich (already in venv via SPEC-004)
  No new pip packages required.

================================================================================
8. OPEN QUESTIONS
================================================================================

  Q1: Should the vault support a "recovery key" in addition to passphrase?
      A recovery key (random 32-byte hex string generated at vault creation)
      could be printed once and stored offline. If the user forgets their
      passphrase, the recovery key decrypts the vault.
      Recommendation: Defer to V1.1. Passphrase-only keeps V1.0 simple.
      Recovery key adds UX complexity (key management) and a second attack
      vector if the printed key is compromised.

  Q2: Should the vault auto-lock after a period of inactivity?
      The vault is a single-operation unlock: user enters passphrase, engine
      runs to completion, process exits. There's no persistent daemon to
      auto-lock. In a future web dashboard (Level 2), session timeouts would
      apply. Not relevant for CLI V1.0.

  Q3: Should we offer a --headless flag on start.sh?
      Already configurable via HEADLESS=true/false in .env. No need for a
      CLI flag. Adding one would create two sources of truth for the same
      setting.

  Q4: What about the playbook.json? Does it need encryption?
      No. The playbook contains broker URLs and CAPTCHA sitekeys — no PII.
      It's a community-maintained public resource. Encrypting it would
      prevent updates.

================================================================================
9. BUILD VERIFICATION CHECKLIST
================================================================================

  [ ] vault.py — Vault class with create, unlock, change_passphrase
  [ ] wizard.py — interactive profile builder with field validation
  [ ] start.sh — one-command launcher with dependency checks
  [ ] main.py — rewritten with vault integration, --status, --setup, migration
  [ ] config.py — updated to accept pre-loaded Profile instead of file path
  [ ] loaders.py — no changes needed (Profile.from_dict() reused by vault)
  [ ] All 18 test vectors pass
  [ ] Round-trip test: create vault → unlock → same Profile fields
  [ ] Wrong passphrase test: 3 attempts → exit 1
  [ ] --status test: ledger displayed without passphrase prompt
  [ ] Migration test: plaintext → encrypted → plaintext deleted
  [ ] Manual test: full end-to-end with real passphrase + engine run
