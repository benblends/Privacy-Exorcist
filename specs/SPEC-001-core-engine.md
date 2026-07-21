================================================================================
SPEC-001: CORE ENGINE & STATE MACHINE
================================================================================
PrivacyExorcist — V1.0
Status: Draft for Review
Date: 2026-07-19
Depends On: PRD v1.1 (spike-validated)
Build Phase: 1 (Foundation — must ship first)

================================================================================
1. PURPOSE
================================================================================

This spec defines the PrivacyExorcist Core Engine: the Boss Orchestrator that
manages the finite state machine (FSM), SQLite persistence layer, task queue,
retry logic, hybrid CAPTCHA branching, and profile.json ingestion.

It is the central nervous system. Sub-agents (Browser Operator, Inbox Sentinel)
and the CLI/HMI layer are external consumers that plug into this engine via
well-defined interfaces. The core engine knows WHAT state transitions are valid,
HOW to persist them, and WHEN to spawn or retire sub-agents — but it never
touches a browser, an IMAP socket, or a terminal color code.

This spec enables the North Star scenario: a developer runs `python main.py`,
the engine ingests their profile, loads the playbook, and walks each broker
through its FSM lifecycle from QUEUED to SCRUBBED (or a terminal state).

================================================================================
2. CODEBASE GROUND TRUTH
================================================================================

2.1 What Exists Already

Spike scripts (throwaway — not production code, but contain validated patterns):

  /home/benblends2/DATA_Broker_Breaker_July_2026/
  ├── capsolver_v3.py          — Working CapSolver direct HTTP integration
  ├── stealth_spike.py         — Working stealth browser config
  ├── spike_runner.py          — Parameterized broker test runner
  ├── e2e_thatsthem.py         — Full end-to-end ThatsThem flow
  └── capsolver_result_thatsthem.json  — Confirmed SUCCESS output

PRD document:

  /home/benblends2/DATA_Broker_Breaker_July_2026/PRD_PrivacyExorcist.txt
  — V1.1 spike-validated, contains FSM diagram, return code taxonomy,
    playbook schema, SQLite schema, anti-bot defense layers.

Framework skill (quirks reference):

  ~/.hermes/skills/software-development/privacy-exorcist-spike-campaign/SKILL.md
  — browser-use v0.13.6 quirks, CapSolver SDK bug, CDP patterns,
    stealth config, anti-bot taxonomy.

Relevant third-party libraries (installed in project .venv):

  browser-use v0.13.6  — Agent, BrowserProfile, Controller, ChatOpenAI (own LLM)
  playwright            — Chromium engine (browser-use wraps this)
  capsolver v1.0.7      — Python SDK (HAS BUG — use direct HTTP instead)
  python-dotenv         — .env loading
  requests              — Direct HTTP for CapSolver API
  sqlite3               — Standard library (persistence)

2.2 What Does NOT Exist (must be created from scratch)

  - Orchestrator class (BossAgent / Orchestrator)
  - FSM implementation (state transitions, guards)
  - SQLite database initialization and CRUD
  - Task queue (ordered broker execution)
  - Retry logic with exponential backoff
  - profile.json schema validation
  - playbook.json loader
  - Hybrid CAPTCHA branching (CapSolver vs HITL path selection)
  - Any production-grade Python files — everything is spike code

2.3 Dependencies

  This spec has zero internal dependencies (it is the foundation).
  SPEC-002 (Browser Operator) depends on this spec's FSM interface.
  SPEC-003 (Inbox Sentinel) depends on this spec's SQLite schema.
  SPEC-004 (CLI/HMI) depends on this spec's state change callbacks.

================================================================================
3. DESIGN
================================================================================

3.1 Architecture

