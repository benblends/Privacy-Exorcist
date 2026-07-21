"""
PrivacyExorcist finite state machine and transition logic.

SPEC-001 §3.2 FSM diagram + §5 Phase 4 implementation.
Maps BrokerResult codes → next BrokerState with CAPTCHA branching,
retry guards, and loop detection.
"""

from __future__ import annotations

from typing import Optional

from privacy_exorcist.models import BrokerResult, BrokerState


# ── Exceptions ──────────────────────────────────────────────────────────────

class InvalidStateTransition(ValueError):
    """Raised when an illegal state transition is attempted."""
    pass


# ── Valid Transitions ───────────────────────────────────────────────────────

# Every allowed (from_state → to_state) edge.
# Terminal states (CAPTCHA_BLOCKED, NO_RECORD, SCRUBBED, PERMANENTLY_FAILED)
# have no outgoing edges — they are sinks.
VALID_TRANSITIONS: dict[BrokerState, set[BrokerState]] = {
    BrokerState.QUEUED: {
        BrokerState.IN_PROGRESS,
    },
    BrokerState.IN_PROGRESS: {
        BrokerState.IN_PROGRESS,  # Self-loop: CAPTCHA auto-solve re-enters submission
        BrokerState.SUBMITTED,
        BrokerState.NO_RECORD,
        BrokerState.AWAITING_HUMAN_INTERVENTION,
        BrokerState.CAPTCHA_BLOCKED,
        BrokerState.FAILED,
        BrokerState.PERMANENTLY_FAILED,
    },
    BrokerState.SUBMITTED: {
        BrokerState.AWAITING_VERIFICATION,
        BrokerState.SCRUBBED,
    },
    BrokerState.AWAITING_VERIFICATION: {
        BrokerState.SCRUBBED,
        BrokerState.FAILED,
    },
    BrokerState.AWAITING_HUMAN_INTERVENTION: {
        BrokerState.IN_PROGRESS,  # Human solved CAPTCHA, resume
        BrokerState.FAILED,       # Human gave up or timed out
    },
    BrokerState.FAILED: {
        BrokerState.IN_PROGRESS,          # Retry
        BrokerState.PERMANENTLY_FAILED,   # Retries exhausted
    },
    # Terminal sinks — no outgoing edges:
    BrokerState.CAPTCHA_BLOCKED: set(),
    BrokerState.NO_RECORD: set(),
    BrokerState.SCRUBBED: set(),
    BrokerState.PERMANENTLY_FAILED: set(),
}

# Non-retryable result codes → terminal states (no retry increment)
NON_RETRYABLE_RESULTS: set[BrokerResult] = {
    BrokerResult.NO_MATCH_FOUND,
    BrokerResult.MULTIPLE_MATCH,
}

# Retryable result codes (trigger retry increment + backoff)
RETRYABLE_RESULTS: set[BrokerResult] = {
    BrokerResult.BROKER_UNREACHABLE,
    BrokerResult.FORM_SUBMIT_FAILED,
    BrokerResult.BLOCKED_403,
    BrokerResult.MULTIPLE_MATCH,
}

# Max retries before permanent failure
MAX_RETRIES = 3

# Max CAPTCHA auto-solve attempts before loop guard triggers
MAX_CAPTCHA_ATTEMPTS = 2


# ── Transition Validation ───────────────────────────────────────────────────

def transition(
    current_state: BrokerState,
    new_state: BrokerState,
) -> BrokerState:
    """Validate and apply a state transition.

    Args:
        current_state: Where the broker is now.
        new_state: Where the orchestrator wants to move it.

    Returns:
        The new state if valid.

    Raises:
        InvalidStateTransition: The transition is not in VALID_TRANSITIONS.
    """
    allowed = VALID_TRANSITIONS.get(current_state, set())
    if new_state not in allowed:
        raise InvalidStateTransition(
            f"Illegal transition: {current_state.value} → {new_state.value}. "
            f"Allowed next states from {current_state.value}: "
            f"{[s.value for s in sorted(allowed, key=lambda s: s.value)]}"
        )
    return new_state


# ── Result Code → Next State ────────────────────────────────────────────────

