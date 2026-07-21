"""
Agent final_result() text → BrokerResult enum mapper.

SPEC-002 §3.6 + §5 Phase 4: Classifies the browser-use agent's final
natural-language output into a structured BrokerResult code.

Classification order matters — success anchors checked FIRST, then
CAPTCHA outcomes, then errors, then unknown → FAILED.
"""

from __future__ import annotations

from privacy_exorcist.models import BrokerResult


# ── Classification ─────────────────────────────────────────────────────────

def map_result(final_text: str, success_anchor: str) -> str:
    """Map agent's final text to a BrokerResult enum value.

    Args:
        final_text: The agent's final_result() string (case-insensitive match).
        success_anchor: The playbook's success_anchor text.

    Returns:
        BrokerResult enum value as string.
    """
    text = final_text.lower()
    anchor = success_anchor.lower()

    # ── Success (check FIRST — anchor may coexist with other keywords) ──
    if anchor and anchor in text:
        if "verification" in text or "email" in text:
            return BrokerResult.VERIFICATION_REQUIRED.value
        return BrokerResult.SUCCESS.value

    # ── CAPTCHA outcomes ──
    if "captcha_blocked" in text:
        return BrokerResult.CAPTCHA_BLOCKED.value
    if ("captcha_loop_guard_triggered" in text or
            ("captcha" in text and "token rejected" in text)):
        return BrokerResult.CAPTCHA_BLOCKED.value
    if "captcha" in text and ("solve" in text or "failed" in text or "detected" in text):
        return BrokerResult.CAPTCHA_DETECTED.value

    # ── HTTP / network errors ──
    if "403" in text or "forbidden" in text:
        return BrokerResult.BLOCKED_403.value

    # ── Search / record errors ──
    if "no match" in text or "no results" in text or "not found" in text:
        # Map to NO_MATCH_FOUND — Orchestrator maps to NO_RECORD
        return BrokerResult.NO_MATCH_FOUND.value

    # ── Connectivity errors ──
    if "timeout" in text or "unreachable" in text or "503" in text:
        return BrokerResult.BROKER_UNREACHABLE.value

    # ── Form submission errors ──
    if "form" in text and ("rejected" in text or "error" in text or "validation" in text):
        return BrokerResult.FORM_SUBMIT_FAILED.value

    # ── Too many results ──
    if "multiple match" in text or "too many" in text:
        return BrokerResult.MULTIPLE_MATCH.value

    # ── Default: partial completion / unknown ──
    return BrokerResult.FORM_SUBMIT_FAILED.value
