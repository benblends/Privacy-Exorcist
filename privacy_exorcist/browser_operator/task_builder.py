"""
Task string builder — converts TaskContext into browser-use agent prompts.

SPEC-002 §3.4 + §5 Phase 2: Builds natural-language task strings for
DIRECT_FORM and SEARCH_AND_CLAIM flow types.

Prompt engineering rules (spike-validated, runs #7-9):
  - CAPTCHA instruction in CRITICAL block BEFORE numbered steps
  - Use "MUST" and "DO NOT" language for CAPTCHA handling
  - Include exact success_anchor text
  - Mention "Solve CAPTCHA" action by name
  - Click CAPTCHA widget first to trigger turnstile, then call solver
"""

from __future__ import annotations

from privacy_exorcist.engine import TaskContext


def build_task(ctx: TaskContext) -> str:
    """Build a browser-use agent task string from a TaskContext.

    Dispatches to the correct builder based on flow_type.
    """
    if ctx.playbook_entry.flow_type == "SEARCH_AND_CLAIM":
        return _build_search_and_claim_task(ctx)
    return _build_direct_form_task(ctx)


# ── DIRECT_FORM builder ────────────────────────────────────────────────────

def _build_direct_form_task(ctx: TaskContext) -> str:
    """Build task for brokers with a direct opt-out form (ThatsThem style).

    The agent navigates to the form, fills all profile fields visually,
    handles any CAPTCHA, submits, and confirms the success anchor.
    """
    p = ctx.profile
    entry = ctx.playbook_entry
    notes = f"\nSITE NOTES: {entry.notes}" if entry.notes else ""

    return (
        f'CRITICAL: If you see a "Verify you are human" Turnstile widget, '
        f"click the widget ONCE to trigger the challenge, THEN call "
        f"'Solve CAPTCHA' to inject the token. DO NOT click the checkbox "
        f"yourself — the tool handles it.\n"
        f"{notes}\n"
        f"\n"
        f"GOAL: Complete the opt-out flow on {entry.broker_id}.\n"
        f"1. Navigate to {entry.seed_url}\n"
        f"2. Fill the form with:\n"
        f"   - First Name: {p.first_name}\n"
        f"   - Last Name: {p.last_name}\n"
        f"   - Street: {p.current_street}\n"
        f"   - City: {p.current_city}\n"
        f"   - State: {p.current_state}\n"
        f"   - Zip: {p.current_zip}\n"
        f"   - Phone: {p.current_phone}\n"
        f"   - Email: {p.sentinel_email}\n"
        f'3. Check any required consent checkboxes or "I am not a robot" toggles\n'
        f"4. Click the Turnstile widget to trigger it, then call 'Solve CAPTCHA'\n"
        f"5. Click the submit / opt-out button\n"
        f'6. Look for confirmation text: "{entry.success_anchor}"\n'
        f"7. If you see the confirmation, report SUCCESS.\n"
        f"   If you see any error or block, describe exactly what happened.\n"
        f"\n"
        f"SYNTHETIC test data only — this is an automated privacy tool."
    )


# ── SEARCH_AND_CLAIM builder ───────────────────────────────────────────────

def _build_search_and_claim_task(ctx: TaskContext) -> str:
    """Build task for brokers requiring search before opt-out (Whitepages style).

    The agent searches for the profile, locates the correct record,
    then follows the site's opt-out flow.
    """
    p = ctx.profile
    entry = ctx.playbook_entry
    notes = f"\nSITE NOTES: {entry.notes}" if entry.notes else ""

    return (
        f'CRITICAL: If you see a "Verify you are human" Turnstile widget, '
        f"click the widget ONCE to trigger the challenge, THEN call "
        f"'Solve CAPTCHA' to inject the token. DO NOT click the checkbox "
        f"yourself — the tool handles it.\n"
        f"{notes}\n"
        f"\n"
        f"GOAL: Find and initiate the opt-out for {p.first_name} {p.last_name} "
        f"on {entry.broker_id}.\n"
        f"1. Navigate to {entry.seed_url}\n"
        f"2. Search for: {p.first_name} {p.last_name}, {p.current_city}, {p.current_state}\n"
        f"3. Locate the record matching:\n"
        f"   - Name: {p.first_name} {p.last_name}\n"
        f"   - City: {p.current_city}\n"
        f"   - State: {p.current_state}\n"
        f"   - Zip: {p.current_zip}\n"
        f"4. Follow the site's opt-out / removal / suppression link for that record\n"
        f"5. If a confirmation form appears, fill it with:\n"
        f"   - Email: {p.sentinel_email}\n"
        f"   - Phone: {p.current_phone}\n"
        f"6. Click the Turnstile widget to trigger it, then call 'Solve CAPTCHA'\n"
        f"7. Submit the request\n"
        f'8. Look for confirmation text: "{entry.success_anchor}"\n'
        f"9. If you see the confirmation, report SUCCESS.\n"
        f"   If you see any error or block, describe exactly what happened.\n"
        f"\n"
        f"SYNTHETIC test data only — this is an automated privacy tool."
    )
