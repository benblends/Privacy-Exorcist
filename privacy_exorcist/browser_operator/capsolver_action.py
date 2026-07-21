"""
CapSolver integration via direct HTTP API — browser-use controller action.

SPEC-002 §3.3 + §5 Phase 3: Registers a 'Solve CAPTCHA' action on the
browser-use Controller. Uses direct HTTP (NOT the Python SDK — v1.0.7 has
parameter name mismatch). All DOM interaction goes through CDP, not Playwright.

CRITICAL FRAMEWORK QUIRKS (from 5 failed runs):
  - All special_context params listed EXPLICITLY — no **kwargs
  - CDP via browser_session.session_manager._get_session_for_target()
  - CapSolver API: POST createTask → poll getTaskResult (20×2s = 40s max)
  - Task types: AntiTurnstileTaskProxyLess (Cloudflare Turnstile)
"""

from __future__ import annotations

import time
from typing import Optional

import requests
from browser_use import Controller


# ── CapSolver API ──────────────────────────────────────────────────────────

CAPSOLVER_CREATE_URL = "https://api.capsolver.com/createTask"
CAPSOLVER_POLL_URL = "https://api.capsolver.com/getTaskResult"
POLL_INTERVAL = 2       # seconds between polls
POLL_MAX_ATTEMPTS = 20  # 40 seconds total


class CapSolverError(Exception):
    """Raised when CapSolver API fails to produce a valid token."""
    pass


def solve_turnstile_via_api(
    sitekey: str,
    page_url: str,
    api_key: str,
) -> str:
    """Solve a Cloudflare Turnstile CAPTCHA via CapSolver direct HTTP.

    Args:
        sitekey: Turnstile sitekey (e.g., 0x4AAAAAACiKzu913X3aFRkP).
        page_url: URL of the page containing the Turnstile widget.
        api_key: CapSolver API key.

    Returns:
        Turnstile solution token (~500+ chars).

    Raises:
        CapSolverError: Task creation failed, polling timed out, or no token.
    """
    # Create task
    create_resp = requests.post(
        CAPSOLVER_CREATE_URL,
        json={
            "clientKey": api_key,
            "task": {
                "type": "AntiTurnstileTaskProxyLess",
                "websiteURL": page_url,
                "websiteKey": sitekey,
            },
        },
        timeout=15,
    )
    task_data = create_resp.json()
    if task_data.get("errorId") != 0:
        raise CapSolverError(
            f"CapSolver createTask failed: {task_data.get('errorDescription', task_data)}"
        )
    task_id = task_data["taskId"]

    # Poll for solution
    for _ in range(POLL_MAX_ATTEMPTS):
        time.sleep(POLL_INTERVAL)
        poll_resp = requests.post(
            CAPSOLVER_POLL_URL,
            json={"clientKey": api_key, "taskId": task_id},
            timeout=10,
        )
        poll_data = poll_resp.json()

        if poll_data.get("status") == "ready":
            token = poll_data.get("solution", {}).get("token")
            if token:
                return token
            raise CapSolverError("No token in CapSolver solution")

        if poll_data.get("status") == "failed":
            raise CapSolverError(
                f"CapSolver task failed: {poll_data.get('errorDescription', poll_data)}"
            )

    raise CapSolverError(f"CapSolver timed out after {POLL_MAX_ATTEMPTS * POLL_INTERVAL}s")


# ── Turnstile Token Injection ─────────────────────────────────────────────

TURNSTILE_INJECT_JS = """
(() => {
    const input = document.querySelector('[name="cf-turnstile-response"]');
    if (input) { input.value = '{token}'; }
    if (window.___turnstileCallback) {
        try { window.___turnstileCallback('{token}'); } catch(e) {}
    }
    const frames = document.querySelectorAll('iframe');
    for (const frame of frames) {
        if (frame.src && frame.src.includes('challenges.cloudflare.com')) {
            try {
                const cb = frame.contentWindow.___turnstileCallback;
                if (cb) cb('{token}');
            } catch(e) {}
        }
    }
    return 'injected';
})()
"""


# ── Controller Factory ─────────────────────────────────────────────────────