The Core Engine is a single Python module (`src/privacy_exorcist/engine.py`)
containing:

  ┌──────────────────────────────────────────────────┐
  │                 Orchestrator                      │
  │  ┌────────────┐  ┌──────────┐  ┌──────────────┐  │
  │  │ Profile    │  │ Playbook │  │ State        │  │
  │  │ Loader     │  │ Loader   │  │ Machine      │  │
  │  └────────────┘  └──────────┘  └──────┬───────┘  │
  │                                       │          │
  │  ┌───────────────────────────────────┐│          │
  │  │        SQLite Ledger              ││          │
  │  │  (broker states, retry counts,   │◄┘          │
  │  │   CAPTCHA costs, error logs)     │            │
  │  └───────────────────────────────────┘           │
  │  ┌───────────────────────────────────┐           │
  │  │        Task Queue                 │           │
  │  │  (ordered broker execution loop)  │           │
  │  └───────────────────────────────────┘           │
  └──────────────────────────────────────────────────┘

The Orchestrator does NOT contain browser, IMAP, or CLI code. It exposes
callback hooks that sub-agents register:

  on_state_change(broker_id, old_state, new_state) → None
  on_broker_start(broker_id, task_context) → None
  on_broker_complete(broker_id, result) → None

3.2 Finite State Machine

Every broker flows through exactly one of these state paths:

  QUEUED ──► IN_PROGRESS ──► SUBMITTED ──► AWAITING_VERIFICATION ──► SCRUBBED
                  │                │
                  │                ├──► AWAITING_HUMAN_INTERVENTION
                  │                │         │
                  │                │         └──► (back to IN_PROGRESS after human)
                  │                │
                  │                └──► FAILED (retry_count < 3)
                  │                └──► PERMANENTLY_FAILED (retry_count >= 3)
                  │
                  ├──► CAPTCHA_BLOCKED (no CapSolver key, no human available)
                  ├──► BROKER_UNREACHABLE (retry with backoff)
                  ├──► NO_RECORD (terminal — broker has no record for user)
                  └──► FAILED (unexpected error)

State transition guards:

  ┌─────────────────────────┬──────────────────────────────────────────────┐
  │ Transition              │ Guard                                        │
  ├─────────────────────────┼──────────────────────────────────────────────┤
  │ QUEUED → IN_PROGRESS    │ Always allowed (orchestrator starts task)    │
  │ IN_PROGRESS → SUBMITTED │ Browser Operator returns SUCCESS or          │
  │                         │ VERIFICATION_REQUIRED                        │
  │ IN_PROGRESS → NO_RECORD │ Browser Operator returns NO_MATCH_FOUND      │
  │                         │ (terminal — broker has no record. No retry.) │
  │ IN_PROGRESS → AWAITING_ │ Browser Operator returns CAPTCHA_DETECTED    │
  │   HUMAN_INTERVENTION    │ AND CAPSOLVER_API_KEY is not set             │
  │                         │ OR captcha_attempts >= 2 (loop guard)        │
  │ IN_PROGRESS → CAPTCHA_  │ Browser Operator returns CAPTCHA_BLOCKED     │
  │   BLOCKED               │ (CapSolver failed, no HITL possible,         │
  │                         │  or captcha_attempts >= 2 in headless mode)  │
  │ IN_PROGRESS → FAILED    │ Browser Operator returns any retryable       │
  │                         │ error AND retry_count < 3                    │
  │ IN_PROGRESS →           │ Browser Operator returns any error           │
  │   PERMANENTLY_FAILED    │ AND retry_count >= 3                         │
  │ AWAITING_HUMAN →        │ Human confirms CAPTCHA solved (via CLI)      │
  │   IN_PROGRESS            │                                              │
  │ SUBMITTED → AWAITING_   │ VERIFICATION_REQUIRED returned               │
  │   VERIFICATION          │                                              │
  │ AWAITING_VERIFICATION → │ Inbox Sentinel confirms email link clicked   │
  │   SCRUBBED              │                                              │
  └─────────────────────────┴──────────────────────────────────────────────┘

Retryable errors (trigger retry_count increment + backoff):
  BROKER_UNREACHABLE, FORM_SUBMIT_FAILED

Non-retryable / terminal (no retry — direct to terminal state):
  NO_MATCH_FOUND → NO_RECORD (successful negative)
  CAPTCHA_BLOCKED → CAPTCHA_BLOCKED (or PERMANENTLY_FAILED if retries exhausted)
  BLOCKED_403 → FAILED (stealth layer should prevent this)

3.3 Hybrid CAPTCHA Branching

