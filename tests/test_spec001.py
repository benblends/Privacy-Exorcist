"""
SPEC-001 test vectors — unit tests for models, database, loaders, and FSM.

Covers all 16 test vectors from the spec plus smoke-tests for
every dataclass, CRUD operation, and validation path.

Run: cd ~/DATA_Broker_Breaker_July_2026 && source .venv/bin/activate && python -m pytest tests/test_spec001.py -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure the project root is on sys.path for privacy_exorcist imports
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))

from privacy_exorcist.database import Database
from privacy_exorcist.loaders import (
    PlaybookValidationError,
    ProfileValidationError,
    load_playbook,
    load_profile,
)
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
    InvalidStateTransition,
    compute_backoff,
    get_next_state,
    human_confirmed,
    inbox_confirmed,
    transition,
)


# ═══════════════════════════════════════════════════════════════════════════════
# TV01–TV02: Enum member counts
# ═══════════════════════════════════════════════════════════════════════════════

def test_broker_state_enum_count():
    """Exactly 10 FSM states."""
    assert len(list(BrokerState)) == 10


def test_broker_result_enum_count():
    """Exactly 9 return codes."""
    assert len(list(BrokerResult)) == 9


# ═══════════════════════════════════════════════════════════════════════════════
# TV15: Profile validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestProfileValidation:
    """TV15 — profile.json loading and validation."""

    VALID_PROFILE = {
        "first_name": "Jane",
        "last_name": "Doe",
        "middle_name": "Alex",
        "aliases": ["Jane A. Smith"],
        "current_street": "123 Main St",
        "current_city": "Austin",
        "current_state": "TX",
        "current_zip": "78701",
        "current_phone": "512-555-0147",
        "past_zips": ["90210", "30303"],
        "birth_year": "1988",
        "sentinel_email": "jane.optout+sentinel@domain.com",
    }

    def test_valid_profile(self):
        p = Profile.from_dict(self.VALID_PROFILE)
        assert p.first_name == "Jane"
        assert p.last_name == "Doe"
        assert p.current_city == "Austin"
        assert p.sentinel_email == "jane.optout+sentinel@domain.com"
        assert p.aliases == ["Jane A. Smith"]
        assert p.past_zips == ["90210", "30303"]

    def test_missing_first_name(self):
        data = {**self.VALID_PROFILE, "first_name": ""}
        with pytest.raises(ProfileValidationError) as exc:
            Profile.from_dict(data)
        assert any("first_name" in e for e in exc.value.errors)

    def test_missing_sentinel_email(self):
        data = {**self.VALID_PROFILE}
        del data["sentinel_email"]
        with pytest.raises(ProfileValidationError) as exc:
            Profile.from_dict(data)
        assert any("sentinel_email" in e for e in exc.value.errors)

    def test_empty_zip(self):
        data = {**self.VALID_PROFILE, "current_zip": ""}
        with pytest.raises(ProfileValidationError) as exc:
            Profile.from_dict(data)
        assert any("current_zip" in e for e in exc.value.errors)

    def test_load_profile_from_file(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(self.VALID_PROFILE, f)
            path = f.name
        try:
            p = load_profile(path)
            assert p.first_name == "Jane"
        finally:
            os.unlink(path)

    def test_load_profile_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_profile("/tmp/nonexistent_profile_xyz.json")


# ═══════════════════════════════════════════════════════════════════════════════
# Playbook validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestPlaybookValidation:
    """Playbook loading and validation."""

    VALID_ENTRY = {
        "broker_id": "thatsthem",
        "seed_url": "https://thatsthem.com/optout",
        "success_anchor": "Request Submitted",
        "flow_type": "DIRECT_FORM",
        "captcha_type": "cloudflare_turnstile",
        "captcha_sitekey": "0x4AAAAAACiKzu913X3aFRkP",
        "protection_layer": 2,
        "known_blockers": ["turnstile"],
        "last_verified": "2026-07-19",
        "notes": "Test broker",
    }

    def test_valid_playbook_entry(self):
        e = PlaybookEntry.from_dict(self.VALID_ENTRY)
        assert e.broker_id == "thatsthem"
        assert e.captcha_type == "cloudflare_turnstile"
        assert e.captcha_sitekey == "0x4AAAAAACiKzu913X3aFRkP"

    def test_missing_seed_url(self):
        data = {**self.VALID_ENTRY}
        del data["seed_url"]
        with pytest.raises(PlaybookValidationError):
            PlaybookEntry.from_dict(data)

    def test_missing_broker_id(self):
        data = {**self.VALID_ENTRY}
        del data["broker_id"]
        with pytest.raises(PlaybookValidationError):
            PlaybookEntry.from_dict(data)

    def test_optional_captcha_fields(self):
        """Entry without captcha fields should load fine."""
        data = {
            "broker_id": "simplebroker",
            "seed_url": "https://example.com/optout",
            "success_anchor": "Done",
        }
        e = PlaybookEntry.from_dict(data)
        assert e.broker_id == "simplebroker"
        assert e.captcha_type is None
        assert e.captcha_sitekey is None

    def test_load_playbook_from_file(self):
        playbook_json = {"brokers": [
            self.VALID_ENTRY,
            {
                "broker_id": "whitepages",
                "seed_url": "https://whitepages.com/suppress",
                "success_anchor": "successfully submitted",
            },
        ]}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(playbook_json, f)
            path = f.name
        try:
            pb = load_playbook(path)
            assert len(pb) == 2
            assert pb.get("thatsthem") is not None
            assert pb.get("whitepages") is not None
        finally:
            os.unlink(path)

    def test_duplicate_broker_id_last_wins(self):
        """Duplicate broker_id → warning + last entry wins."""
        playbook_json = {"brokers": [
            {**self.VALID_ENTRY, "notes": "first"},
            {**self.VALID_ENTRY, "notes": "second"},
        ]}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(playbook_json, f)
            path = f.name
        try:
            pb = load_playbook(path)
            assert len(pb) == 1
            assert pb.get("thatsthem").notes == "second"
        finally:
            os.unlink(path)

    def test_empty_brokers_array(self):
        playbook_json = {"brokers": []}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(playbook_json, f)
            path = f.name
        try:
            with pytest.raises(PlaybookValidationError):
                load_playbook(path)
        finally:
            os.unlink(path)


# ═══════════════════════════════════════════════════════════════════════════════
# TV14: SQLite CRUD
# ═══════════════════════════════════════════════════════════════════════════════

class TestDatabase:
    """TV14 — database CRUD, counters, run history."""

    @pytest.fixture
    def db(self):
        """Create a Database on a temp file (not :memory: — SQLite :memory:
        connections are per-handle, so each _connection() call gets a fresh
        empty database). Temp file gives us persistent tables."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        d = Database(path)
        d.migrate()
        yield d
        # Cleanup
        try:
            os.unlink(path)
        except OSError:
            pass

    def test_create_tables(self, db):
        """Tables exist after migrate(). Verified by upserting — if tables
        don't exist, upsert raises OperationalError."""
        # If tables don't exist, this raises sqlite3.OperationalError
        db.upsert_broker("test", "QUEUED")
        rec = db.get_broker("test")
        assert rec is not None
        assert rec.current_status == BrokerState.QUEUED

    def test_upsert_and_get(self, db):
        db.upsert_broker("thatsthem", "QUEUED")
        rec = db.get_broker("thatsthem")
        assert rec is not None
        assert rec.broker_id == "thatsthem"
        assert rec.current_status == BrokerState.QUEUED
        assert rec.retry_count == 0

    def test_upsert_update(self, db):
        db.upsert_broker("thatsthem", "QUEUED")
        db.upsert_broker("thatsthem", "IN_PROGRESS", error_log="test error")
        rec = db.get_broker("thatsthem")
        assert rec.current_status == BrokerState.IN_PROGRESS
        assert rec.error_log == "test error"

    def test_get_nonexistent(self, db):
        assert db.get_broker("nonexistent") is None

    def test_get_all_brokers(self, db):
        db.upsert_broker("a", "QUEUED")
        db.upsert_broker("b", "IN_PROGRESS")
        all_brokers = db.get_all_brokers()
        assert len(all_brokers) == 2
        ids = {b.broker_id for b in all_brokers}
        assert ids == {"a", "b"}

    def test_increment_retry(self, db):
        db.upsert_broker("test", "FAILED")
        assert db.increment_retry("test") == 1
        assert db.increment_retry("test") == 2
        rec = db.get_broker("test")
        assert rec.retry_count == 2

    def test_increment_captcha_solve(self, db):
        db.upsert_broker("test", "IN_PROGRESS")
        assert db.increment_captcha_solve("test") == 1
        assert db.increment_captcha_solve("test") == 2
        rec = db.get_broker("test")
        assert rec.captcha_solves == 2

    def test_log_run(self, db):
        db.upsert_broker("thatsthem", "QUEUED")
        run_id = db.log_run("thatsthem", "SUBMITTED", 68.4)
        assert run_id is not None
        with db._connection() as conn:
            row = conn.execute(
                "SELECT * FROM run_history WHERE broker_id = ?",
                ("thatsthem",),
            ).fetchone()
        assert row is not None
        assert row["outcome"] == "SUBMITTED"
        assert row["duration_seconds"] == 68.4


