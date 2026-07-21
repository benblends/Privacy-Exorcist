================================================================================
SPEC-004: CLI, HMI & CONFIGURATION
================================================================================
PrivacyExorcist — V1.0
Status: Draft for Review
Date: 2026-07-19
Depends On: SPEC-001 (Core Engine) — state change callbacks, .env schema
             SPEC-002 (Browser Operator) — execution output
             SPEC-003 (Inbox Sentinel) — verification output
Build Phase: 4 (UX Layer — last to build, first thing the user sees)

================================================================================
1. PURPOSE
================================================================================

This spec defines the command-line interface, human-machine interaction layer,
and configuration management for PrivacyExorcist. It is the user's window into
the engine — the only surface they interact with.

It covers:
  - Color-coded terminal output via Python's `rich` library
  - Structured reasoning feed showing what the agent is doing
  - Human-in-the-Loop (HITL) CAPTCHA prompts
  - Global panic switch (Ctrl+C graceful shutdown)
  - .env configuration schema with all required keys
  - HEADLESS mode toggle for visual auditing
  - The main entry point (`main.py`) that wires everything together

This spec contains zero business logic. It is pure presentation and orchestration.

================================================================================
2. CODEBASE GROUND TRUTH
================================================================================

2.1 What Exists Already

Spike scripts (throwaway HMI patterns):
  hybrid_spike.py — HITL terminal prompt with ASCII banner:
    "🖐️  HUMAN-IN-THE-LOOP: CAPTCHA detected."
    "   Press Enter after solving the CAPTCHA..."

  capsolver_v3.py — print-based status output:
    "🤖 CapSolver v3: Solving Turnstile on {page_url}"
    "✅ Token received (542 chars)"

PRD v1.1 §5.6 (HMI Requirements):
  REQ-012: Color-coded terminal output with [REASONING], [ACTION], [CAPTCHA],
           [SUCCESS] prefixes.
  REQ-013: HEADLESS=False support for visual auditing.
  REQ-014: Global panic switch (Ctrl+C).

Available libraries:
  rich (terminal formatting — tables, colors, progress bars, live displays)
  logging (Python stdlib — already used by browser-use)
  python-dotenv (already used, loads ~/.hermes/.env)
  os.environ (stdlib)

2.2 What Does NOT Exist

  - CLI formatter class (wraps rich for consistent output)
  - HITL prompt handler
  - Signal handler for Ctrl+C
  - main.py entry point
  - .env schema documentation / validation
  - Color theme / style constants

2.3 Dependencies

  SPEC-001 (Core Engine) — Orchestrator.on_state_change callback,
    Orchestrator.on_hitl_prompt callback.
  SPEC-002 (Browser Operator) — terminal output during agent execution.
  SPEC-003 (Inbox Sentinel) — verification status messages.
  No new external dependencies beyond `rich` (already installable).

================================================================================
3. DESIGN
================================================================================