At the point where the FSM receives CAPTCHA_DETECTED, the orchestrator maintains
a per-broker-run `captcha_attempts` counter (reset to 0 at the start of each
broker execution, NOT persisted across retries — this is a single-run loop guard).

  def _handle_captcha(self, broker_id: str, captcha_attempts: int) -> str:
      if captcha_attempts >= 2:
          # Loop guard: CapSolver token was rejected twice.
          # Force fallback to HITL (if headed) or give up (if headless).
          if not self._headless:
              self._set_state(broker_id, "AWAITING_HUMAN_INTERVENTION")
              self._emit_hitl_prompt(broker_id)
              return "AWAITING_HUMAN_INTERVENTION"
          else:
              self._set_state(broker_id, "CAPTCHA_BLOCKED")
              return "CAPTCHA_BLOCKED"

      if self._capsolver_key:
          # Automated path: CapSolver solves, token injected, retry submit.
          # captcha_attempts is incremented before re-entering the loop.
          return "IN_PROGRESS"  # re-enter the submission loop
      elif not self._headless:
          # HITL path: terminal prompt, wait for Enter, retry submit
          self._set_state(broker_id, "AWAITING_HUMAN_INTERVENTION")
          self._emit_hitl_prompt(broker_id)
          return "AWAITING_HUMAN_INTERVENTION"
      else:
          # Headless + no API key = dead end
          self._set_state(broker_id, "CAPTCHA_BLOCKED")
          return "CAPTCHA_BLOCKED"

Loop guard rationale: If CapSolver returns a token the broker rejects (e.g.,
expired sitekey, IP mismatch), browser-use re-renders the page, sees the
CAPTCHA again, returns CAPTCHA_DETECTED, and without this guard the FSM
would loop indefinitely burning tokens. Two attempts is generous enough
for transient failures; beyond that, escalation is required.

3.4 SQLite Schema

  CREATE TABLE IF NOT EXISTS broker_ledger (
      broker_id          TEXT PRIMARY KEY,
      current_status     TEXT NOT NULL DEFAULT 'QUEUED',
      last_run_timestamp TEXT,
      retry_count        INTEGER NOT NULL DEFAULT 0,
      captcha_solves     INTEGER NOT NULL DEFAULT 0,
      error_log          TEXT,
      created_at         TEXT NOT NULL DEFAULT (datetime('now')),
      updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
  );

  CREATE TABLE IF NOT EXISTS run_history (
      id                 INTEGER PRIMARY KEY AUTOINCREMENT,
      broker_id          TEXT NOT NULL,
      run_started        TEXT NOT NULL,
      run_completed      TEXT,
      outcome            TEXT,
      duration_seconds   REAL,
      FOREIGN KEY (broker_id) REFERENCES broker_ledger(broker_id)
  );

Valid current_status values (enforced at application level):
  QUEUED, IN_PROGRESS, SUBMITTED, AWAITING_VERIFICATION,
  AWAITING_HUMAN_INTERVENTION, CAPTCHA_BLOCKED,
  NO_RECORD, SCRUBBED, FAILED, PERMANENTLY_FAILED

3.5 Data Flow

  profile.json ──► ProfileLoader.validate() ──► Orchestrator.profile
  playbook.json ──► PlaybookLoader.load() ──► Orchestrator.playbook

  Orchestrator.run():
    1. For each broker in playbook (ordered by priority):
       a. Set state → IN_PROGRESS
       b. Fire on_broker_start(broker_id, task_context)
       c. Wait for on_broker_complete(broker_id, result)
       d. Apply state transition based on result code
       e. If retryable: requeue with backoff (2^retry_count seconds)
       f. If terminal: next broker
    2. Print summary: X scrubbed, Y failed, Z skipped

================================================================================
4. SCENARIO WALKTHROUGHS
================================================================================

4.1 Happy Path — Direct Form, No CAPTCHA

  Given: profile.json is valid, playbook has a broker with flow_type=DIRECT_FORM
         and known_blockers=[], CAPSOLVER_API_KEY is not set
  When:  orchestrator.run() reaches this broker
  Then:
    1. State changes: QUEUED → IN_PROGRESS
    2. Browser Operator fills form, submits, sees "Request Submitted"
    3. Browser Operator returns SUCCESS
    4. State changes: IN_PROGRESS → SUBMITTED
    5. (If VERIFICATION_REQUIRED: SUBMITTED → AWAITING_VERIFICATION →
       Inbox Sentinel confirms → SCRUBBED)
    6. Orchestrator moves to next broker

