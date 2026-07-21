"""
Verification link processor for the Inbox Sentinel.

SPEC-003 §3.3–§3.4 + §5 Phase 2: Extracts verification URLs from email bodies,
matches senders to playbook brokers, clicks links via httpx, and applies
JS-detection heuristics to decide when browser escalation is needed.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx
from privacy_exorcist.models import Playbook


# ── URL Extraction ─────────────────────────────────────────────────────────

# Matches http/https URLs, stopping at whitespace, angle brackets, quotes,
# or certain punctuation that commonly trails URLs in HTML.
_URL_RE = re.compile(r'https?://[^\s<>"\')\]]+')


def extract_urls(body: str) -> list[str]:
    """Extract all HTTP/HTTPS URLs from an email body.

    Applies sanitization to strip trailing HTML cruft and decode entities.
    """
    raw = _URL_RE.findall(body)
    return [_sanitize_url(u) for u in raw]


def _sanitize_url(raw_url: str) -> str:
    """Strip trailing HTML/XML artifacts and decode common HTML entities."""
    cleaned = raw_url.rstrip('\">)]\\\'')
    cleaned = cleaned.replace("&amp;", "&")
    cleaned = cleaned.replace("&lt;", "<")
    cleaned = cleaned.replace("&gt;", ">")
    return cleaned


# ── Broker Matching ────────────────────────────────────────────────────────

def match_broker(sender: str, body: str, playbook: Playbook) -> str | None:
    """Match an email sender to a playbook broker by domain or keyword.

    Returns broker_id if matched, None otherwise.
    """
    sender_lower = sender.lower()
    for entry in playbook:
        domain = _extract_domain(entry.seed_url)
        if domain and domain in sender_lower:
            return entry.broker_id
        # Fallback: check for broker name in body
        if entry.broker_id.lower() in body.lower():
            return entry.broker_id
    return None


def select_verification_url(urls: list[str], broker_id: str, playbook: Playbook) -> str | None:
    """Select the URL most likely to be the verification link.

    Prefers URLs matching the broker's domain. Falls back to longest URL.
    """
    if not urls:
        return None

    entry = playbook.get(broker_id)
    if entry:
        domain = _extract_domain(entry.seed_url)
        if domain:
            for url in urls:
                if domain in url:
                    return url

    # Fallback: longest URL (verification links tend to have long tokens)
    return max(urls, key=len)


def _extract_domain(url: str) -> str:
    """Extract the netloc (domain) from a URL, lowercased."""
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


# ── Link Clicking with JS-Detection Heuristic ──────────────────────────────

# Confirmation keywords that indicate successful server-side verification
_CONFIRMATION_KEYWORDS = [
    "confirmed", "verified", "thank you", "success",
    "your request", "has been processed", "opt-out complete",
    "removal confirmed", "suppression successful",
]

# Minimum content length for a non-blank page
_MIN_CONTENT_LENGTH = 200


async def click_verification_link(url: str) -> str:
    """Attempt headless verification link click via httpx GET.

    Returns:
        "SCRUBBED" — confirmation text found in response.
        "ESCALATE_TO_BROWSER" — page requires JavaScript.
        "FAILED" — HTTP error or no confirmation.

    Heuristics (in order):
      1. Response < 200 chars → blank SPA shell → ESCALATE
      2. <noscript> tag present → requires JS → ESCALATE
      3. Confirmation keywords found → SCRUBBED
      4. Otherwise → ESCALATE (ambiguous)
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(url)

        if resp.status_code != 200:
            return "FAILED"

        text = resp.text.lower()
        content_length = len(text.strip())

        # Heuristic 1: blank SPA shell
        if content_length < _MIN_CONTENT_LENGTH:
            return "ESCALATE_TO_BROWSER"

        # Heuristic 2: noscript tag
        if "<noscript>" in text:
            return "ESCALATE_TO_BROWSER"

        # Heuristic 3: confirmation keywords
        if any(kw in text for kw in _CONFIRMATION_KEYWORDS):
            return "SCRUBBED"

        # Heuristic 4: ambiguous
        return "ESCALATE_TO_BROWSER"

    except Exception:
        return "FAILED"
