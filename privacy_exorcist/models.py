"""
PrivacyExorcist data models — enums, dataclasses, and data contracts.

SPEC-001 §5 Phase 1: Core types shared across all subsystems.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ── Broker Lifecycle State ──────────────────────────────────────────────────

class BrokerState(str, Enum):
    """
    Finite state machine states for each broker in the ledger.
    Matches SPEC-001 §3.2 FSM diagram — exactly 10 states.
    """
    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    SUBMITTED = "SUBMITTED"
    AWAITING_VERIFICATION = "AWAITING_VERIFICATION"
    AWAITING_HUMAN_INTERVENTION = "AWAITING_HUMAN_INTERVENTION"
    CAPTCHA_BLOCKED = "CAPTCHA_BLOCKED"
    NO_RECORD = "NO_RECORD"
    SCRUBBED = "SCRUBBED"
    FAILED = "FAILED"
    PERMANENTLY_FAILED = "PERMANENTLY_FAILED"


# ── Browser Operator Return Code ────────────────────────────────────────────

class BrokerResult(str, Enum):
    """
    Return codes from Browser Operator → Orchestrator.
    PRD §3.4 taxonomy — exactly 9 members.
    """
    SUCCESS = "SUCCESS"
    VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
    CAPTCHA_DETECTED = "CAPTCHA_DETECTED"
    CAPTCHA_BLOCKED = "CAPTCHA_BLOCKED"
    BROKER_UNREACHABLE = "BROKER_UNREACHABLE"
    NO_MATCH_FOUND = "NO_MATCH_FOUND"
    MULTIPLE_MATCH = "MULTIPLE_MATCH"
    FORM_SUBMIT_FAILED = "FORM_SUBMIT_FAILED"
    BLOCKED_403 = "BLOCKED_403"


# ── SQLite Ledger Record ────────────────────────────────────────────────────

@dataclass
class BrokerRecord:
    """A single row from the broker_ledger table."""
    broker_id: str
    current_status: BrokerState = BrokerState.QUEUED
    last_run_timestamp: Optional[str] = None
    retry_count: int = 0
    captcha_solves: int = 0
    error_log: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


# ── User Profile (profile.json) ─────────────────────────────────────────────

@dataclass
class Profile:
    """
    Local identity vault — loaded from profile.json.
    PRD §5.1 REQ-001 schema.
    """
    first_name: str
    last_name: str
    current_street: str
    current_city: str
    current_state: str
    current_zip: str
    current_phone: str
    sentinel_email: str
    middle_name: str = ""
    aliases: list[str] = field(default_factory=list)
    past_zips: list[str] = field(default_factory=list)
    birth_year: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        """Validate and construct from raw JSON dict.

        Raises ProfileValidationError with field-level messages.
        """
        from privacy_exorcist.loaders import ProfileValidationError

        errors: list[str] = []

        def _required(key: str) -> str:
            value = data.get(key, "")
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{key} is required")
                return ""
            return value.strip()

        first_name = _required("first_name")
        last_name = _required("last_name")
        current_street = _required("current_street")
        current_city = _required("current_city")
        current_state = _required("current_state")
        current_zip = _required("current_zip")
        current_phone = _required("current_phone")
        sentinel_email = _required("sentinel_email")

        if errors:
            raise ProfileValidationError(errors)

        return cls(
            first_name=first_name,
            last_name=last_name,
            current_street=current_street,
            current_city=current_city,
            current_state=current_state,
            current_zip=current_zip,
            current_phone=current_phone,
            sentinel_email=sentinel_email,
            middle_name=data.get("middle_name", "").strip(),
            aliases=data.get("aliases") or [],
            past_zips=data.get("past_zips") or [],
            birth_year=data.get("birth_year", "").strip(),
        )


# ── Broker Playbook Entry ───────────────────────────────────────────────────

@dataclass
class PlaybookEntry:
    """
    A single broker's opt-out configuration.
    PRD §5.2 REQ-003 / REQ-004 schema.
    """
    broker_id: str
    seed_url: str
    success_anchor: str
    flow_type: str = "DIRECT_FORM"
    captcha_type: Optional[str] = None
    captcha_sitekey: Optional[str] = None
    protection_layer: int = 0
    known_blockers: list[str] = field(default_factory=list)
    last_verified: str = ""
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "PlaybookEntry":
        """Construct from a playbook JSON entry dict.

        Validates required fields. Handles optional captcha fields gracefully.
        """
        from privacy_exorcist.loaders import PlaybookValidationError

        broker_id = data.get("broker_id", "").strip()
        seed_url = data.get("seed_url", "").strip()
        success_anchor = data.get("success_anchor", "").strip()

        missing: list[str] = []
        if not broker_id:
            missing.append("broker_id")
        if not seed_url:
            missing.append("seed_url")
        if not success_anchor:
            missing.append("success_anchor")
        if missing:
            raise PlaybookValidationError(
                [f"Missing required field(s): {', '.join(missing)}"]
            )

        return cls(
            broker_id=broker_id,
            seed_url=seed_url,
            success_anchor=success_anchor,
            flow_type=data.get("flow_type", "DIRECT_FORM"),
            captcha_type=data.get("captcha_type"),
            captcha_sitekey=data.get("captcha_sitekey"),
            protection_layer=data.get("protection_layer", 0),
            known_blockers=data.get("known_blockers") or [],
            last_verified=data.get("last_verified", ""),
            notes=data.get("notes", ""),
        )


# ── Aggregated Playbook ─────────────────────────────────────────────────────

@dataclass
class Playbook:
    """Container for all playbook entries."""
    brokers: list[PlaybookEntry] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.brokers)

    def __iter__(self):
        return iter(self.brokers)

    def get(self, broker_id: str) -> Optional[PlaybookEntry]:
        for entry in self.brokers:
            if entry.broker_id == broker_id:
                return entry
        return None