def get_next_state(
    current_state: BrokerState,
    result: BrokerResult,
    *,
    retry_count: int = 0,
    capsolver_key: Optional[str] = None,
    headless: bool = True,
    captcha_attempts: int = 0,
) -> BrokerState:
    """Compute the next FSM state from a Browser Operator result code.

    This is the core routing table — all SPEC-001 test vectors test this
    function or code paths it represents.

    Args:
        current_state: Current broker state (almost always IN_PROGRESS).
        result: Return code from Browser Operator.
        retry_count: Current retry count from the ledger.
        capsolver_key: CapSolver API key (None if not configured).
        headless: True if headless mode, False for headed (visual audit).
        captcha_attempts: Number of CAPTCHA solve attempts this run.

    Returns:
        The next BrokerState for the ledger.
    """
    # ── CAPTCHA routing (must be checked first — highest branching logic) ──

    if result == BrokerResult.CAPTCHA_DETECTED:
        return _route_captcha_detected(
            captcha_attempts=captcha_attempts,
            capsolver_key=capsolver_key,
            headless=headless,
        )

    # ── CAPTCHA_BLOCKED returned by Browser Operator ──
    if result == BrokerResult.CAPTCHA_BLOCKED:
        if retry_count >= MAX_RETRIES:
            return BrokerState.PERMANENTLY_FAILED
        return BrokerState.CAPTCHA_BLOCKED

    # ── Non-retryable terminal outcomes ──
    if result == BrokerResult.NO_MATCH_FOUND:
        return BrokerState.NO_RECORD

    if result == BrokerResult.MULTIPLE_MATCH:
        if retry_count >= MAX_RETRIES:
            return BrokerState.PERMANENTLY_FAILED
        return BrokerState.FAILED

    # ── Success outcomes ──
    if result == BrokerResult.SUCCESS:
        return BrokerState.SUBMITTED

    if result == BrokerResult.VERIFICATION_REQUIRED:
        return BrokerState.SUBMITTED  # Orchestrator then moves: SUBMITTED → AWAITING_VERIFICATION

    # ── Retryable errors ──
    if result in RETRYABLE_RESULTS:
        if retry_count >= MAX_RETRIES:
            return BrokerState.PERMANENTLY_FAILED
        return BrokerState.FAILED

    # ── Fallback (unexpected / future codes) ──
    if retry_count >= MAX_RETRIES:
        return BrokerState.PERMANENTLY_FAILED
    return BrokerState.FAILED


# ── CAPTCHA Branching ───────────────────────────────────────────────────────

def _route_captcha_detected(
    *,
    captcha_attempts: int,
    capsolver_key: Optional[str],
    headless: bool,
) -> BrokerState:
    """Route CAPTCHA_DETECTED to the correct next state.

    Logic from SPEC-001 §3.3 Hybrid CAPTCHA Branching.

    The captcha_attempts counter is a single-run loop guard:
      - attempts 0-1: auto-solve via CapSolver (if key available)
      - attempts >= 2: loop guard triggers → fallback to HITL or give up
    """
    has_key = bool(capsolver_key and capsolver_key.strip())

    # Loop guard: CapSolver token was rejected twice (or user clicked 2+ times)
    if captcha_attempts >= MAX_CAPTCHA_ATTEMPTS:
        if not headless:
            # Headed mode → fall back to human
            return BrokerState.AWAITING_HUMAN_INTERVENTION
        else:
            # Headless + no human possible → dead end
            return BrokerState.CAPTCHA_BLOCKED

    # Under the loop guard threshold
    if has_key:
        # Auto-solve path: re-enter the submission loop
        return BrokerState.IN_PROGRESS
    elif not headless:
        # No API key, but headed → prompt human
        return BrokerState.AWAITING_HUMAN_INTERVENTION
    else:
        # Headless + no API key = dead end
        return BrokerState.CAPTCHA_BLOCKED


# ── Backoff Calculation ─────────────────────────────────────────────────────

def compute_backoff(retry_count: int) -> int:
    """Exponential backoff in seconds: 2^retry_count.

    SPEC-001 TV13:
      retry_count=0 → 1s
      retry_count=1 → 2s
      retry_count=2 → 4s
      retry_count=3 → blocked (PERMANENTLY_FAILED)
    """
    if retry_count <= 0:
        return 1
    return 2 ** retry_count


# ── Human / Inbox Transition Helpers ────────────────────────────────────────

def human_confirmed(current_state: BrokerState) -> BrokerState:
    """Handle the 'human confirms CAPTCHA solved' transition.

    TV10: AWAITING_HUMAN_INTERVENTION → IN_PROGRESS
    """
    return transition(current_state, BrokerState.IN_PROGRESS)


def inbox_confirmed(current_state: BrokerState) -> BrokerState:
    """Handle the 'Inbox Sentinel confirms verification link clicked' transition.

    TV11: SUBMITTED → SCRUBBED
    (In production: SUBMITTED → AWAITING_VERIFICATION → SCRUBBED)
    """
    return transition(current_state, BrokerState.SCRUBBED)