def create_capsolver_controller(
    capsolver_key: Optional[str],
    playbook_entry,
    *,
    headless: bool = True,
) -> Controller:
    """Create a browser-use Controller with the 'Solve CAPTCHA' action.

    The controller maintains a per-execution captcha_attempts counter
    for defense-in-depth loop detection (independent of Orchestrator).

    In headed mode (headless=False), when CapSolver fails with a bot
    detection error, the action blocks on input() — the user solves
    the CAPTCHA in the visible browser window and presses Enter.

    Args:
        capsolver_key: CapSolver API key (None if HITL-only).
        playbook_entry: PlaybookEntry with captcha_type, captcha_sitekey.
        headless: True for headless mode, False for visible browser.
    """
    # Mutable state shared with the closure
    state = {"captcha_attempts": 0}

    # Extract known CAPTCHA details from playbook
    known_sitekey = playbook_entry.captcha_sitekey
    captcha_type = playbook_entry.captcha_type or "unknown"

    controller = Controller()

    @controller.action(
        'Solve CAPTCHA — automatically detect type, call CapSolver API, '
        'and inject the solution token. Use this action whenever you see '
        'any CAPTCHA, Turnstile widget, "verify you are human" challenge, '
        'or anti-bot gate on the page.',
    )
    async def solve_captcha(
        browser_session=None,
        page_url=None,
        cdp_client=None,
        page_extraction_llm=None,
        available_file_paths=None,
        has_sensitive_data=None,
        file_system=None,
        extraction_schema=None,
    ) -> str:
        # ── LOOP GUARD ──
        state["captcha_attempts"] += 1
        if state["captcha_attempts"] > 2:
            return (
                "CAPTCHA_LOOP_GUARD_TRIGGERED: CapSolver token rejected "
                "multiple times. Do not attempt further CAPTCHA solves. "
                "Report CAPTCHA_BLOCKED or request human intervention."
            )

        # ── Validate environment ──
        if not capsolver_key:
            return (
                "CAPTCHA_DETECTED: No CapSolver API key configured. "
                "Report CAPTCHA_DETECTED to request human intervention."
            )

        if not browser_session or not cdp_client:
            return "CAPTCHA_BLOCKED: No browser session available."

        try:
            # ── Resolve sitekey ──
            sitekey = known_sitekey
            if not sitekey:
                # Try to detect sitekey from the page via CDP
                target_id = browser_session.agent_focus_target_id
                session = browser_session.session_manager._get_session_for_target(target_id)
                if session:
                    result = await session.cdp_client.send.Runtime.evaluate(
                        params={
                            "expression": (
                                "document.querySelector('[data-sitekey]')?.dataset?.sitekey || ''"
                            ),
                            "returnByValue": True,
                        },
                        session_id=session.session_id,
                    )
                    sitekey = (result.get("result", {}).get("value") or "").strip()

            if not sitekey:
                return (
                    "CAPTCHA_BLOCKED: Could not determine CAPTCHA sitekey. "
                    f"CAPTCHA type: {captcha_type}."
                )

            # ── Solve via CapSolver ──
            page = page_url or playbook_entry.seed_url
            token = solve_turnstile_via_api(sitekey, page, capsolver_key)

            # ── Inject token via CDP ──
            target_id = browser_session.agent_focus_target_id
            session = browser_session.session_manager._get_session_for_target(target_id)
            if not session:
                return "CAPTCHA_BLOCKED: Could not access browser session."

            inject_js = TURNSTILE_INJECT_JS.replace("{token}", token)
            await session.cdp_client.send.Runtime.evaluate(
                params={"expression": inject_js, "returnByValue": True},
                session_id=session.session_id,
            )

            return (
                "Turnstile CAPTCHA solved and token injected successfully. "
                "The form should now submit. Continue."
            )

        except CapSolverError as e:
            error_msg = str(e)
            # Bot detection in headed mode → pause for human to solve
            if not headless and ("600010" in error_msg or "bot" in error_msg.lower()):
                try:
                    input(
                        "\n🖐️  HUMAN-IN-THE-LOOP: CAPTCHA requires human verification.\n"
                        "   Please solve the CAPTCHA in the browser window.\n"
                        "   Press Enter when done...\n"
                    )
                    return (
                        "Human solved the CAPTCHA. The form should now submit. Continue."
                    )
                except EOFError:
                    pass
            return f"CAPTCHA_BLOCKED: {e}"
        except Exception as e:
            return f"CAPTCHA_BLOCKED: Unexpected error — {e}"

    return controller