4.2 Happy Path — Direct Form, Turnstile CAPTCHA, CapSolver

  Given: profile.json valid, playbook entry for ThatsThem with
         captcha_type=cloudflare_turnstile, CAPSOLVER_API_KEY is set
  When:  orchestrator.run() reaches ThatsThem
  Then:
    1. State: QUEUED → IN_PROGRESS
    2. Browser Operator fills form, clicks submit, sees Turnstile
    3. Browser Operator returns CAPTCHA_DETECTED
    4. Orchestrator checks: CAPSOLVER_API_KEY is set → auto-solve path
    5. CapSolver called → token received → injected → form re-submitted
    6. Browser Operator sees "Request Submitted" → returns SUCCESS
    7. State: IN_PROGRESS → SUBMITTED
    8. captcha_solves incremented by 1 in ledger
    9. Orchestrator moves to next broker

4.3 HITL Fallback — CAPTCHA, No API Key, Headed Mode

  Given: profile.json valid, playbook entry with CAPTCHA,
         CAPSOLVER_API_KEY is not set, HEADLESS=False
  When:  orchestrator.run() reaches broker, Browser Operator hits CAPTCHA
  Then:
    1. State: QUEUED → IN_PROGRESS
    2. Browser Operator fills form, hits CAPTCHA → returns CAPTCHA_DETECTED
    3. Orchestrator checks: no API key, headless=False → HITL path
    4. State: IN_PROGRESS → AWAITING_HUMAN_INTERVENTION
    5. Terminal prompt displayed: "🚨 [ACTION REQUIRED]: Anti-bot gate on
       {broker_id}. Please solve the CAPTCHA in the open Chromium window."
    6. Human presses Enter → Orchestrator changes state back to IN_PROGRESS
    7. Browser Operator resumes, submits form → returns SUCCESS
    8. State: IN_PROGRESS → SUBMITTED

4.4 Retry — Broker Unreachable

  Given: broker seed_url returns 503 or timeout
  When:  Browser Operator returns BROKER_UNREACHABLE
  Then:
    1. State: IN_PROGRESS → FAILED
    2. retry_count incremented
    3. Backoff: 2^retry_count seconds before retry
    4. If retry_count >= 3: FAILED → PERMANENTLY_FAILED (skip broker)
    5. run_history records the attempt

4.5 Edge Cases

  - Empty playbook: Orchestrator prints "No brokers in playbook. Exiting."
    and exits cleanly with code 0.
  - Invalid profile.json: Orchestrator prints validation errors and exits
    with code 1 before any broker is touched.
  - SQLite locked: Retry with 1s backoff, max 3 attempts. If still locked,
    log CRITICAL and exit.
  - Duplicate broker_id in playbook: Last entry wins (warn on stderr).
  - Ctrl+C during run: Set global shutdown flag. Current broker completes
    or aborts. No partial state corruption.
  - CapSolver timeout: Attempts auto-solve 3 times. If all fail, fall
    through to HITL (if headed) or CAPTCHA_BLOCKED (if headless).
  - Retry loop detection: If same broker transitions IN_PROGRESS → FAILED
    5+ times without reaching a terminal state, force PERMANENTLY_FAILED.

================================================================================
5. IMPLEMENTATION PLAN
================================================================================

All files live under:
  /home/benblends2/DATA_Broker_Breaker_July_2026/privacy_exorcist/

