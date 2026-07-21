"""
SPEC-003 test vectors — 11 verification + 2 integration tests.

Tests extract_urls, match_broker, select_verification_url,
click_verification_link, IMAP client, and InboxSentinel polling.
"""

from __future__ import annotations

import asyncio
import email
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx
import pytest
from privacy_exorcist.database import Database
from privacy_exorcist.inbox_sentinel.imap_client import IMAPClient, _extract_body
from privacy_exorcist.inbox_sentinel.sentinel import (
    VERIFICATION_TIMEOUT_SECONDS,
    InboxSentinel,
)
from privacy_exorcist.inbox_sentinel.verification import (
    _sanitize_url,
    click_verification_link,
    extract_urls,
    match_broker,
    select_verification_url,
)
from privacy_exorcist.models import (
    BrokerState,
    Playbook,
    PlaybookEntry,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def sample_playbook():
    return Playbook(brokers=[
        PlaybookEntry(
            broker_id="thatsthem",
            seed_url="https://thatsthem.com/optout",
            success_anchor="Request Submitted",
        ),
        PlaybookEntry(
            broker_id="whitepages",
            seed_url="https://www.whitepages.com/suppression-requests",
            success_anchor="successfully submitted",
        ),
    ])


# ═══════════════════════════════════════════════════════════════════════════════
# URL Extraction — TV01, TV11
# ═══════════════════════════════════════════════════════════════════════════════

class TestURLExtraction:

    def test_tv01_extract_single_url(self):
        body = 'Click here to confirm: https://thatsthem.com/verify?token=abc123'
        urls = extract_urls(body)
        assert len(urls) == 1
        assert urls[0] == "https://thatsthem.com/verify?token=abc123"

    def test_extract_multiple_urls(self):
        body = "Link 1: https://a.com Link 2: https://b.com/verify"
        urls = extract_urls(body)
        assert len(urls) == 2

    def test_tv11_sanitize_url(self):
        """Strip trailing HTML cruft and decode entities."""
        raw = 'https://broker.com/verify?token=abc123&amp;x=y">'
        cleaned = _sanitize_url(raw)
        assert cleaned == "https://broker.com/verify?token=abc123&x=y"
        assert ">" not in cleaned
        assert "&amp;" not in cleaned

    def test_sanitize_trailing_quote_bracket(self):
        raw = "https://test.com/verify\">"
        cleaned = _sanitize_url(raw)
        assert cleaned == "https://test.com/verify"


# ═══════════════════════════════════════════════════════════════════════════════
# Broker Matching — TV02–TV03
# ═══════════════════════════════════════════════════════════════════════════════

class TestBrokerMatching:

    def test_tv02_match_by_sender_domain(self, sample_playbook):
        sender = "noreply@thatsthem.com"
        body = "Confirm your opt-out request"
        result = match_broker(sender, body, sample_playbook)
        assert result == "thatsthem"

    def test_tv03_no_match(self, sample_playbook):
        sender = "unknown@random.com"
        body = "Some random email"
        result = match_broker(sender, body, sample_playbook)
        assert result is None

    def test_match_by_body_keyword(self, sample_playbook):
        sender = "noreply@random-company.com"
        body = "Your whitepages removal is being processed"
        result = match_broker(sender, body, sample_playbook)
        assert result == "whitepages"


# ═══════════════════════════════════════════════════════════════════════════════
# URL Selection — TV04
# ═══════════════════════════════════════════════════════════════════════════════

class TestURLSelection:

    def test_tv04_prefers_broker_domain(self, sample_playbook):
        urls = [
            "https://tracking.pixel.com/1x1.gif",
            "https://thatsthem.com/verify?token=abc",
        ]
        selected = select_verification_url(urls, "thatsthem", sample_playbook)
        assert selected == "https://thatsthem.com/verify?token=abc"

    def test_fallback_longest_url(self, sample_playbook):
        urls = [
            "https://x.com/a",
            "https://unrelated.com/very-long-verification-token-12345",
        ]
        selected = select_verification_url(urls, "thatsthem", sample_playbook)
        assert "very-long" in selected

    def test_empty_urls_returns_none(self, sample_playbook):
        assert select_verification_url([], "thatsthem", sample_playbook) is None


# ═══════════════════════════════════════════════════════════════════════════════
# Verification Link Clicking — TV05–TV08
# ═══════════════════════════════════════════════════════════════════════════════

class TestClickVerification:

    @pytest.mark.asyncio
    async def test_tv05_scrubbed(self):
        """HTTP 200 with 'confirmed' → SCRUBBED."""
        with mock.patch("httpx.AsyncClient.get", new_callable=mock.AsyncMock) as mock_get:
            mock_resp = mock.AsyncMock()
            mock_resp.status_code = 200
            mock_resp.text = "Your email has been confirmed. Thank you. " + ("x" * 200)
            mock_get.return_value = mock_resp

            result = await click_verification_link("https://test.com/verify")
            assert result == "SCRUBBED"

    @pytest.mark.asyncio
    async def test_tv06_escalate_blank_spa(self):
        """Content < 200 chars → ESCALATE_TO_BROWSER."""
        with mock.patch.object(httpx.AsyncClient, "get") as mock_get:
            mock_resp = mock.AsyncMock()
            mock_resp.status_code = 200
            mock_resp.text = '<div id="app"></div>'  # 45 chars
            mock_get.return_value = mock_resp

            result = await click_verification_link("https://test.com/verify")
            assert result == "ESCALATE_TO_BROWSER"

    @pytest.mark.asyncio
    async def test_tv07_escalate_noscript(self):
        """<noscript> tag → ESCALATE_TO_BROWSER."""
        with mock.patch.object(httpx.AsyncClient, "get") as mock_get:
            mock_resp = mock.AsyncMock()
            mock_resp.status_code = 200
            mock_resp.text = "<noscript>Please enable JavaScript</noscript>" + ("x" * 200)
            mock_get.return_value = mock_resp

            result = await click_verification_link("https://test.com/verify")
            assert result == "ESCALATE_TO_BROWSER"

    @pytest.mark.asyncio
    async def test_tv08_failed_http_500(self):
        """HTTP 500 → FAILED."""
        with mock.patch.object(httpx.AsyncClient, "get") as mock_get:
            mock_resp = mock.AsyncMock()
            mock_resp.status_code = 500
            mock_resp.text = "Internal Server Error"
            mock_get.return_value = mock_resp

            result = await click_verification_link("https://test.com/verify")
            assert result == "FAILED"


# ═══════════════════════════════════════════════════════════════════════════════
# Email Body Extraction — TV12
# ═══════════════════════════════════════════════════════════════════════════════

class TestBodyExtraction:

    def test_tv12_html_only_email(self):
        """Multipart email with text/html only → returns HTML source."""
        msg = email.message.EmailMessage()
        msg["From"] = "test@example.com"
        msg["Subject"] = "Verify"
        msg.set_type("text/html")
        msg.set_payload("<html><body><a href='https://verify.com/click'>Click</a></body></html>")
        body = _extract_body(msg)
        assert "https://verify.com/click" in body

    def test_plain_text_preferred(self):
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        msg = MIMEMultipart()
        msg["From"] = "test@example.com"
        msg.attach(MIMEText("Plain text body", "plain"))
        msg.attach(MIMEText("<html>HTML body</html>", "html"))
        body = _extract_body(msg)
        assert "Plain text body" in body

    def test_empty_body(self):
        msg = email.message.EmailMessage()
        msg["From"] = "test@example.com"
        body = _extract_body(msg)
        assert body == ""


# ═══════════════════════════════════════════════════════════════════════════════
# Inbox Sentinel Integration — TV09, TV10
# ═══════════════════════════════════════════════════════════════════════════════

class TestInboxSentinelIntegration:

    @pytest.fixture
    def db(self):
        import os
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        d = Database(path)
        d.migrate()
        yield d
        try:
            os.unlink(path)
        except OSError:
            pass

    def test_tv09_verification_timeout(self, db, sample_playbook):
        """Broker AWAITING_VERIFICATION for > 1h → FAILED."""
        broker_id = "thatsthem"
        # Set broker to AWAITING_VERIFICATION with an old timestamp
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=VERIFICATION_TIMEOUT_SECONDS + 100)).isoformat()
        db.upsert_broker(broker_id, BrokerState.AWAITING_VERIFICATION.value)
        # Manually set updated_at to old time
        with db._connection() as conn:
            conn.execute(
                "UPDATE broker_ledger SET updated_at = ? WHERE broker_id = ?",
                (old_time, broker_id),
            )
            conn.commit()

        # Run the timeout check through a sentinel instance
        completed: list[dict] = []
        sentinel = InboxSentinel(
            database=db,
            imap_server="imap.test.com",
            imap_port=993,
            imap_username="test",
            imap_password="test",
            playbook=sample_playbook,
            on_broker_complete=lambda bid, r: completed.append(r),
        )
        # Don't connect to real IMAP — just trigger timeout check
        asyncio.get_event_loop().run_until_complete(
            sentinel._check_timeouts()
        )

        rec = db.get_broker(broker_id)
        assert rec is not None
        assert rec.current_status == BrokerState.FAILED
        assert "timeout" in (rec.error_log or "").lower()
        assert len(completed) == 1
        assert completed[0]["outcome"] == "FAILED"
