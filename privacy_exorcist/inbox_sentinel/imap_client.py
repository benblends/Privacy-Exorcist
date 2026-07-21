"""
IMAP connection manager for the Inbox Sentinel.

SPEC-003 §3.2 + §5 Phase 1: Wraps synchronous imaplib operations
in asyncio.to_thread() to avoid blocking the event loop. Handles
multipart email body extraction (plain text preferred, HTML fallback).
"""

from __future__ import annotations

import asyncio
import email
import imaplib
from typing import Optional


class IMAPClient:
    """Async-safe IMAP client for polling a broker verification inbox.

    All IMAP operations are synchronous and wrapped in asyncio.to_thread().
    """

    def __init__(
        self,
        server: str,
        port: int,
        username: str,
        password: str,
    ):
        self._server = server
        self._port = port
        self._username = username
        self._password = password
        self._conn: Optional[imaplib.IMAP4_SSL] = None

    # ── Connection ──────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Establish IMAP connection and select INBOX.

        Raises imaplib.IMAP4.error on auth failure.
        """
        self._conn = imaplib.IMAP4_SSL(self._server, self._port)
        self._conn.login(self._username, self._password)
        self._conn.select("INBOX")

    async def disconnect(self) -> None:
        """Logout and close the IMAP connection."""
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    # ── Polling ──────────────────────────────────────────────────────────

    async def poll_unread(self) -> list[dict]:
        """Fetch all unread (UNSEEN) emails.

        Returns a list of dicts with keys: msg_id, sender, subject, body, date.
        All IMAP operations are wrapped in asyncio.to_thread().

        Returns empty list if no unread messages or on IMAP error.
        """
        if not self._conn:
            return []

        try:
            status, messages = await asyncio.to_thread(
                self._conn.search, None, "UNSEEN"
            )
        except imaplib.IMAP4.error:
            return []

        if status != "OK" or not messages[0]:
            return []

        results: list[dict] = []
        for msg_id in messages[0].split():
            try:
                status, msg_data = await asyncio.to_thread(
                    self._conn.fetch, msg_id, "(RFC822)"
                )
            except imaplib.IMAP4.error:
                continue

            if status != "OK":
                continue

            msg = email.message_from_bytes(msg_data[0][1])
            results.append({
                "msg_id": msg_id.decode() if isinstance(msg_id, bytes) else msg_id,
                "sender": msg["From"] or "",
                "subject": msg["Subject"] or "",
                "body": _extract_body(msg),
                "date": msg["Date"] or "",
            })

        return results

    # ── Mark Read ────────────────────────────────────────────────────────

    async def mark_read(self, msg_id: str) -> None:
        """Mark an email as Seen."""
        if not self._conn:
            return
        try:
            await asyncio.to_thread(
                self._conn.store, msg_id, "+FLAGS", "\\Seen"
            )
        except imaplib.IMAP4.error:
            pass


# ── Body Extraction ────────────────────────────────────────────────────────

def _extract_body(msg) -> str:
    """Extract the readable body from an email.message.Message.

    Prefers text/plain. Falls back to text/html (URL regex works on HTML).
    Returns empty string if no body found.
    """
    plain_text: Optional[str] = None
    html_text: Optional[str] = None

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            decoded = payload.decode(errors="replace")
            if content_type == "text/plain" and not plain_text:
                plain_text = decoded
            elif content_type == "text/html" and not html_text:
                html_text = decoded
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            decoded = payload.decode(errors="replace")
            if msg.get_content_type() == "text/html":
                html_text = decoded
            else:
                plain_text = decoded

    return plain_text or html_text or ""