Phase 1: Project skeleton + data models (~200 lines)

  Files to create:
    privacy_exorcist/__init__.py           (empty)
    privacy_exorcist/models.py             (~60 lines)

  models.py contents:
    - BrokerState enum (QUEUED, IN_PROGRESS, ..., PERMANENTLY_FAILED)
    - BrokerResult enum (SUCCESS, VERIFICATION_REQUIRED, CAPTCHA_DETECTED,
      CAPTCHA_BLOCKED, BROKER_UNREACHABLE, NO_MATCH_FOUND, MULTIPLE_MATCH,
      FORM_SUBMIT_FAILED, BLOCKED_403)
    - BrokerRecord dataclass (broker_id, current_status, retry_count, ...)
    - Profile dataclass (first_name, last_name, street, city, state,
      zip, phone, sentinel_email, past_zips, birth_year, aliases)
    - PlaybookEntry dataclass (broker_id, seed_url, success_anchor,
      flow_type, captcha_type, captcha_sitekey, protection_layer,
      known_blockers, last_verified, notes)
    - Playbook dataclass (brokers: list[PlaybookEntry])

  Test verification:
    - Unit test: BrokerState enum has exactly 9 members
    - Unit test: BrokerResult enum has exactly 9 members
    - Unit test: Profile.from_dict() validates required fields
    - Unit test: PlaybookEntry.from_dict() handles optional captcha fields

Phase 2: SQLite persistence (~150 lines)

  Files to create:
    privacy_exorcist/database.py           (~120 lines)

  database.py contents:
    - Database class (__init__, connect, close, migrate)
    - _create_tables() — CREATE TABLE IF NOT EXISTS for broker_ledger
      and run_history
    - upsert_broker(broker_id, status, error_log=None)
    - get_broker(broker_id) → BrokerRecord
    - get_all_brokers() → list[BrokerRecord]
    - log_run(broker_id, outcome, duration) → run_id
    - increment_retry(broker_id)
    - increment_captcha_solve(broker_id)

  Test verification:
    - Unit test: create tables, upsert, get, verify state
    - Unit test: retry_count increments correctly
    - Unit test: captcha_solves increments correctly
    - Unit test: run history logged with correct broker_id FK
    - Unit test: concurrent access (two threads) → no corruption

Phase 3: Profile & Playbook loaders (~120 lines)

  Files to create:
    privacy_exorcist/loaders.py            (~100 lines)

  loaders.py contents:
    - load_profile(path) → Profile
      - Reads JSON, validates required fields
      - Raises ProfileValidationError with specific field errors
    - load_playbook(path) → Playbook
      - Reads JSON, validates each entry
      - Deduplicate warning for duplicate broker_id
      - Raises PlaybookValidationError on missing required fields
    - ProfileValidationError, PlaybookValidationError (custom exceptions)

  Test verification:
    - Unit test: valid profile.json → Profile object
    - Unit test: missing first_name → ProfileValidationError
    - Unit test: valid playbook.json → Playbook object
    - Unit test: duplicate broker_id → warning, last wins
    - Unit test: missing seed_url → PlaybookValidationError

Phase 4: Finite State Machine (~180 lines)

  Files to create:
    privacy_exorcist/state_machine.py      (~150 lines)

  state_machine.py contents:
    - StateMachine class
    - VALID_TRANSITIONS dict (from_state → set of valid to_states)
    - transition(current_state, new_state) → new_state
      - Validates transition is legal
      - Raises InvalidStateTransition if not
    - get_next_state(current_state, result_code) → next_state
      - Maps BrokerResult to next FSM state
      - Handles retry logic (retry_count < 3 → FAILED, >= 3 → PERMANENTLY_FAILED)

  Test verification:
    - Unit test: every valid transition in VALID_TRANSITIONS succeeds
    - Unit test: illegal transition raises InvalidStateTransition
    - Unit test: SUCCESS → SUBMITTED
    - Unit test: CAPTCHA_DETECTED + retry_count=0 → FAILED
    - Unit test: CAPTCHA_BLOCKED + retry_count=3 → PERMANENTLY_FAILED
    - Unit test: BROKER_UNREACHABLE + retry_count=0 → FAILED
    - Unit test: all 9 BrokerResult codes mapped to correct next_state