3.1 Architecture

  ┌─────────────────────────────────────────────────────────┐
  │                     main.py                              │
  │  ┌──────────────┐  ┌────────────┐  ┌─────────────────┐  │
  │  │ Config Loader │  │ CLI Engine │  │ Signal Handler  │  │
  │  │ (.env +       │  │ (rich      │  │ (Ctrl+C →       │  │
  │  │  profile.json │  │  formatter)│  │  graceful exit) │  │
  │  └──────┬───────┘  └─────┬──────┘  └────────┬────────┘  │
  │         │                │                   │           │
  │  ┌──────┴────────────────┴───────────────────┴────────┐  │
  │  │              Orchestrator (SPEC-001)                │  │
  │  │  ┌──────────┐  ┌──────────────┐  ┌──────────────┐  │  │
  │  │  │ Browser  │  │ Inbox        │  │              │  │  │
  │  │  │ Operator │  │ Sentinel     │  │              │  │  │
  │  │  │(SPEC-002)│  │ (SPEC-003)   │  │              │  │  │
  │  │  └──────────┘  └──────────────┘  └──────────────┘  │  │
  │  └─────────────────────────────────────────────────────┘  │
  └─────────────────────────────────────────────────────────┘

main.py is thin — ~100 lines. It loads config, creates the Orchestrator,
registers CLI callbacks, wires the signal handler, and calls orchestrator.run().

3.2 CLI Formatter (rich-based)

  class CLIFormatter:
      """Consistent terminal output using rich library."""

      STYLES = {
          "info":       "bold white",
          "success":    "bold green",
          "warning":    "bold yellow",
          "error":      "bold red",
          "captcha":    "bold magenta",
          "reasoning":  "dim cyan",
          "action":     "cyan",
          "broker":     "bold blue",
          "summary":    "bold white on blue",
      }

      def __init__(self):
          self.console = Console()
          self.progress = None  # Optional Progress bar for multi-broker runs

      def print_header(self):
          """Print ASCII banner on startup."""
          self.console.print()
          self.console.print("🔒 PrivacyExorcist v1.0", style="bold white")
          self.console.print("   Local-first data broker opt-out engine", style="dim")
          self.console.print()

      def print_config_summary(self, profile, playbook, config):
          """Print loaded configuration."""
          table = Table(title="Configuration")
          table.add_column("Setting", style="dim")
          table.add_column("Value")
          table.add_row("Profile", f"{profile.first_name} {profile.last_name}")
          table.add_row("Sentinel Email", profile.sentinel_email)
          table.add_row("Brokers in Playbook", str(len(playbook.brokers)))
          table.add_row("CapSolver", "✅ Enabled" if config.capsolver_key else "❌ Disabled (HITL)")
          table.add_row("Headless", "✅ Yes" if config.headless else "❌ No (visual audit mode)")
          self.console.print(table)
          self.console.print()

      def on_state_change(self, broker_id, old_state, new_state):
          """Called by Orchestrator when broker state changes."""
          icon_map = {
              "QUEUED": "⏳", "IN_PROGRESS": "🔄", "SUBMITTED": "📤",
              "SCRUBBED": "✅", "AWAITING_VERIFICATION": "📧",
              "AWAITING_HUMAN_INTERVENTION": "🖐️",
              "NO_RECORD": "📭", "CAPTCHA_BLOCKED": "🚫",
              "FAILED": "❌", "PERMANENTLY_FAILED": "💀",
          }
          icon = icon_map.get(new_state, "•")
          self.console.print(
              f"  {icon} [{broker_id}] {old_state} → {new_state}",
              style=self.STYLES.get("info", "")
          )

      def on_broker_start(self, broker_id, task_context):
          """Called when Orchestrator starts processing a broker."""
          self.console.rule(f"[bold blue]{broker_id}[/bold blue]")
          self.console.print(
              f"  🎯 Target: {task_context.get('seed_url', 'unknown')}",
              style="dim"
          )

      def on_broker_complete(self, broker_id, result):
          """Called when Orchestrator finishes a broker."""
          outcome = result.get("outcome", "UNKNOWN")
          duration = result.get("duration_seconds", 0)
          style = self.STYLES.get(
              "success" if outcome == "SUCCESS" else "warning"
          )
          self.console.print(
              f"  {'✅' if outcome == 'SUCCESS' else '⚠️'} "
              f"{broker_id}: {outcome} ({duration:.1f}s)",
              style=style
          )

      def on_agent_reasoning(self, message):
          """Called by Browser Operator when agent emits reasoning."""
          self.console.print(f"    [REASONING] {message}", style=self.STYLES["reasoning"])

      def on_agent_action(self, message):
          """Called by Browser Operator when agent performs action."""
          self.console.print(f"    [ACTION] {message}", style=self.STYLES["action"])

      def on_hitl_prompt(self, broker_id):
          """Called by Orchestrator when HITL CAPTCHA intervention needed."""
          self.console.print()
          self.console.rule("[bold yellow]🖐️  HUMAN-IN-THE-LOOP[/bold yellow]")
          self.console.print(
              f"[bold yellow]🚨 [ACTION REQUIRED]:[/bold yellow] "
              f"Anti-bot gate triggered on [bold]{broker_id}[/bold]."
          )
          self.console.print(
              "   The agent has filled all form fields. "
              "Please solve the CAPTCHA in the open Chromium window."
          )
          self.console.print(
              "   [dim]Press Enter when done to let the agent continue...[/dim]"
          )
          self.console.print()
          input()  # Blocking — OK in HITL mode (foreground, headed)

      def print_run_summary(self, summary):
          """Print final summary after all brokers processed."""
          self.console.rule("[bold]Run Complete[/bold]")
          table = Table(title="Summary")
          table.add_column("Status", style="bold")
          table.add_column("Count")
          table.add_row("✅ Scrubbed", str(summary.get("scrubbed", 0)))
          table.add_row("📭 No Record", str(summary.get("no_record", 0)))
          table.add_row("❌ Failed", str(summary.get("failed", 0)))
          table.add_row("🚫 Skipped (CAPTCHA)", str(summary.get("skipped", 0)))
          table.add_row("📊 Total", str(summary.get("total", 0)))
          self.console.print(table)

3.3 HITL Prompt Flow

The HITL prompt is the only blocking user interaction in the system:

  1. Orchestrator sets state → AWAITING_HUMAN_INTERVENTION
  2. Orchestrator calls CLIFormatter.on_hitl_prompt(broker_id)
  3. Terminal displays the action-required banner
  4. input() blocks the main thread
  5. User solves CAPTCHA in the visible browser window
  6. User presses Enter
  7. CLIFormatter returns control to Orchestrator
  8. Orchestrator changes state → IN_PROGRESS (retry submission)

HITL mode requires HEADLESS=False. If the engine is running in headless
mode and a HITL prompt is triggered, the banner still displays but
explains that no browser window is visible and the broker will be skipped.

3.4 Panic Switch (Ctrl+C Handler)

  import signal

  class SignalHandler:
      def __init__(self):
          self._shutdown_requested = False
          self._original_sigint = signal.getsignal(signal.SIGINT)

      def install(self, orchestrator):
          """Install Ctrl+C handler that triggers graceful shutdown."""
          def handler(signum, frame):
              if self._shutdown_requested:
                  # Second Ctrl+C — force exit
                  print("\n💀 Force quitting...")
                  os._exit(1)

              self._shutdown_requested = True
              print("\n🛑 Shutting down gracefully...")
              print("   (Press Ctrl+C again to force quit)")
              orchestrator.request_shutdown()

          signal.signal(signal.SIGINT, handler)

      def restore(self):
          signal.signal(signal.SIGINT, self._original_sigint)

Graceful shutdown sequence:
  1. orchestrator.request_shutdown() sets a shutdown flag
  2. Current broker's agent.run() is allowed to finish (or timeout at 30s)
  3. Browser session closed cleanly
  4. IMAP connection disconnected
  5. SQLite flushed and closed
  6. Final summary printed
  7. Exit code 0

3.5 Configuration Schema (.env)

  # ── Required ──────────────────────────────────────────────────
  OPENAI_API_KEY=sk-...           # LLM vision model (gpt-4o)
  SENTINEL_EMAIL=jane.optout+sentinel@domain.com

  # ── Optional: CapSolver (fully automated CAPTCHA solving) ─────
  CAPSOLVER_API_KEY=CAP-...       # If absent, HITL mode used

  # ── Optional: IMAP (verification email pipeline) ──────────────
  IMAP_SERVER=imap.gmail.com
  IMAP_PORT=993
  IMAP_USERNAME=jane.optout+sentinel@gmail.com
  IMAP_PASSWORD=xxxx              # App-specific password

  # ── Optional: Runtime behavior ────────────────────────────────
  HEADLESS=true                   # false = visible browser for auditing
  LOG_LEVEL=INFO                  # DEBUG, INFO, WARNING, ERROR

Validation rules:
  - OPENAI_API_KEY is REQUIRED. Engine refuses to start without it.
  - IMAP_* variables are ALL required if any is set (all-or-nothing).
  - If CAPSOLVER_API_KEY is absent, HITL mode is active and a warning
    is printed at startup.
  - HEADLESS defaults to true if not set.
  - LOG_LEVEL defaults to INFO.

3.6 Profile Schema (profile.json)

  {
      "first_name": "Jane",           // Required
      "last_name": "Doe",             // Required
      "middle_name": "Alex",          // Optional
      "aliases": ["Jane A. Smith"],   // Optional
      "current_street": "123 Main St",// Required
      "current_city": "Austin",       // Required
      "current_state": "TX",          // Required
      "current_zip": "78701",         // Required
      "current_phone": "512-555-0147",// Required
      "past_zips": ["90210"],         // Optional
      "birth_year": 1988,             // Optional
      "sentinel_email": "jane.optout+sentinel@domain.com"  // Required
  }

Validation rules (enforced by SPEC-001 ProfileLoader):
  - first_name, last_name, current_street, current_city, current_state,
    current_zip, current_phone, sentinel_email are REQUIRED.
  - current_zip must match /^\d{5}(-\d{4})?$/ (US ZIP format).
  - sentinel_email must match /^[^@]+@[^@]+\.[^@]+$/ (basic email format).
  - birth_year must be between 1900 and 2010 if provided.

3.7 main.py Entry Point

  #!/usr/bin/env python3
  """PrivacyExorcist — local-first data broker opt-out engine."""

  import asyncio
  import sys
  from pathlib import Path
  from dotenv import load_dotenv

  # Load environment BEFORE any imports that touch os.environ
  load_dotenv(Path.home() / ".hermes" / ".env")

  from privacy_exorcist.engine import Orchestrator
  from privacy_exorcist.cli import CLIFormatter, SignalHandler, load_config


  async def main():
      # 1. Load and validate configuration
      config = load_config()
      if config.errors:
          for err in config.errors:
              print(f"❌ {err}", file=sys.stderr)
          sys.exit(1)

      # 2. Initialize CLI
      cli = CLIFormatter()
      cli.print_header()
      cli.print_config_summary(config.profile, config.playbook, config)

      # 3. Create orchestrator
      orch = Orchestrator(
          profile_path=config.profile_path,
          playbook_path=config.playbook_path,
          capsolver_key=config.capsolver_key,
          headless=config.headless,
          openai_key=config.openai_key,
          imap_config=config.imap_config,
      )

      # 4. Wire CLI callbacks
      orch.on_state_change = cli.on_state_change
      orch.on_broker_start = cli.on_broker_start
      orch.on_broker_complete = cli.on_broker_complete
      orch.on_hitl_prompt = cli.on_hitl_prompt

      # 5. Install signal handler
      signal_handler = SignalHandler()
      signal_handler.install(orch)

      try:
          # 6. Run the engine
          summary = await orch.run()
          cli.print_run_summary(summary)
      finally:
          signal_handler.restore()

      return 0 if summary.get("failed", 0) == 0 else 1


  if __name__ == "__main__":
      sys.exit(asyncio.run(main()))

================================================================================
4. SCENARIO WALKTHROUGHS
================================================================================

4.1 Happy Path — Single Broker, CapSolver, Full Automation

  Given: .env has OPENAI_API_KEY + CAPSOLVER_API_KEY, HEADLESS=true,
         profile.json valid, playbook has 1 broker (ThatsThem)
  When:  User runs `python main.py`
  Then:
    1. CLI prints ASCII banner + config summary
    2. "CapSolver: ✅ Enabled" shown
    3. Orchestrator processes ThatsThem
    4. Terminal shows: 🔄 [thatsthem] QUEUED → IN_PROGRESS
    5. Agent reasoning/action messages scroll by
    6. CapSolver solves Turnstile
    7. Terminal shows: ✅ [thatsthem] IN_PROGRESS → SUBMITTED
    8. Terminal shows: ✅ [thatsthem] SUBMITTED → SCRUBBED
    9. Summary table: 1 Scrubbed, 0 Failed
    10. Exit code 0

4.2 HITL Path — No CapSolver Key, Headed Mode

  Given: .env has OPENAI_API_KEY but NO CAPSOLVER_API_KEY, HEADLESS=false,
         visible Chromium window open
  When:  User runs `python main.py`, broker has Turnstile CAPTCHA
  Then:
    1. CLI prints "CapSolver: ❌ Disabled (HITL)" at startup
    2. Agent fills form, hits CAPTCHA
    3. Terminal shows HITL banner with broker name
    4. User clicks Turnstile checkbox in visible browser
    5. User presses Enter
    6. Agent resumes, submits form
    7. Broker completes successfully

4.3 Ctrl+C Mid-Run

  Given: Orchestrator processing Whitepages (60s deep into flow)
  When:  User presses Ctrl+C
  Then:
    1. Signal handler fires
    2. Terminal prints: "🛑 Shutting down gracefully..."
    3. Current agent.run() is allowed to complete or times out at 30s
    4. Browser session closed
    5. SQLite state flushed
    6. Summary printed with current progress
    7. Exit code 0

  When: User presses Ctrl+C AGAIN during shutdown
  Then: Terminal prints "💀 Force quitting..." → os._exit(1)

4.4 Invalid Configuration

  Given: profile.json missing "sentinel_email"
  When:  User runs `python main.py`
  Then:
    1. load_config() returns errors list
    2. CLI prints: "❌ sentinel_email is required"
    3. Engine does NOT start
    4. Exit code 1

4.5 Missing API Key

  Given: .env has no OPENAI_API_KEY
  When:  User runs `python main.py`
  Then:
    1. load_config() returns error: "OPENAI_API_KEY is required"
    2. Exit code 1

4.6 Edge Cases

  - Empty playbook: Engine prints "No brokers in playbook. Exiting." and
    exits code 0 (not an error).
  - IMAP partially configured: If IMAP_SERVER is set but IMAP_PASSWORD
    is not, config validation fails with specific error.
  - Terminal width < 80 chars: rich auto-wraps. No special handling needed.
  - Piped output (not a TTY): rich detects non-TTY and strips formatting.
    Plain text output still readable.
  - Windows terminal: rich handles Windows Terminal, cmd.exe, PowerShell.
    No platform-specific code needed.

================================================================================
5. IMPLEMENTATION PLAN
================================================================================

All files under:
  /home/benblends2/DATA_Broker_Breaker_July_2026/privacy_exorcist/

Phase 1: CLI formatter (~150 lines)

  Files to create:
    privacy_exorcist/cli/__init__.py               (empty)
    privacy_exorcist/cli/formatter.py              (~120 lines)

  formatter.py contents:
    - CLIFormatter class with all formatting methods
    - STYLES constants
    - print_header, print_config_summary, on_state_change,
      on_broker_start, on_broker_complete, on_hitl_prompt,
      on_agent_reasoning, on_agent_action, print_run_summary

  Test verification:
    - Unit test: print_config_summary renders table without crashing
    - Unit test: on_state_change produces expected icon for each state
    - Unit test: on_hitl_prompt format string contains broker_id
    - Unit test: print_run_summary renders all summary fields

Phase 2: Configuration loader (~100 lines)

  Files to create:
    privacy_exorcist/cli/config.py                  (~80 lines)

  config.py contents:
    - AppConfig dataclass (profile, playbook, capsolver_key, headless,
      openai_key, imap_config, errors)
    - load_config() → AppConfig
    - validate_profile(profile) → list[str] (errors)
    - validate_env() → list[str] (errors)
    - validate_imap_config() → list[str] (errors)

  Test verification:
    - Unit test: valid .env → AppConfig with no errors
    - Unit test: missing OPENAI_API_KEY → error in errors list
    - Unit test: invalid profile.json → error in errors list
    - Unit test: partial IMAP config → error in errors list
    - Unit test: HEADLESS not set → defaults to True

Phase 3: Signal handler (~50 lines)

  Files to create:
    privacy_exorcist/cli/signals.py                 (~40 lines)

  signals.py contents:
    - SignalHandler class (install, restore, _handler)
    - Double-Ctrl+C detection (force quit)

  Test verification:
    - Integration test: send SIGINT → shutdown flag set, graceful exit
    - Integration test: send SIGINT twice → os._exit(1) called

Phase 4: Main entry point (~80 lines)

  Files to create:
    main.py                                         (~60 lines)
      (in project root: ~/DATA_Broker_Breaker_July_2026/main.py)

  main.py contents:
    - async main() function
    - Config loading, CLI setup, orchestrator wiring, signal handler
    - if __name__ == "__main__": asyncio.run(main())

  Test verification:
    - Integration test: main.py with valid config → runs without crash
    - Integration test: main.py with invalid config → exits code 1
    - Integration test: main.py with empty playbook → exits code 0
    - Manual test: run main.py against ThatsThem with real API keys

================================================================================
6. TEST VECTORS
================================================================================

  ┌──────┬──────────────────────────────┬──────────────────────────────────┐
  │ #    │ Input                        │ Expected Output                  │
  ├──────┼──────────────────────────────┼──────────────────────────────────┤
  │ TV01 │ State change:                 │ Terminal: "🔄 [whitepages]      │
  │      │ broker_id="whitepages",       │ QUEUED → IN_PROGRESS"           │
  │      │ old=QUEUED, new=IN_PROGRESS   │                                  │
  ├──────┼──────────────────────────────┼──────────────────────────────────┤
  │ TV02 │ State change:                 │ Terminal: "✅ [thatsthem]        │
  │      │ broker_id="thatsthem",        │ SUBMITTED → SCRUBBED"            │
  │      │ old=SUBMITTED, new=SCRUBBED   │                                  │
  ├──────┼──────────────────────────────┼──────────────────────────────────┤
  │ TV03 │ HITL prompt:                  │ Terminal banner with "🖐️        │
  │      │ broker_id="nuwber"            │ HUMAN-IN-THE-LOOP", broker name │
  │      │                              │ "nuwber", and input() prompt     │
  ├──────┼──────────────────────────────┼──────────────────────────────────┤
  │ TV04 │ Config: OPENAI_API_KEY unset  │ Error: "OPENAI_API_KEY is       │
  │      │                              │ required" → exit code 1          │
  ├──────┼──────────────────────────────┼──────────────────────────────────┤
  │ TV05 │ Config: valid .env, valid     │ No errors → engine starts       │
  │      │ profile.json, valid playbook  │                                  │
  ├──────┼──────────────────────────────┼──────────────────────────────────┤
  │ TV06 │ Config: IMAP_SERVER set but   │ Error: "IMAP_PORT is required   │
  │      │ IMAP_PORT not set             │ when IMAP is configured"         │
  ├──────┼──────────────────────────────┼──────────────────────────────────┤
  │ TV07 │ Config: HEADLESS not set      │ Defaults to True (headless)     │
  ├──────┼──────────────────────────────┼──────────────────────────────────┤
  │ TV08 │ Profile: valid JSON, all      │ No errors → Profile object      │
  │      │ required fields present       │ created                          │
  ├──────┼──────────────────────────────┼──────────────────────────────────┤
  │ TV09 │ Profile: missing "first_name" │ Error: "first_name is required" │
  ├──────┼──────────────────────────────┼──────────────────────────────────┤
  │ TV10 │ Profile: zip="abcde"          │ Error: "current_zip must be     │
  │      │ (non-numeric)                 │ 5-digit US ZIP code"            │
  ├──────┼──────────────────────────────┼──────────────────────────────────┤
  │ TV11 │ Ctrl+C during run             │ "🛑 Shutting down gracefully..." │
  │      │                              │ → graceful exit, exit code 0     │
  ├──────┼──────────────────────────────┼──────────────────────────────────┤
  │ TV12 │ Ctrl+C twice during shutdown  │ "💀 Force quitting..."           │
  │      │                              │ → immediate exit                 │
  ├──────┼──────────────────────────────┼──────────────────────────────────┤
  │ TV13 │ Run summary: 3 scrubbed,      │ Table: 3 ✅ Scrubbed,            │
  │      │ 1 no_record, 2 failed,        │ 1 📭 No Record, 2 ❌ Failed,     │
  │      │ 1 skipped (CAPTCHA)           │ 1 🚫 Skipped, 7 📊 Total        │
  └──────┴──────────────────────────────┴──────────────────────────────────┘

================================================================================
7. INTERFACES
================================================================================

7.1 Input

  .env file at ~/.hermes/.env:
    - OPENAI_API_KEY (required)
    - CAPSOLVER_API_KEY (optional)
    - IMAP_SERVER, IMAP_PORT, IMAP_USERNAME, IMAP_PASSWORD (optional)
    - HEADLESS (optional, default true)
    - LOG_LEVEL (optional, default INFO)

  profile.json (path passed via command line or default location):
    - All fields per §3.6 schema

  playbook.json (path passed via command line or default location):
    - Schema per SPEC-001 / PRD §5.2

7.2 Output

  Terminal (stdout): Rich-formatted output with colors, icons, tables.
  Terminal (stderr): Validation errors, crash traces.
  Exit codes: 0 = success, 1 = configuration error or run failures.

7.3 External Libraries

  rich — terminal formatting (tables, colors, panels, rules, progress)
  python-dotenv — .env loading (already used)
  signal (stdlib) — SIGINT handler
  asyncio (stdlib) — event loop

================================================================================
8. OPEN QUESTIONS
================================================================================

  Q1: Should main.py accept command-line arguments?
      Minimal V1.0: profile.json and playbook.json paths are hardcoded
      or read from .env. Recommendation: add --profile and --playbook
      CLI args in V1.1 for multi-profile support.

  Q2: Should the CLI support a --dry-run mode?
      Would print what brokers would be targeted without executing.
      Recommendation: V1.1 feature. Low priority.

  Q3: Should agent reasoning output be toggleable?
      Currently always shown. Power users may want --quiet mode.
      Recommendation: Add --quiet flag in V1.1.

================================================================================
9. BUILD VERIFICATION CHECKLIST
================================================================================

  [ ] formatter.py — CLIFormatter with all output methods
  [ ] config.py — AppConfig, load_config(), validation
  [ ] signals.py — SignalHandler with double-Ctrl+C detection
  [ ] main.py — entry point wiring everything together
  [ ] All 13 test vectors pass
  [ ] Integration test: main.py with valid config → engine runs
  [ ] Integration test: main.py with invalid config → exit code 1
  [ ] Manual test: run with real API keys against ThatsThem
  [ ] Manual test: HEADLESS=false → visible Chromium window
  [ ] Manual test: Ctrl+C → graceful shutdown
