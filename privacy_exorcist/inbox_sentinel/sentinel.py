"""
Inbox Sentinel — lightweight email verification polling service.

SPEC-003 §3.6 + §5 Phase 3: Polls IMAP inbox every 60 seconds for broker
verification emails. Extracts confirmation links, clicks them via httpx,
and updates broker state through the Orchestrator callback.

Deliberately NOT a browser-use agent — uses imaplib + httpx only.
Zero LLM tokens. Zero browser automation. Zero CapSolver calls.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from privacy_exorcist.database import Database
from privacy_exorcist.inbox_sentinel.imap_client import IMAPClient
from privacy_exorcist.inbox_sentinel.verification import (
    click_verification_link,
    extract_urls,
    match_broker,
    select_verification_url,
)
from privacy_exorcist.models import BrokerState, Playbook

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

POLL_INTERVAL_SECONDS = 60
VERIFICATION_TIMEOUT_SECONDS = 3600  # 1 hour


# ── Inbox Sentinel ─────────────────────────────────────────────────────────

class InboxSentinel:
    """Async background service that monitors a dedicated inbox for broker
    verification emails and clicks confirmation links.

    Usage:
        sentinel = InboxSentinel(db, imap_config, playbook, on_complete)
        await sentinel.run()  # blocks until shutdown()
    """

    def __init__(
        self,
        database: Database,
        imap_server: str,
        imap_port: int,
        imap_username: str,
        imap_password: str,
        playbook: Playbook,
        on_broker_complete: Callable[[str, dict], None],
    ):
        self._db = database
        self._playbook = playbook
        self._on_broker_complete = on_broker_complete
        self._imap = IMAPClient(
            server=imap_server,
            port=imap_port,
            username=imap_username,
            password=imap_password,
        )
        self._shutdown = asyncio.Event()

    # ── Public API ───────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main polling loop. Blocks until shutdown() is called.

        Connects to IMAP, then polls every 60 seconds for:
          1. Verification timeout (brokers waiting > 1 hour → FAILED)
          2. New unread verification emails → click link → SCRUBBED
        """
        try:
            await self._imap.connect()
            logger.info("Inbox Sentinel connected and polling.")
        except Exception as e:
            logger.critical(f"IMAP connection failed: {e}")
            return

        while not self._shutdown.is_set():
            try:
                await self._check_timeouts()
                await self._process_inbox()
            except Exception as e:
                logger.error(f"Poll cycle error: {e}")

            # Sleep in 1-second chunks so shutdown is responsive
            for _ in range(POLL_INTERVAL_SECONDS):
                if self._shutdown.is_set():
                    break
                await asyncio.sleep(1)

        await self._imap.disconnect()
        logger.info("Inbox Sentinel shut down.")

    async def shutdown(self) -> None:
        """Signal the polling loop to stop and disconnect."""
        self._shutdown.set()

    # ── Internal ──────────────────────────────────────────────────────────

    async def _check_timeouts(self) -> None:
        """Check for brokers that have been AWAITING_VERIFICATION too long."""
        now = datetime.now(timezone.utc)
        for entry in self._playbook:
            rec = self._db.get_broker(entry.broker_id)
            if rec is None or rec.current_status != BrokerState.AWAITING_VERIFICATION:
                continue
            if rec.updated_at:
                try:
                    updated = datetime.fromisoformat(rec.updated_at)
                    elapsed = (now - updated).total_seconds()
                    if elapsed > VERIFICATION_TIMEOUT_SECONDS:
                        logger.warning(
                            f"Verification timeout for {entry.broker_id} "
                            f"({elapsed:.0f}s > {VERIFICATION_TIMEOUT_SECONDS}s)"
                        )
                        self._db.upsert_broker(
                            entry.broker_id, BrokerState.FAILED.value,
                            error_log="Verification timeout — no email received.",
                        )
                        self._on_broker_complete(entry.broker_id, {
                            "broker_id": entry.broker_id,
                            "outcome": "FAILED",
                            "error": "Verification timeout — no email received.",
                        })
                except (ValueError, TypeError):
                    pass

    async def _process_inbox(self) -> None:
        """Poll inbox for new verification emails."""
        emails = await self._imap.poll_unread()
        if not emails:
            return

        # Get currently awaiting brokers
        awaiting_ids = set()
        for entry in self._playbook:
            rec = self._db.get_broker(entry.broker_id)
            if rec and rec.current_status == BrokerState.AWAITING_VERIFICATION:
                awaiting_ids.add(entry.broker_id)

        if not awaiting_ids:
            return

        for email_data in emails:
            await self._handle_email(email_data, awaiting_ids)

    async def _handle_email(
        self, email_data: dict, awaiting_ids: set[str]
    ) -> None:
        """Process a single unread email."""
        sender = email_data["sender"]
        body = email_data["body"]

        broker_id = match_broker(sender, body, self._playbook)
        if not broker_id or broker_id not in awaiting_ids:
            return

        urls = extract_urls(body)
        if not urls:
            return

        url = select_verification_url(urls, broker_id, self._playbook)
        if not url:
            return

        logger.info(f"Verification email found for {broker_id}. Clicking: {url[:80]}...")

        result = await click_verification_link(url)

        # Mark read regardless of outcome
        await self._imap.mark_read(email_data["msg_id"])

        if result == "SCRUBBED":
            self._db.upsert_broker(broker_id, BrokerState.SCRUBBED.value)
            self._on_broker_complete(broker_id, {
                "broker_id": broker_id,
                "outcome": "SCRUBBED",
                "verification_method": "httpx",
            })
        elif result == "ESCALATE_TO_BROWSER":
            # V1.0: mark FAILED (no browser escalation yet)
            self._db.upsert_broker(
                broker_id, BrokerState.FAILED.value,
                error_log="Verification requires JS — escalation not in V1.0",
            )
            self._on_broker_complete(broker_id, {
                "broker_id": broker_id,
                "outcome": "FAILED",
                "error": "Verification requires JS — escalation not implemented in V1.0",
            })
        else:
            self._db.upsert_broker(
                broker_id, BrokerState.FAILED.value,
                error_log=f"Verification click returned: {result}",
            )
            self._on_broker_complete(broker_id, {
                "broker_id": broker_id,
                "outcome": "FAILED",
                "error": f"Verification click returned: {result}",
            })