Phase 5: Orchestrator (~250 lines)

  Files to create:
    privacy_exorcist/engine.py             (~200 lines)

  engine.py contents:
    - Orchestrator class
      - __init__(profile_path, playbook_path, capsolver_key, headless)
      - run() → dict (summary: {scrubbed, failed, skipped, total})
      - _process_broker(broker_entry) → None
      - _apply_result(broker_id, result_code) → None
      - _handle_captcha(broker_id) → str (next state)
      - _should_retry(broker_id) → bool
      - _compute_backoff(retry_count) → int (2^count seconds)
    - Callback hooks:
      - on_broker_start: callable(broker_id, task_context) | None
      - on_broker_complete: callable(broker_id, result) | None
      - on_state_change: callable(broker_id, old_state, new_state) | None

  Test verification:
    - Integration test: orchestrator with mock sub-agent, 1 broker → SUBMITTED
    - Integration test: orchestrator with mock sub-agent returning
      BROKER_UNREACHABLE → retry logic → PERMANENTLY_FAILED after 3 retries
    - Integration test: CAPTCHA_DETECTED + capsolver key → auto-solve path
    - Integration test: CAPTCHA_DETECTED + no key + headless=False → HITL path
    - Integration test: Ctrl+C → clean shutdown, no SQLite corruption
    - Integration test: empty playbook → exit code 0
    - Integration test: invalid profile → exit code 1, no brokers touched

================================================================================
6. TEST VECTORS
================================================================================

Each test vector is an (input_state, result_code, retry_count, capsolver_key,
headless) tuple mapped to an expected output_state.

  ┌──────┬─────────────────────┬──────────┬───────┬────────┬──────────────┐
  │ #    │ Input State         │ Result   │ Retry │ CSP Key│ Expected     │
  ├──────┼─────────────────────┼──────────┼───────┼────────┼──────────────┤
  │ TV01 │ IN_PROGRESS         │ SUCCESS  │ 0     │ N/A    │ SUBMITTED    │
  │ TV02 │ IN_PROGRESS         │ VERIF_   │ 0     │ N/A    │ SUBMITTED    │
  │      │                     │ REQUIRED │       │        │              │
  │ TV03 │ IN_PROGRESS         │ CAPTCHA_ │ 0     │ set    │ IN_PROGRESS  │
  │      │                     │ DETECTED │       │        │ (auto-solve) │
  │ TV04 │ IN_PROGRESS         │ CAPTCHA_ │ 0     │ unset  │ AWAITING_    │
  │      │                     │ DETECTED │       │        │ HUMAN        │
  │ TV05 │ IN_PROGRESS         │ CAPTCHA_ │ 2     │ set    │ CAPTCHA_     │
  │      │                     │ BLOCKED  │       │        │ BLOCKED      │
  │ TV06 │ IN_PROGRESS         │ CAPTCHA_ │ 3     │ set    │ PERMANENTLY_ │
  │      │                     │ BLOCKED  │       │        │ FAILED       │
  │ TV07 │ IN_PROGRESS         │ BROKER_  │ 0     │ N/A    │ FAILED       │
  │      │                     │ UNREACH  │       │        │              │
  │ TV08 │ IN_PROGRESS         │ BROKER_  │ 3     │ N/A    │ PERMANENTLY_ │
  │      │                     │ UNREACH  │       │        │ FAILED       │
  │ TV09 │ IN_PROGRESS         │ NO_MATCH │ 0     │ N/A    │ NO_RECORD    │
  │      │                     │ _FOUND   │       │        │ (terminal)   │
  │ TV10 │ AWAITING_HUMAN      │ (human   │ N/A   │ N/A    │ IN_PROGRESS  │
  │      │                     │ confirms)│       │        │              │
  │ TV11 │ SUBMITTED           │ (Inbox   │ N/A   │ N/A    │ SCRUBBED     │
  │      │                     │ confirms)│       │        │              │
  │ TV12 │ IN_PROGRESS         │ FORM_    │ 1     │ N/A    │ FAILED       │
  │      │                     │ SUBMIT_  │       │        │              │
  │      │                     │ FAILED   │       │        │              │
  └──────┴─────────────────────┴──────────┴───────┴────────┴──────────────┘

  TV13: Retry backoff calculation
    retry_count=0 → backoff=1 second
    retry_count=1 → backoff=2 seconds
    retry_count=2 → backoff=4 seconds
    retry_count=3 → PERMANENTLY_FAILED (no backoff — terminal)

  TV14: SQLite CRUD
    Insert broker → upsert_broker("thatsthem", "QUEUED")
    Read back → get_broker("thatsthem").current_status == "QUEUED"
    Update → upsert_broker("thatsthem", "IN_PROGRESS")
    Verify → get_broker("thatsthem").current_status == "IN_PROGRESS"
    Log run → log_run("thatsthem", "SUBMITTED", 68.4)
    Verify history → run_history has 1 row with correct broker_id

  TV15: Profile validation
    Valid JSON with all required fields → Profile object
    Missing "first_name" → ProfileValidationError("first_name is required")
    Missing "sentinel_email" → ProfileValidationError("sentinel_email is required")
    Empty "current_zip" → ProfileValidationError("current_zip must not be empty")

  TV16: CAPTCHA loop guard (captcha_attempts counter)
    captcha_attempts=0 + CAPTCHA_DETECTED + CapSolver set + headed
      → IN_PROGRESS (auto-solve path, increment captcha_attempts)
    captcha_attempts=1 + CAPTCHA_DETECTED + CapSolver set + headed
      → IN_PROGRESS (second auto-solve attempt, increment captcha_attempts)
    captcha_attempts=2 + CAPTCHA_DETECTED + CapSolver set + headed
      → AWAITING_HUMAN_INTERVENTION (loop guard triggered, fallback to HITL)
    captcha_attempts=2 + CAPTCHA_DETECTED + CapSolver set + headless
      → CAPTCHA_BLOCKED (loop guard triggered, no HITL available)
    captcha_attempts=0 + CAPTCHA_DETECTED + no CapSolver + headed
      → AWAITING_HUMAN_INTERVENTION (immediate HITL, counter irrelevant)

