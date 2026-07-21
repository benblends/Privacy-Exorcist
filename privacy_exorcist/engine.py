"""
PrivacyExorcist Orchestrator — central coordinator and state manager.

SPEC-001 §5 Phase 5: Wires profile, playbook, SQLite, and FSM into a
single lifecycle manager. Purely synchronous — the async browser and
IMAP work happens externally via callbacks.

Pattern:
    orchestrator = Orchestrator(profile, playbook, db, capsolver_key=..., headless=...)
    orchestrator.on_broker_start = my_browser_operator.start
    orchestrator.on_broker_complete = my_ui.on_complete
    orchestrator.on_state_change = my_ui.on_state_change

    for broker_id in orchestrator.get_pending_brokers():
        while True:
            ctx = orchestrator.start_broker(broker_id)
            result = await my_browser_operator.execute(ctx)
            orchestrator.finish_broker(broker_id, result)
            if not orchestrator.should_retry(broker_id):
                break
            await asyncio.sleep(orchestrator.get_backoff(broker_id))
            orchestrator.requeue_broker(broker_id)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from privacy_exorcist.database import Database
from privacy_exorcist.models import (
    BrokerRecord,
    BrokerResult,
    BrokerState,
    Playbook,
    PlaybookEntry,
    Profile,
)
from privacy_exorcist.state_machine import (
    MAX_CAPTCHA_ATTEMPTS,
    MAX_RETRIES,
    compute_backoff,
    get_next_state,
    human_confirmed,
    transition,
)


# ── Task Context (Orchestrator → Browser Operator) ──────────────────────────

@dataclass
class TaskContext:
    """Data passed to the Browser Operator for a single broker execution.

    SPEC-001 §7.1 contract."""
    broker_id: str
    seed_url: str
    profile: Profile
    playbook_entry: PlaybookEntry
    capsolver_key: Optional[str]
    headless: bool


# ── Broker Result (Browser Operator → Orchestrator) ─────────────────────────

@dataclass
class BrokerRunResult:
    """Structured result from Browser Operator after execution.

    SPEC-001 §7.1 contract."""
    broker_id: str
    outcome: str            # BrokerResult enum value
    duration_seconds: float = 0.0
    final_state: str = ""   # Agent's final result text (truncated to 3000 chars)
    captcha_solved: bool = False
    error: Optional[str] = None


# ── Orchestrator ─────────────────────────────────────────────────────────────

class Orchestrator:
    """Central coordinator managing broker lifecycle through the FSM.

    Responsibilities:
      - Load/validate profile and playbook
      - Seed the SQLite ledger on first run
      - Manage per-broker state transitions via the FSM
      - Track retry counts and CAPTCHA attempts
      - Fire callbacks for UI and sub-agents to hook into
    """

    def __init__(
        self,
        profile: Profile,
        playbook: Playbook,
        database: Database,
        *,
        capsolver_key: Optional[str] = None,
        headless: bool = True,
    ):
        self.profile = profile
        self.playbook = playbook
        self.db = database
        self.capsolver_key = capsolver_key
        self.headless = headless

        # Callback hooks — set by main.py / CLI layer
        self.on_state_change: Optional[
            Callable[[str, BrokerState, BrokerState], None]
        ] = None
        self.on_broker_start: Optional[Callable[[TaskContext], None]] = None
        self.on_broker_complete: Optional[Callable[[BrokerRunResult], None]] = None
        self.on_hitl_prompt: Optional[Callable[[str], None]] = None

        # Per-run CAPTCHA attempt counter (reset per broker execution)
        self._captcha_attempts: dict[str, int] = {}

        # Graceful shutdown
        self._shutdown_flag = False

        # Seed ledger: every playbook entry gets a QUEUED row if not present
        self._seed_ledger()

    # ── Public API ───────────────────────────────────────────────────────

    def get_pending_brokers(self) -> list[str]:
        """Return broker IDs that are NOT in a terminal state.

        Terminal states: SCRUBBED, NO_RECORD, PERMANENTLY_FAILED.
        """
        terminal = {
            BrokerState.SCRUBBED,
            BrokerState.NO_RECORD,
            BrokerState.PERMANENTLY_FAILED,
        }
        pending: list[str] = []
        for entry in self.playbook:
            rec = self.db.get_broker(entry.broker_id)
            if rec is None or rec.current_status not in terminal:
                pending.append(entry.broker_id)
        return pending

    def is_terminal(self, broker_id: str) -> bool:
        """True if broker is in a terminal state (no further work needed)."""
        rec = self.db.get_broker(broker_id)
        if rec is None:
            return False
        return rec.current_status in {
            BrokerState.SCRUBBED,
            BrokerState.NO_RECORD,
            BrokerState.PERMANENTLY_FAILED,
        }

    def start_broker(self, broker_id: str) -> TaskContext:
        """Transition broker to IN_PROGRESS and build the task context.

        Resets the per-run captcha_attempts counter for this execution.
        Fires on_broker_start callback.
        """
        entry = self.playbook.get(broker_id)
        if entry is None:
            raise ValueError(f"Broker '{broker_id}' not found in playbook")

        # Reset per-run counter
        self._captcha_attempts[broker_id] = 0

        # Transition to IN_PROGRESS
        self._apply_transition(broker_id, BrokerState.IN_PROGRESS)

        ctx = TaskContext(
            broker_id=broker_id,
            seed_url=entry.seed_url,
            profile=self.profile,
            playbook_entry=entry,
            capsolver_key=self.capsolver_key,
            headless=self.headless,
        )

        if self.on_broker_start:
            self.on_broker_start(ctx)

        return ctx

    def finish_broker(self, broker_id: str, result: BrokerRunResult) -> None:
        """Process the sub-agent's result and apply the FSM transition.

        This is the main decision point — it computes the next state,
        persists it, handles retries/CAPTCHA, and fires callbacks.
        """
        rec = self.db.get_broker(broker_id)
        if rec is None:
            raise ValueError(f"Broker '{broker_id}' not in ledger")

        current_state = rec.current_status
        outcome = BrokerResult(result.outcome) if result.outcome else BrokerResult.FAILED

        # Capture CAPTCHA attempt count BEFORE incrementing — the FSM
        # uses the pre-increment value (TV16: 0,1 → auto-solve; 2 → guard).
        captcha_attempts = self._captcha_attempts.get(broker_id, 0)

        # Compute next state via FSM
        next_state = get_next_state(
            current_state,
            outcome,
            retry_count=rec.retry_count,
            capsolver_key=self.capsolver_key,
            headless=self.headless,
            captcha_attempts=captcha_attempts,
        )

        # Post-computation side effects
        if outcome == BrokerResult.CAPTCHA_DETECTED:
            self._captcha_attempts[broker_id] = captcha_attempts + 1

        # Side effects based on next state
        if next_state == BrokerState.IN_PROGRESS:
            # Auto-solve path — CapSolver token was requested, increment counter
            self.db.increment_captcha_solve(broker_id)

        if next_state == BrokerState.FAILED:
            # Retryable failure — increment retry counter, escalate if exhausted
            new_retry = self.db.increment_retry(broker_id)
            if new_retry >= MAX_RETRIES:
                next_state = BrokerState.PERMANENTLY_FAILED

        if next_state == BrokerState.AWAITING_HUMAN_INTERVENTION:
            if self.on_hitl_prompt:
                self.on_hitl_prompt(broker_id)

        # Apply the transition and persist
        self._apply_transition(broker_id, next_state)

        # Log run history
        self.db.log_run(broker_id, outcome.value, result.duration_seconds)

        # Fire completion callback
        if self.on_broker_complete:
            self.on_broker_complete(result)

    def should_retry(self, broker_id: str) -> bool:
        """True if broker should be retried (FAILED + retry_count < MAX)."""
        rec = self.db.get_broker(broker_id)
        if rec is None:
            return False
        return (
            rec.current_status == BrokerState.FAILED
            and rec.retry_count < MAX_RETRIES
        )

    def get_backoff(self, broker_id: str) -> int:
        """Seconds to wait before retry based on retry_count."""
        rec = self.db.get_broker(broker_id)
        if rec is None:
            return 1
        return compute_backoff(rec.retry_count)

    def requeue_broker(self, broker_id: str) -> None:
        """Mark broker for retry: FAILED → IN_PROGRESS.

        Resets the per-run captcha_attempts counter since this is a
        fresh execution attempt (not a CAPTCHA retry within the same run).
        """
        self._captcha_attempts[broker_id] = 0
        self._apply_transition(broker_id, BrokerState.IN_PROGRESS)

    def human_confirm(self, broker_id: str) -> None:
        """Handle HITL confirmation: AWAITING_HUMAN_INTERVENTION → IN_PROGRESS."""
        rec = self.db.get_broker(broker_id)
        if rec is None:
            return
        if rec.current_status == BrokerState.AWAITING_HUMAN_INTERVENTION:
            self._captcha_attempts[broker_id] = 0  # Reset — fresh attempt
            self._apply_transition(broker_id, BrokerState.IN_PROGRESS)

    def inbox_confirm(self, broker_id: str) -> None:
        """Handle inbox verification: SUBMITTED → AWAITING_VERIFICATION → SCRUBBED."""
        rec = self.db.get_broker(broker_id)
        if rec is None:
            return
        if rec.current_status == BrokerState.SUBMITTED:
            self._apply_transition(broker_id, BrokerState.AWAITING_VERIFICATION)
        if rec.current_status == BrokerState.AWAITING_VERIFICATION:
            self._apply_transition(broker_id, BrokerState.SCRUBBED)

    def shutdown(self) -> None:
        """Graceful shutdown — set flag, stop processing new brokers."""
        self._shutdown_flag = True

    def get_summary(self) -> dict[str, int]:
        """Return counts: {scrubbed, failed, skipped, total, ...}."""
        counts: dict[str, int] = {
            "total": 0,
            "scrubbed": 0,
            "failed": 0,
            "permanently_failed": 0,
            "no_record": 0,
            "captcha_blocked": 0,
            "pending": 0,
        }
        for entry in self.playbook:
            counts["total"] += 1
            rec = self.db.get_broker(entry.broker_id)
            if rec is None:
                counts["pending"] += 1
                continue
            status = rec.current_status
            if status == BrokerState.SCRUBBED:
                counts["scrubbed"] += 1
            elif status == BrokerState.NO_RECORD:
                counts["no_record"] += 1
            elif status == BrokerState.PERMANENTLY_FAILED:
                counts["permanently_failed"] += 1
            elif status in (BrokerState.FAILED, BrokerState.CAPTCHA_BLOCKED):
                counts["failed"] += 1
            else:
                counts["pending"] += 1
        return counts

    # ── Internal ──────────────────────────────────────────────────────────

    def _seed_ledger(self) -> None:
        """Ensure every playbook entry has a row in the ledger.

        First run: inserts with status QUEUED. Subsequent runs: no-op
        for brokers already present.
        """
        for entry in self.playbook:
            existing = self.db.get_broker(entry.broker_id)
            if existing is None:
                self.db.upsert_broker(entry.broker_id, BrokerState.QUEUED.value)

    def _apply_transition(
        self, broker_id: str, new_state: BrokerState
    ) -> None:
        """Validate and apply a state transition, persisting to ledger."""
        rec = self.db.get_broker(broker_id)
        old_state = rec.current_status if rec else BrokerState.QUEUED

        # Validate (raises InvalidStateTransition if illegal)
        transition(old_state, new_state)

        # Persist
        self.db.upsert_broker(broker_id, new_state.value)

        # Notify listeners
        if self.on_state_change:
            self.on_state_change(broker_id, old_state, new_state)
