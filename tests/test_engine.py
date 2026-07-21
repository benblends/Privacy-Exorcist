"""
SPEC-001 §5 Phase 5 integration tests — Orchestrator lifecycle.

Tests the full Orchestrator with mock Browser Operator callbacks,
verifying FSM transitions, retry logic, CAPTCHA branching, and
ledger persistence.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from privacy_exorcist.database import Database
from privacy_exorcist.engine import BrokerRunResult, Orchestrator, TaskContext
from privacy_exorcist.loaders import load_playbook, load_profile
from privacy_exorcist.models import (
    BrokerResult,
    BrokerState,
    Playbook,
    PlaybookEntry,
    Profile,
)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_profile() -> Profile:
    return Profile(
        first_name="Jane",
        last_name="Doe",
        current_street="123 Main St",
        current_city="Austin",
        current_state="TX",
        current_zip="78701",
        current_phone="512-555-0147",
        sentinel_email="jane@example.com",
    )


@pytest.fixture
def sample_playbook() -> Playbook:
    return Playbook(brokers=[
        PlaybookEntry(
            broker_id="thatsthem",
            seed_url="https://thatsthem.com/optout",
            success_anchor="Request Submitted",
            flow_type="DIRECT_FORM",
            captcha_type="cloudflare_turnstile",
            captcha_sitekey="0x4AAAAAACiKzu913X3aFRkP",
            protection_layer=2,
        ),
        PlaybookEntry(
            broker_id="simplebroker",
            seed_url="https://example.com/optout",
            success_anchor="Done",
            flow_type="DIRECT_FORM",
        ),
    ])


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    d = Database(path)
    d.migrate()
    yield d
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def orchestrator(sample_profile, sample_playbook, db):
    return Orchestrator(
        profile=sample_profile,
        playbook=sample_playbook,
        database=db,
        capsolver_key="sk-test-key",
        headless=True,
    )


# ── Helper: mock browser operator result ────────────────────────────────────

def _mock_result(broker_id: str, outcome: str, **kwargs) -> BrokerRunResult:
    defaults = {
        "broker_id": broker_id,
        "outcome": outcome,
        "duration_seconds": 2.5,
        "captcha_solved": False,
    }
    defaults.update(kwargs)
    return BrokerRunResult(**defaults)


# ── Tests ───────────────────────────────────────────────────────────────────

class TestOrchestratorLifecycle:
    """Full lifecycle: start → execute → finish → verify."""

    def test_happy_path_success(self, orchestrator, db):
        """1 broker, SUCCESS → SUBMITTED."""
        broker_id = "simplebroker"

        # Start
        ctx = orchestrator.start_broker(broker_id)
        assert ctx.broker_id == broker_id
        assert ctx.seed_url == "https://example.com/optout"

        # Verify IN_PROGRESS
        rec = db.get_broker(broker_id)
        assert rec.current_status == BrokerState.IN_PROGRESS

        # Mock browser: returns SUCCESS
        result = _mock_result(broker_id, BrokerResult.SUCCESS.value)
        orchestrator.finish_broker(broker_id, result)

        # Verify SUBMITTED
        rec = db.get_broker(broker_id)
        assert rec.current_status == BrokerState.SUBMITTED
        assert not orchestrator.should_retry(broker_id)

    def test_captcha_auto_solve(self, orchestrator, db):
        """CAPTCHA_DETECTED with CapSolver key → IN_PROGRESS (auto-solve)."""
        broker_id = "thatsthem"
        orchestrator.start_broker(broker_id)

        # CAPTCHA detected → auto-solve path
        result = _mock_result(broker_id, BrokerResult.CAPTCHA_DETECTED.value,
                              captcha_solved=True)
        orchestrator.finish_broker(broker_id, result)

        rec = db.get_broker(broker_id)
        # Auto-solve re-enters IN_PROGRESS (CapSolver was called)
        assert rec.current_status == BrokerState.IN_PROGRESS
        # captcha_solves incremented
        assert rec.captcha_solves == 1

        # Retry: browser succeeds after CAPTCHA solved
        result2 = _mock_result(broker_id, BrokerResult.SUCCESS.value)
        orchestrator.finish_broker(broker_id, result2)
        rec = db.get_broker(broker_id)
        assert rec.current_status == BrokerState.SUBMITTED

    def test_captcha_loop_guard_headless(self, orchestrator, db):
        """CAPTCHA_DETECTED × 3 in headless → CAPTCHA_BLOCKED."""
        broker_id = "thatsthem"
        orchestrator.start_broker(broker_id)

        # Attempt 1: auto-solve
        r1 = _mock_result(broker_id, BrokerResult.CAPTCHA_DETECTED.value)
        orchestrator.finish_broker(broker_id, r1)
        assert db.get_broker(broker_id).current_status == BrokerState.IN_PROGRESS

        # Attempt 2: auto-solve again
        r2 = _mock_result(broker_id, BrokerResult.CAPTCHA_DETECTED.value)
        orchestrator.finish_broker(broker_id, r2)
        assert db.get_broker(broker_id).current_status == BrokerState.IN_PROGRESS

        # Attempt 3: loop guard triggers (captcha_attempts=2, headless)
        r3 = _mock_result(broker_id, BrokerResult.CAPTCHA_DETECTED.value)
        orchestrator.finish_broker(broker_id, r3)
        assert db.get_broker(broker_id).current_status == BrokerState.CAPTCHA_BLOCKED

    def test_captcha_hitl_path(self, sample_profile, sample_playbook, db):
        """CAPTCHA_DETECTED + no key + headed → AWAITING_HUMAN_INTERVENTION."""
        orch = Orchestrator(
            profile=sample_profile,
            playbook=sample_playbook,
            database=db,
            capsolver_key=None,  # No API key
            headless=False,      # Headed mode
        )
        broker_id = "thatsthem"
        orch.start_broker(broker_id)

        result = _mock_result(broker_id, BrokerResult.CAPTCHA_DETECTED.value)
        orch.finish_broker(broker_id, result)

        rec = db.get_broker(broker_id)
        assert rec.current_status == BrokerState.AWAITING_HUMAN_INTERVENTION

        # Human confirms
        orch.human_confirm(broker_id)
        rec = db.get_broker(broker_id)
        assert rec.current_status == BrokerState.IN_PROGRESS

    def test_retry_logic(self, orchestrator, db):
        """BROKER_UNREACHABLE retries: 1→2→3 → PERMANENTLY_FAILED."""
        broker_id = "simplebroker"
        orchestrator.start_broker(broker_id)

        # Attempt 1: FAILED
        r1 = _mock_result(broker_id, BrokerResult.BROKER_UNREACHABLE.value)
        orchestrator.finish_broker(broker_id, r1)
        assert db.get_broker(broker_id).current_status == BrokerState.FAILED
        assert orchestrator.should_retry(broker_id)
        # After first failure: retry_count=1, backoff = 2^1 = 2 seconds (TV13)
        assert orchestrator.get_backoff(broker_id) == 2

        # Requeue for retry
        orchestrator.requeue_broker(broker_id)
        assert db.get_broker(broker_id).current_status == BrokerState.IN_PROGRESS

        # Attempt 2
        r2 = _mock_result(broker_id, BrokerResult.BROKER_UNREACHABLE.value)
        orchestrator.finish_broker(broker_id, r2)
        assert db.get_broker(broker_id).current_status == BrokerState.FAILED
        assert orchestrator.should_retry(broker_id)
        # After second failure: retry_count=2, backoff = 2^2 = 4 seconds
        assert orchestrator.get_backoff(broker_id) == 4

        orchestrator.requeue_broker(broker_id)

        # Attempt 3: retries exhausted → PERMANENTLY_FAILED
        r3 = _mock_result(broker_id, BrokerResult.BROKER_UNREACHABLE.value)
        orchestrator.finish_broker(broker_id, r3)
        rec = db.get_broker(broker_id)
        assert rec.current_status == BrokerState.PERMANENTLY_FAILED
        assert rec.retry_count == 3
        assert not orchestrator.should_retry(broker_id)

    def test_no_match_found_terminal(self, orchestrator, db):
        """NO_MATCH_FOUND → NO_RECORD (terminal, no retry)."""
        broker_id = "simplebroker"
        orchestrator.start_broker(broker_id)

        result = _mock_result(broker_id, BrokerResult.NO_MATCH_FOUND.value)
        orchestrator.finish_broker(broker_id, result)

        rec = db.get_broker(broker_id)
        assert rec.current_status == BrokerState.NO_RECORD
        assert not orchestrator.should_retry(broker_id)

    def test_inbox_verification_flow(self, orchestrator, db):
        """SUBMITTED → AWAITING_VERIFICATION → SCRUBBED."""
        broker_id = "simplebroker"
        orchestrator.start_broker(broker_id)

        # First: submit succeeds but verification required
        result = _mock_result(broker_id, BrokerResult.VERIFICATION_REQUIRED.value)
        orchestrator.finish_broker(broker_id, result)
        rec = db.get_broker(broker_id)
        assert rec.current_status == BrokerState.SUBMITTED

        # Inbox Sentinel: moves to AWAITING_VERIFICATION
        orchestrator.inbox_confirm(broker_id)
        rec = db.get_broker(broker_id)
        assert rec.current_status == BrokerState.AWAITING_VERIFICATION

        # Second inbox confirmation → SCRUBBED
        orchestrator.inbox_confirm(broker_id)
        rec = db.get_broker(broker_id)
        assert rec.current_status == BrokerState.SCRUBBED


class TestOrchestratorBookkeeping:
    """Pending brokers, get_summary, terminal detection."""

    def test_get_pending_brokers(self, orchestrator, db):
        """Only non-terminal brokers are pending."""
        all_ids = [e.broker_id for e in orchestrator.playbook]
        # Initially both are pending
        assert set(orchestrator.get_pending_brokers()) == set(all_ids)

        # Complete one
        orchestrator.start_broker("simplebroker")
        orchestrator.finish_broker(
            "simplebroker",
            _mock_result("simplebroker", BrokerResult.SUCCESS.value),
        )
        # simplebroker is SUBMITTED (not terminal) → still pending
        pending = orchestrator.get_pending_brokers()
        assert "simplebroker" in pending

        # Mark as NO_RECORD (terminal)
        orchestrator.start_broker("thatsthem")
        orchestrator.finish_broker(
            "thatsthem",
            _mock_result("thatsthem", BrokerResult.NO_MATCH_FOUND.value),
        )
        # thatsthem is NO_RECORD → excluded
        assert "thatsthem" not in orchestrator.get_pending_brokers()

    def test_summary(self, orchestrator, db):
        """get_summary returns correct counts."""
        # Scrub one
        orchestrator.start_broker("simplebroker")
        orchestrator.finish_broker(
            "simplebroker",
            _mock_result("simplebroker", BrokerResult.SUCCESS.value),
        )
        orchestrator.inbox_confirm("simplebroker")
        orchestrator.inbox_confirm("simplebroker")

        # Fail one permanently (3 retries → PERMANENTLY_FAILED)
        for attempt in range(3):
            orchestrator.start_broker("thatsthem")
            orchestrator.finish_broker(
                "thatsthem",
                _mock_result("thatsthem", BrokerResult.BROKER_UNREACHABLE.value),
            )
            if orchestrator.should_retry("thatsthem"):
                orchestrator.requeue_broker("thatsthem")

        summary = orchestrator.get_summary()
        assert summary["total"] == 2
        assert summary["scrubbed"] == 1
        assert summary["permanently_failed"] == 1
        assert summary["pending"] == 0

    def test_is_terminal(self, orchestrator):
        """is_terminal returns True only for sink states."""
        assert not orchestrator.is_terminal("thatsthem")  # QUEUED


class TestCallbacks:
    """Callback hooks fire correctly."""

    def test_on_state_change_callback(self, sample_profile, sample_playbook, db):
        transitions: list[tuple] = []

        orch = Orchestrator(sample_profile, sample_playbook, db)
        orch.on_state_change = lambda bid, old, new: transitions.append((bid, old, new))

        orch.start_broker("simplebroker")
        assert len(transitions) == 1
        assert transitions[0] == ("simplebroker", BrokerState.QUEUED, BrokerState.IN_PROGRESS)

        orch.finish_broker(
            "simplebroker",
            _mock_result("simplebroker", BrokerResult.SUCCESS.value),
        )
        assert len(transitions) == 2
        assert transitions[1] == ("simplebroker", BrokerState.IN_PROGRESS, BrokerState.SUBMITTED)

    def test_on_hitl_prompt_callback(self, sample_profile, sample_playbook, db):
        hitl_calls: list[str] = []

        orch = Orchestrator(
            sample_profile, sample_playbook, db,
            capsolver_key=None, headless=False,
        )
        orch.on_hitl_prompt = lambda bid: hitl_calls.append(bid)

        orch.start_broker("thatsthem")
        orch.finish_broker(
            "thatsthem",
            _mock_result("thatsthem", BrokerResult.CAPTCHA_DETECTED.value),
        )
        assert hitl_calls == ["thatsthem"]


class TestEdgeCases:
    """Empty playbook, duplicate entries, etc."""

    def test_empty_playbook(self, sample_profile, db):
        playbook = Playbook(brokers=[])
        orch = Orchestrator(sample_profile, playbook, db)
        assert orch.get_pending_brokers() == []
        summary = orch.get_summary()
        assert summary["total"] == 0

    def test_shutdown_flag_persists(self, orchestrator):
        orchestrator.shutdown()
        assert orchestrator._shutdown_flag is True

    def test_ledger_seeded_on_construction(self, orchestrator, db):
        """On first Orchestrator init, all playbook entries get QUEUED rows."""
        rec = db.get_broker("thatsthem")
        assert rec is not None
        assert rec.current_status == BrokerState.QUEUED

        rec = db.get_broker("simplebroker")
        assert rec is not None
        assert rec.current_status == BrokerState.QUEUED

    def test_start_broker_unknown_raises(self, orchestrator):
        with pytest.raises(ValueError, match="not found in playbook"):
            orchestrator.start_broker("nonexistent")