================================================================================
7. INTERFACES
================================================================================

7.1 Orchestrator → Browser Operator (SPEC-002 contract)

  Orchestrator calls on_broker_start(broker_id, task_context):
    task_context = {
        "broker_id": str,
        "seed_url": str,
        "profile": Profile (dataclass),
        "playbook_entry": PlaybookEntry (dataclass),
        "capsolver_key": str | None,
        "headless": bool,
    }

  Browser Operator returns to Orchestrator via on_broker_complete:
    result = {
        "broker_id": str,
        "outcome": str,       # BrokerResult enum value
        "duration_seconds": float,
        "final_state": str,   # Agent's final result text (truncated to 3000 chars)
        "captcha_solved": bool,
        "error": str | None,
    }

7.2 Orchestrator → Inbox Sentinel (SPEC-003 contract)

  Orchestrator sets broker state to AWAITING_VERIFICATION.
  Inbox Sentinel polls IMAP, finds verification email, clicks link.
  Inbox Sentinel calls on_broker_complete(broker_id, result) with
    outcome="SCRUBBED" (on success) or outcome="FAILED" (on timeout).

7.3 Orchestrator → CLI/HMI (SPEC-004 contract)

  Orchestrator fires on_state_change(broker_id, old_state, new_state).
  CLI listens for these and formats appropriate terminal output.
  CLI also listens for HITL prompts (when state → AWAITING_HUMAN).

================================================================================
8. OPEN QUESTIONS
================================================================================

  Q1: Should the orchestrator run brokers sequentially or in parallel?
      Sequential is safer (browser sessions don't conflict, SQLite is
      single-writer). Parallel would require browser pool management.
      Recommendation: V1.0 sequential. V1.1 parallel if needed.

  Q2: Should profile.json support multiple identities?
      PRD says single-tenant for V1.0. But the schema supports it trivially
      (add profile_id FK to broker_ledger). Defer to V1.1.

  Q3: How should the orchestrator handle brokers missing from the ledger
      (first run)? Auto-insert with status QUEUED for every playbook entry.

================================================================================
9. BUILD VERIFICATION CHECKLIST
================================================================================

  [ ] models.py — BrokerState, BrokerResult enums + dataclasses
  [ ] database.py — SQLite init, CRUD, migration path
  [ ] loaders.py — profile.json + playbook.json validation
  [ ] state_machine.py — FSM with transition guards
  [ ] engine.py — Orchestrator with callback hooks
  [ ] All 15 test vectors pass
  [ ] Integration test: orchestrator with mock sub-agent, full broker lifecycle
  [ ] Ctrl+C clean shutdown verified
  [ ] Empty/invalid input handling verified
