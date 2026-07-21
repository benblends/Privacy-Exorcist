"""
Rich-based terminal formatter for PrivacyExorcist.

SPEC-004 §3.2 + §5 Phase 1: Color-coded CLI output with icons,
tables, rules, and structured state-change rendering.

Provides callbacks that plug directly into Orchestrator hooks:
  on_state_change, on_broker_start, on_broker_complete, on_hitl_prompt.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from privacy_exorcist.engine import BrokerRunResult, TaskContext
from privacy_exorcist.models import BrokerState, Playbook, Profile


# ── Styles ──────────────────────────────────────────────────────────────────

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

STATE_ICONS = {
    "QUEUED": "⏳",
    "IN_PROGRESS": "🔄",
    "SUBMITTED": "📤",
    "SCRUBBED": "✅",
    "AWAITING_VERIFICATION": "📧",
    "AWAITING_HUMAN_INTERVENTION": "🖐️",
    "NO_RECORD": "📭",
    "CAPTCHA_BLOCKED": "🚫",
    "FAILED": "❌",
    "PERMANENTLY_FAILED": "💀",
}


# ── CLIFormatter ────────────────────────────────────────────────────────────

class CLIFormatter:
    """Consistent rich-based terminal output for the PrivacyExorcist CLI.

    Usage:
        cli = CLIFormatter()
        cli.print_header()
        cli.print_config_summary(profile, playbook, capsolver_key=..., headless=...)
        orch.on_state_change = cli.on_state_change
        orch.on_hitl_prompt = cli.on_hitl_prompt
    """

    def __init__(self) -> None:
        self.console = Console()

    # ── Header ──────────────────────────────────────────────────────────

    def print_header(self) -> None:
        """Print ASCII banner on startup."""
        self.console.print()
        self.console.print("🔒 PrivacyExorcist v1.0", style="bold white")
        self.console.print(
            "   Local-first data broker opt-out engine", style="dim"
        )
        self.console.print()

    # ── Config Summary ───────────────────────────────────────────────────

    def print_config_summary(
        self,
        profile: Profile,
        playbook: Playbook,
        *,
        capsolver_key: str | None = None,
        headless: bool = True,
    ) -> None:
        """Print loaded configuration as a rich Table."""
        table = Table(title="Configuration")
        table.add_column("Setting", style="dim")
        table.add_column("Value")
        table.add_row(
            "Profile", f"{profile.first_name} {profile.last_name}"
        )
        table.add_row("Sentinel Email", profile.sentinel_email)
        table.add_row("Brokers in Playbook", str(len(playbook)))
        table.add_row(
            "CapSolver",
            "✅ Enabled" if capsolver_key else "❌ Disabled (HITL)",
        )
        table.add_row(
            "Headless",
            "✅ Yes" if headless else "❌ No (visual audit mode)",
        )
        self.console.print(table)
        self.console.print()

    # ── Callbacks (for Orchestrator hooks) ───────────────────────────────

    def on_state_change(
        self,
        broker_id: str,
        old_state: BrokerState,
        new_state: BrokerState,
    ) -> None:
        """Called when a broker's state changes."""
        icon = STATE_ICONS.get(
            new_state.value if hasattr(new_state, 'value') else str(new_state),
            "•",
        )
        old = old_state.value if hasattr(old_state, 'value') else str(old_state)
        new = new_state.value if hasattr(new_state, 'value') else str(new_state)
        self.console.print(
            f"  {icon} [{broker_id}] {old} → {new}",
            style=STYLES["info"],
        )

    def on_broker_start(self, ctx: TaskContext) -> None:
        """Called when Orchestrator starts processing a broker."""
        self.console.rule(f"[bold blue]{ctx.broker_id}[/bold blue]")
        self.console.print(
            f"  🎯 Target: {ctx.seed_url}",
            style="dim",
        )

    def on_broker_complete(self, result: BrokerRunResult) -> None:
        """Called when Orchestrator finishes a broker."""
        outcome = result.outcome
        duration = result.duration_seconds
        is_success = outcome in ("SUCCESS", "SCRUBBED")
        style = STYLES["success"] if is_success else STYLES["warning"]
        icon = "✅" if is_success else "⚠️"
        self.console.print(
            f"  {icon} {result.broker_id}: {outcome} ({duration:.1f}s)",
            style=style,
        )

    def on_agent_reasoning(self, message: str) -> None:
        """Called when browser-use agent emits reasoning."""
        self.console.print(
            f"    [REASONING] {message}", style=STYLES["reasoning"]
        )

    def on_agent_action(self, message: str) -> None:
        """Called when browser-use agent performs an action."""
        self.console.print(
            f"    [ACTION] {message}", style=STYLES["action"]
        )

    # ── HITL ─────────────────────────────────────────────────────────────

    def on_hitl_prompt(self, broker_id: str) -> None:
        """Called when HITL CAPTCHA intervention is needed.

        Blocks on input() — the user solves the CAPTCHA in the visible
        browser, then presses Enter to resume.
        """
        self.console.print()
        self.console.rule(
            "[bold yellow]🖐️  HUMAN-IN-THE-LOOP[/bold yellow]"
        )
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
        try:
            input()
        except EOFError:
            pass  # Non-interactive mode — skip

    # ── Summary ──────────────────────────────────────────────────────────

    def print_run_summary(self, summary: dict[str, int]) -> None:
        """Print final summary table after all brokers processed."""
        self.console.rule("[bold]Run Complete[/bold]")
        table = Table(title="Summary")
        table.add_column("Status", style="bold")
        table.add_column("Count")
        table.add_row("✅ Scrubbed", str(summary.get("scrubbed", 0)))
        table.add_row("📭 No Record", str(summary.get("no_record", 0)))
        table.add_row("💀 Permanent Fail", str(summary.get("permanently_failed", 0)))
        table.add_row("❌ Failed", str(summary.get("failed", 0)))
        table.add_row("🚫 CAPTCHA Blocked", str(summary.get("captcha_blocked", 0)))
        table.add_row("⏳ Pending", str(summary.get("pending", 0)))
        table.add_row("📊 Total", str(summary.get("total", 0)))
        self.console.print(table)
