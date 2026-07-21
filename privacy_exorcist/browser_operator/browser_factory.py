"""
Stealth BrowserProfile factory for PrivacyExorcist.

SPEC-002 §3.2 + §5 Phase 1: Creates a browser-use BrowserProfile
with anti-detection flags validated in spike runs #4-9.
"""

from __future__ import annotations

from browser_use import BrowserProfile

# ── Constants ──────────────────────────────────────────────────────────────

STEALTH_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]


# ── Factory ────────────────────────────────────────────────────────────────

def create_browser_profile(headless: bool = True) -> BrowserProfile:
    """Create a stealth-configured BrowserProfile for broker automation.

    All settings are spike-validated (run #4: TLS bypass confirmed).
    chromium_sandbox=False is MANDATORY — omitting it causes the 30-second
    BrowserStartEvent timeout on restricted Linux systems.

    Args:
        headless: True for headless mode (production), False for headed
                  (visual audit / HITL debugging).

    Returns:
        Configured BrowserProfile ready for browser-use Agent.
    """
    return BrowserProfile(
        headless=headless,
        disable_security=True,
        chromium_sandbox=False,
        user_agent=STEALTH_USER_AGENT,
        args=list(STEALTH_ARGS),
    )