# ═══════════════════════════════════════════════════════════════════════════════
# TV01–TV12, TV16: State Machine
# ═══════════════════════════════════════════════════════════════════════════════

class TestStateMachine:
    """TV01–TV12 + TV16 from SPEC-001 §6."""

    # ── TV01: SUCCESS → SUBMITTED ────────────────────────────────────────

    def test_tv01_success(self):
        assert (
            get_next_state(BrokerState.IN_PROGRESS, BrokerResult.SUCCESS, retry_count=0)
            == BrokerState.SUBMITTED
        )

    # ── TV02: VERIFICATION_REQUIRED → SUBMITTED ──────────────────────────

    def test_tv02_verification_required(self):
        assert (
            get_next_state(
                BrokerState.IN_PROGRESS,
                BrokerResult.VERIFICATION_REQUIRED,
                retry_count=0,
            )
            == BrokerState.SUBMITTED
        )

    # ── TV03: CAPTCHA_DETECTED + CapSolver key → IN_PROGRESS (auto-solve) ─

    def test_tv03_captcha_detected_auto_solve(self):
        assert (
            get_next_state(
                BrokerState.IN_PROGRESS,
                BrokerResult.CAPTCHA_DETECTED,
                retry_count=0,
                capsolver_key="sk-valid",
                headless=True,
                captcha_attempts=0,
            )
            == BrokerState.IN_PROGRESS
        )

    # ── TV04: CAPTCHA_DETECTED + no key + headed → HITL ──────────────────

    def test_tv04_captcha_detected_hitl(self):
        assert (
            get_next_state(
                BrokerState.IN_PROGRESS,
                BrokerResult.CAPTCHA_DETECTED,
                retry_count=0,
                capsolver_key=None,
                headless=False,
                captcha_attempts=0,
            )
            == BrokerState.AWAITING_HUMAN_INTERVENTION
        )

    # ── TV05: CAPTCHA_BLOCKED + retry=2 → CAPTCHA_BLOCKED ────────────────

    def test_tv05_captcha_blocked_retry2(self):
        assert (
            get_next_state(
                BrokerState.IN_PROGRESS,
                BrokerResult.CAPTCHA_BLOCKED,
                retry_count=2,
                capsolver_key="sk-valid",
            )
            == BrokerState.CAPTCHA_BLOCKED
        )

    # ── TV06: CAPTCHA_BLOCKED + retry=3 → PERMANENTLY_FAILED ─────────────

    def test_tv06_captcha_blocked_permafail(self):
        assert (
            get_next_state(
                BrokerState.IN_PROGRESS,
                BrokerResult.CAPTCHA_BLOCKED,
                retry_count=3,
                capsolver_key="sk-valid",
            )
            == BrokerState.PERMANENTLY_FAILED
        )

    # ── TV07: BROKER_UNREACHABLE + retry=0 → FAILED ──────────────────────

    def test_tv07_broker_unreachable(self):
        assert (
            get_next_state(
                BrokerState.IN_PROGRESS,
                BrokerResult.BROKER_UNREACHABLE,
                retry_count=0,
            )
            == BrokerState.FAILED
        )

    # ── TV08: BROKER_UNREACHABLE + retry=3 → PERMANENTLY_FAILED ──────────

    def test_tv08_broker_unreachable_permafail(self):
        assert (
            get_next_state(
                BrokerState.IN_PROGRESS,
                BrokerResult.BROKER_UNREACHABLE,
                retry_count=3,
            )
            == BrokerState.PERMANENTLY_FAILED
        )

    # ── TV09: NO_MATCH_FOUND → NO_RECORD (terminal) ──────────────────────

    def test_tv09_no_match_found(self):
        assert (
            get_next_state(
                BrokerState.IN_PROGRESS,
                BrokerResult.NO_MATCH_FOUND,
                retry_count=0,
            )
            == BrokerState.NO_RECORD
        )

    # ── TV10: Human confirms → IN_PROGRESS ───────────────────────────────

    def test_tv10_human_confirms(self):
        assert (
            human_confirmed(BrokerState.AWAITING_HUMAN_INTERVENTION)
            == BrokerState.IN_PROGRESS
        )

    # ── TV11: Inbox confirms → SCRUBBED ──────────────────────────────────

    def test_tv11_inbox_confirms(self):
        assert (
            inbox_confirmed(BrokerState.SUBMITTED)
            == BrokerState.SCRUBBED
        )

    # ── TV12: FORM_SUBMIT_FAILED + retry=1 → FAILED ─────────────────────

    def test_tv12_form_submit_failed(self):
        assert (
            get_next_state(
                BrokerState.IN_PROGRESS,
                BrokerResult.FORM_SUBMIT_FAILED,
                retry_count=1,
            )
            == BrokerState.FAILED
        )

    # ── TV16: CAPTCHA loop guard (captcha_attempts counter) ───────────────

    def test_tv16a_attempts0_auto_solve(self):
        """captcha_attempts=0 + key + headed → auto-solve (IN_PROGRESS)."""
        assert (
            get_next_state(
                BrokerState.IN_PROGRESS,
                BrokerResult.CAPTCHA_DETECTED,
                capsolver_key="sk-valid",
                headless=False,
                captcha_attempts=0,
            )
            == BrokerState.IN_PROGRESS
        )

    def test_tv16b_attempts1_auto_solve(self):
        """captcha_attempts=1 + key + headed → second auto-solve attempt."""
        assert (
            get_next_state(
                BrokerState.IN_PROGRESS,
                BrokerResult.CAPTCHA_DETECTED,
                capsolver_key="sk-valid",
                headless=False,
                captcha_attempts=1,
            )
            == BrokerState.IN_PROGRESS
        )

    def test_tv16c_attempts2_headed_fallback(self):
        """captcha_attempts=2 + key + headed → HITL fallback."""
        assert (
            get_next_state(
                BrokerState.IN_PROGRESS,
                BrokerResult.CAPTCHA_DETECTED,
                capsolver_key="sk-valid",
                headless=False,
                captcha_attempts=2,
            )
            == BrokerState.AWAITING_HUMAN_INTERVENTION
        )

    def test_tv16d_attempts2_headless_blocked(self):
        """captcha_attempts=2 + key + headless → CAPTCHA_BLOCKED."""
        assert (
            get_next_state(
                BrokerState.IN_PROGRESS,
                BrokerResult.CAPTCHA_DETECTED,
                capsolver_key="sk-valid",
                headless=True,
                captcha_attempts=2,
            )
            == BrokerState.CAPTCHA_BLOCKED
        )

    def test_tv16e_attempts0_no_key_headed(self):
        """captcha_attempts=0 + no key + headed → immediate HITL."""
        assert (
            get_next_state(
                BrokerState.IN_PROGRESS,
                BrokerResult.CAPTCHA_DETECTED,
                capsolver_key=None,
                headless=False,
                captcha_attempts=0,
            )
            == BrokerState.AWAITING_HUMAN_INTERVENTION
        )

    # ── Additional FSM edge cases ────────────────────────────────────────

    def test_form_submit_failed_permafail(self):
        assert (
            get_next_state(
                BrokerState.IN_PROGRESS,
                BrokerResult.FORM_SUBMIT_FAILED,
                retry_count=MAX_RETRIES,
            )
            == BrokerState.PERMANENTLY_FAILED
        )

    def test_blocked_403_retryable(self):
        assert (
            get_next_state(
                BrokerState.IN_PROGRESS,
                BrokerResult.BLOCKED_403,
                retry_count=0,
            )
            == BrokerState.FAILED
        )

    def test_blocked_403_permafail(self):
        assert (
            get_next_state(
                BrokerState.IN_PROGRESS,
                BrokerResult.BLOCKED_403,
                retry_count=MAX_RETRIES,
            )
            == BrokerState.PERMANENTLY_FAILED
        )

    def test_invalid_transition(self):
        """Illegal transition raises InvalidStateTransition."""
        with pytest.raises(InvalidStateTransition):
            transition(BrokerState.SCRUBBED, BrokerState.IN_PROGRESS)

    def test_all_valid_transitions(self):
        """Every edge in VALID_TRANSITIONS should pass."""
        for src, targets in [
            (BrokerState.QUEUED, {BrokerState.IN_PROGRESS}),
            (BrokerState.AWAITING_HUMAN_INTERVENTION, {BrokerState.IN_PROGRESS}),
            (BrokerState.SUBMITTED, {BrokerState.SCRUBBED}),
        ]:
            for tgt in targets:
                assert transition(src, tgt) == tgt


# ═══════════════════════════════════════════════════════════════════════════════
# TV13: Retry backoff
# ═══════════════════════════════════════════════════════════════════════════════

class TestBackoff:
    def test_backoff_0(self):
        assert compute_backoff(0) == 1

    def test_backoff_1(self):
        assert compute_backoff(1) == 2

    def test_backoff_2(self):
        assert compute_backoff(2) == 4

    def test_backoff_negative(self):
        """Negative count treated as 0 → 1 second."""
        assert compute_backoff(-1) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Dataclass constructors (smoke tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDataclasses:
    def test_broker_record_defaults(self):
        r = BrokerRecord(broker_id="test")
        assert r.broker_id == "test"
        assert r.current_status == BrokerState.QUEUED
        assert r.retry_count == 0

    def test_playbook_len_and_iter(self):
        pb = Playbook(brokers=[
            PlaybookEntry.from_dict({
                "broker_id": "a",
                "seed_url": "https://a.com",
                "success_anchor": "Done",
            }),
            PlaybookEntry.from_dict({
                "broker_id": "b",
                "seed_url": "https://b.com",
                "success_anchor": "OK",
            }),
        ])
        assert len(pb) == 2
        ids = [e.broker_id for e in pb]
        assert ids == ["a", "b"]
