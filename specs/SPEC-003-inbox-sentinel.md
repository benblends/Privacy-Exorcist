================================================================================
SPEC-003: INBOX SENTINEL & VERIFICATION PIPELINE
================================================================================
PrivacyExorcist — V1.0
Status: Draft for Review
Date: 2026-07-19
Depends On: SPEC-001 (Core Engine) — SQLite schema + FSM contract
             SPEC-002 (Browser Operator) — VERIFICATION_REQUIRED flow trigger
Build Phase: 3 (Email Pipeline — can build in parallel with Phase 2)

================================================================================
1. PURPOSE
================================================================================

This spec defines the Inbox Sentinel: a lightweight, non-agentic background
service that polls a dedicated email inbox via IMAP, detects broker verification
emails, extracts confirmation links, and clicks them to complete the opt-out
lifecycle.

It answers one question: "The broker said they sent a verification email — did
the user actually get scrubbed?"

The sentinel is deliberately NOT a browser-use agent. It uses:
  - IMAP (imaplib) for email polling
  - Python regex for URL extraction
  - httpx for headless HTTP GET verification link clicks
  - A JS-detection heuristic to decide when to escalate to the Browser Operator

This keeps the sentinel fast, cheap (zero LLM tokens), and isolated from
browser automation credentials.

================================================================================
2. CODEBASE GROUND TRUTH
================================================================================

2.1 What Exists Already

No spike code exists for the Inbox Sentinel — this spec is built from
architecture, not from throwaway scripts. The design is grounded in:

  PRD v1.1 §5.4 (IMAP Inbox Sentinel requirements REQ-008 through REQ-010):
    - 60-second async polling loop
    - Domain matching against playbook domains
    - httpx GET for verification links
    - JS-detection heuristic for browser escalation

  SPEC-001 §3.2 (FSM):
    - AWAITING_VERIFICATION state → Inbox Sentinel confirms → SCRUBBED

  SPEC-001 §3.4 (SQLite):
    - broker_ledger table with current_status, updated_at columns

Python standard library modules available:
  - imaplib (IMAP4_SSL)
  - email (message parsing)
  - re (regex for URL extraction)
  - asyncio (async polling loop)

Third-party:
  - httpx (async HTTP client, already a dependency)

2.2 What Does NOT Exist

  - InboxSentinel class
  - IMAP connection manager
  - Email parser for broker verification messages
  - URL extraction via regex
  - JS-detection heuristic
  - Browser escalation trigger
  - Verification timeout logic (broker links expire)

2.3 Dependencies

  SPEC-001 (Core Engine) — reads/writes broker_ledger for AWAITING_VERIFICATION
    brokers. Fires on_broker_complete when verification completes.
  SPEC-002 (Browser Operator) — escalation path when verification link requires
    JavaScript.
  .env — IMAP credentials (IMAP_SERVER, IMAP_PORT, IMAP_USERNAME, IMAP_PASSWORD,
    SENTINEL_EMAIL — must match profile.json sentinel_email).

================================================================================
3. DESIGN
================================================================================

3.1 Architecture

  ┌────────────────────────────────────────────────────────────┐
  │                     Inbox Sentinel                          │
  │                                                            │
  │  ┌──────────────────┐   ┌──────────────────────────────┐   │
  │  │ IMAP Connection  │   │ Verification Link Processor  │   │
  │  │ Manager          │   │                              │   │
  │  │ - connect()      │   │ - extract_urls(email_body)   │   │
  │  │ - poll_inbox()   │   │ - match_domain(url, playbook)│   │
  │  │ - mark_read()    │   │ - click_link(url)            │   │
  │  └────────┬─────────┘   │ - detect_js_required(html)   │   │
  │           │             └──────────────┬───────────────┘   │
  │           │                            │                    │
  │  ┌────────┴────────────────────────────┴───────────────┐   │
  │  │              Polling Loop (async, 60s)              │   │
  │  │  1. Query SQLite for AWAITING_VERIFICATION brokers  │   │
  │  │  2. Poll IMAP for unread emails                     │   │
  │  │  3. Match sender domain to broker playbook entry    │   │
  │  │  4. Extract verification URL                        │   │
  │  │  5. Click URL via httpx or escalate to browser      │   │
  │  │  6. Update broker state → SCRUBBED or FAILED        │   │
  │  └─────────────────────────────────────────────────────┘   │
  └────────────────────────────────────────────────────────────┘

3.2 IMAP Connection Manager

  class IMAPConnection:
      def __init__(self, server, port, username, password):
          self._server = server
          self._port = port        # Typically 993
          self._username = username
          self._password = password
          self._conn = None

      async def connect(self) -> None:
          self._conn = imaplib.IMAP4_SSL(self._server, self._port)
          self._conn.login(self._username, self._password)
          self._conn.select("INBOX")

      async def poll_unread(self) -> list[dict]:
          """Return list of unread email dicts with sender, subject, body.
          
          CRITICAL: imaplib is synchronous and blocking. All IMAP operations
          MUST be wrapped in asyncio.to_thread() to avoid freezing the event loop.
          """
          status, messages = await asyncio.to_thread(
              self._conn.search, None, "UNSEEN"
          )
          if status != "OK":
              return []

          results = []
          for msg_id in messages[0].split():
              status, msg_data = await asyncio.to_thread(
                  self._conn.fetch, msg_id, "(RFC822)"
              )
              if status != "OK":
                  continue
              msg = email.message_from_bytes(msg_data[0][1])
              results.append({
                  "msg_id": msg_id,
                  "sender": msg["From"],
                  "subject": msg["Subject"],
                  "body": self._extract_body(msg),
                  "date": msg["Date"],
              })
          return results

      def _extract_body(self, msg) -> str:
          """Extract plain text body. Fall back to HTML if no text/plain found.
          
          Many broker transactional emails are text/html only. If text/plain is
          absent, extract the HTML body — URL regex works on HTML source too.
          """
          plain_text = None
          html_text = None
          
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

      async def mark_read(self, msg_id) -> None:
          await asyncio.to_thread(
              self._conn.store, msg_id, "+FLAGS", "\\Seen"
          )

      async def disconnect(self) -> None:
          if self._conn:
              self._conn.logout()

3.3 Verification Link Processor

  class VerificationProcessor:
      def __init__(self, playbook: dict):
          self._playbook = playbook  # broker_id → PlaybookEntry

      def extract_urls(self, body: str) -> list[str]:
          """Extract all HTTP/HTTPS URLs from email body and sanitize them.
          
          Regex extraction from HTML bodies often captures trailing HTML cruft
          (closing tags, quotes, brackets, entities). Each URL is sanitized
          before being returned.
          """
          raw_urls = re.findall(r'https?://[^\s<>"\')\]]+', body)
          return [self._sanitize_url(u) for u in raw_urls]

      @staticmethod
      def _sanitize_url(raw_url: str) -> str:
          """Strip trailing HTML/XML artifacts and decode HTML entities."""
          cleaned = raw_url.rstrip('">)]\'')
          cleaned = cleaned.replace("&amp;", "&")
          cleaned = cleaned.replace("&lt;", "<")
          cleaned = cleaned.replace("&gt;", ">")
          return cleaned

      def match_broker(self, sender: str, body: str) -> str | None:
          """
          Match sender domain or email body keywords to a playbook broker.
          Returns broker_id or None.
          """
          sender_lower = sender.lower()
          for broker_id, entry in self._playbook.items():
              seed_domain = urlparse(entry["seed_url"]).netloc.lower()
              if seed_domain in sender_lower:
                  return broker_id
              # Fallback: check for broker name in body
              if broker_id.lower() in body.lower():
                  return broker_id
          return None

      def select_verification_url(self, urls: list[str], broker_id: str) -> str | None:
          """Select the URL most likely to be the verification link."""
          entry = self._playbook.get(broker_id)
          if not entry:
              return urls[0] if urls else None
          seed_domain = urlparse(entry["seed_url"]).netloc
          # Prefer URLs matching broker's domain
          for url in urls:
              if seed_domain in url:
                  return url
          # Fallback: return longest URL (verification links tend to be long)
          return max(urls, key=len) if urls else None

3.4 Link Clicking with JS-Detection Heuristic

  async def click_verification_link(self, url: str) -> str:
      """
      Attempt headless verification via httpx GET.
      Returns "SCRUBBED", "ESCALATE_TO_BROWSER", or "FAILED".
      """
      try:
          async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
              resp = await client.get(url)
              if resp.status_code != 200:
                  return "FAILED"

              text = resp.text.lower()
              content_length = len(text.strip())

              # Heuristic 1: Blank SPA shell
              if content_length < 200:
                  return "ESCALATE_TO_BROWSER"

              # Heuristic 2: noscript tag present
              if "<noscript>" in text:
                  return "ESCALATE_TO_BROWSER"

              # Heuristic 3: Confirmation keywords
              if any(kw in text for kw in [
                  "confirmed", "verified", "thank you", "success",
                  "your request", "has been processed", "opt-out complete",
                  "removal confirmed", "suppression successful"
              ]):
                  return "SCRUBBED"

              # Heuristic 4: Ambiguous — escalate
              return "ESCALATE_TO_BROWSER"

      except Exception as e:
          logger.error(f"httpx click failed: {e}")
          return "FAILED"

Rationale for each heuristic:
  - <200 chars body: Single-page app that renders via JS (e.g., React, Vue).
    httpx gets an empty shell. Need a browser to execute JS.
  - <noscript> tag: Site explicitly requires JavaScript.
  - Confirmation keywords: The page resolved server-side and confirmed the
    verification. No browser needed. This is the 80% case.
  - Ambiguous: Page loaded but no confirmation text found. Could be a JS
    redirect or a custom UI. Escalate.

3.5 Browser Escalation Path

When the sentinel returns ESCALATE_TO_BROWSER:

  1. Sentinel marks broker as AWAITING_HUMAN_INTERVENTION (temporary state)
  2. Sentinel calls on_broker_start(broker_id, escalation_context) where
     escalation_context contains the verification URL as seed_url
  3. Browser Operator spawns a lightweight browser-use session
  4. Browser navigates to the URL, waits for confirmation
  5. Browser returns SUCCESS or FAILED
  6. Orchestrator updates state to SCRUBBED or FAILED

V1.0 note: Escalation is optional. If no Browser Operator is available
(e.g., headless mode, no API keys), the sentinel marks the broker as
FAILED with error "Verification link requires JavaScript — no browser available."

3.6 Polling Loop

  async def run(self):
      """Main polling loop. Runs until shutdown flag is set."""
      await self._imap.connect()

      while not self._shutdown.is_set():
          try:
              # Find brokers waiting for verification
              awaiting = self._db.get_brokers_by_status("AWAITING_VERIFICATION")
              if not awaiting:
                  await asyncio.sleep(60)
                  continue

              # Poll inbox
              emails = await self._imap.poll_unread()
              for email_data in emails:
                  broker_id = self._processor.match_broker(
                      email_data["sender"], email_data["body"]
                  )
                  if not broker_id or broker_id not in {b.broker_id for b in awaiting}:
                      continue

                  urls = self._processor.extract_urls(email_data["body"])
                  if not urls:
                      continue

                  url = self._processor.select_verification_url(urls, broker_id)
                  if not url:
                      continue

                  logger.info(f"Verification email found for {broker_id}. Clicking...")
                  result = await self._processor.click_verification_link(url)

                  await self._imap.mark_read(email_data["msg_id"])

                  if result == "SCRUBBED":
                      self._on_broker_complete(broker_id, {
                          "outcome": "SCRUBBED",
                          "verification_method": "httpx",
                      })
                  elif result == "ESCALATE_TO_BROWSER":
                      # V1.0: mark FAILED, log reason
                      # V1.1: trigger browser escalation
                      self._on_broker_complete(broker_id, {
                          "outcome": "FAILED",
                          "error": "Verification requires JS — escalation not implemented in V1.0",
                      })
                  else:
                      self._on_broker_complete(broker_id, {
                          "outcome": "FAILED",
                          "error": f"Verification click returned: {result}",
                      })

          except imaplib.IMAP4.error as e:
              logger.error(f"IMAP error: {e}. Reconnecting in 60s...")
              await asyncio.sleep(60)
              await self._imap.connect()

          await asyncio.sleep(60)

3.7 Verification Timeout

Broker verification links may expire. The sentinel maintains a timeout:

  VERIFICATION_TIMEOUT_SECONDS = 3600  # 1 hour

If a broker has been AWAITING_VERIFICATION for more than 1 hour,
the sentinel marks it as FAILED with error "Verification timeout — no email received."

This prevents brokers from sitting in AWAITING_VERIFICATION indefinitely.

================================================================================
4. SCENARIO WALKTHROUGHS
================================================================================

4.1 Happy Path — Email Arrives, httpx Click Works

  Given: ThatsThem broker in AWAITING_VERIFICATION state, sentinel polling
  When:  Broker sends verification email to sentinel_email
  Then:
    1. Polling loop detects unread email from thatsthem.com domain
    2. Processor matches sender domain → broker_id="thatsthem"
    3. URL extracted from email body
    4. httpx GET to verification URL
    5. Response body contains "thank you" → heuristic returns SCRUBBED
    6. Sentinel marks email as read
    7. Sentinel calls on_broker_complete(outcome="SCRUBBED")
    8. Orchestrator updates SQLite: AWAITING_VERIFICATION → SCRUBBED

4.2 JS-Required — httpx Gets Blank Page

  Given: Broker in AWAITING_VERIFICATION, verification link leads to SPA
  When:  httpx GET returns <200 chars body
  Then:
    1. Heuristic returns ESCALATE_TO_BROWSER
    2. V1.0: Sentinel marks broker FAILED with "requires JS" error
    3. Email still marked as read (don't re-process)
    4. V1.1: Would escalate to Browser Operator for JS execution

4.3 Timeout — No Email Arrives

  Given: Broker in AWAITING_VERIFICATION for 90 minutes
  When:  Polling loop checks elapsed time
  Then:
    1. Elapsed > VERIFICATION_TIMEOUT_SECONDS (3600s)
    2. Sentinel calls on_broker_complete(outcome="FAILED",
       error="Verification timeout")
    3. Orchestrator updates SQLite: AWAITING_VERIFICATION → FAILED

4.4 Multiple Brokers Awaiting Verification

  Given: 3 brokers all in AWAITING_VERIFICATION
  When:  Single email arrives from broker A
  Then:
    1. Processor matches sender → broker A only
    2. Broker A processed, brokers B and C continue waiting
    3. Polling loop checks all awaiting brokers each cycle

4.5 Edge Cases

  - IMAP connection lost mid-poll: Exception caught, reconnection attempted
    after 60s sleep. Loop continues.
  - Email with no plain text body: _extract_body returns empty string.
    Processor finds no URLs → email skipped, marked read.
  - Multiple URLs in email: select_verification_url prefers broker's domain
    then falls back to longest URL.
  - Duplicate verification emails: Email marked read after first processing.
    Subsequent identical emails from same sender are skipped (UNSEEN only).
  - IMAP credentials invalid: connect() raises IMAP4.error. Sentinel logs
    CRITICAL and exits. Brokers remain AWAITING_VERIFICATION until manual fix.
  - Empty playbook: No brokers to match. Processor returns None for all emails.
    Emails marked read, no state changes.

================================================================================
5. IMPLEMENTATION PLAN
================================================================================

All files under:
  /home/benblends2/DATA_Broker_Breaker_July_2026/privacy_exorcist/

Phase 1: IMAP connection manager (~100 lines)

  Files to create:
    privacy_exorcist/inbox_sentinel/__init__.py       (empty)
    privacy_exorcist/inbox_sentinel/imap_client.py     (~80 lines)

  imap_client.py contents:
    - IMAPClient class (connect, poll_unread, mark_read, disconnect)
    - _extract_body helper (plain text from multipart message)

  Test verification:
    - Unit test: poll_unread with mock IMAP → returns parsed email dicts
    - Unit test: _extract_body from multipart message → plain text
    - Unit test: _extract_body from HTML-only message → empty (graceful)
    - Unit test: mark_read calls IMAP store with correct flags

Phase 2: Verification processor (~100 lines)

  Files to create:
    privacy_exorcist/inbox_sentinel/verification.py    (~80 lines)

  verification.py contents:
    - VerificationProcessor class
    - extract_urls, match_broker, select_verification_url
    - click_verification_link (httpx GET with JS heuristic)
    - JS-detection heuristic constants (MIN_CONTENT_LENGTH=200)

  Test verification:
    - Unit test: extract_urls from body with 3 links → returns all 3
    - Unit test: match_broker with matching domain → broker_id
    - Unit test: match_broker with non-matching domain → None
    - Unit test: select_verification_url prefers broker domain
    - Unit test: click_verification_link with "confirmed" → SCRUBBED
    - Unit test: click_verification_link with empty body → ESCALATE_TO_BROWSER
    - Unit test: click_verification_link with noscript tag → ESCALATE_TO_BROWSER
    - Unit test: click_verification_link with HTTP error → FAILED

Phase 3: Inbox Sentinel service (~120 lines)

  Files to create:
    privacy_exorcist/inbox_sentinel/sentinel.py        (~100 lines)

  sentinel.py contents:
    - InboxSentinel class
      - __init__(db, imap_config, playbook, on_broker_complete_callback)
      - async run() — main polling loop
      - async _process_email(email_data) — per-email handling
      - async _check_timeouts() — verification timeout enforcement
      - async shutdown() — clean disconnect

  Test verification:
    - Integration test: mock IMAP returning verification email →
      on_broker_complete called with SCRUBBED
    - Integration test: mock IMAP returning email for unknown broker →
      on_broker_complete NOT called
    - Integration test: timeout check → broker older than 1h → FAILED
    - Integration test: IMAP error → reconnection attempted

================================================================================
6. TEST VECTORS
================================================================================

  ┌──────┬──────────────────────────────────┬──────────────────────────────┐
  │ #    │ Input                            │ Expected Output              │
  ├──────┼──────────────────────────────────┼──────────────────────────────┤
  │ TV01 │ HTML body: "Click here to        │ URL extracted:               │
  │      │ confirm: https://thatsthem.com   │ https://thatsthem.com/       │
  │      │ /verify?token=abc123"            │ verify?token=abc123          │
  ├──────┼──────────────────────────────────┼──────────────────────────────┤
  │ TV02 │ Sender: "noreply@thatsthem.com"  │ broker_id = "thatsthem"      │
  │      │ Playbook has thatsthem entry     │                              │
  ├──────┼──────────────────────────────────┼──────────────────────────────┤
  │ TV03 │ Sender: "unknown@random.com"     │ broker_id = None             │
  │      │ Body does not match any broker   │ (email skipped)              │
  ├──────┼──────────────────────────────────┼──────────────────────────────┤
  │ TV04 │ Multiple URLs: broker domain +   │ Selected: broker domain URL  │
  │      │ tracking pixel URL               │ (tracking pixel ignored)     │
  ├──────┼──────────────────────────────────┼──────────────────────────────┤
  │ TV05 │ httpx response: "Your email      │ Result: SCRUBBED             │
  │      │ has been confirmed. Thank you."  │                              │
  ├──────┼──────────────────────────────────┼──────────────────────────────┤
  │ TV06 │ httpx response: "<div id=app>"   │ Result: ESCALATE_TO_BROWSER  │
  │      │ (45 chars, blank SPA shell)      │ (content < 200 chars)        │
  ├──────┼──────────────────────────────────┼──────────────────────────────┤
  │ TV07 │ httpx response: "<noscript>      │ Result: ESCALATE_TO_BROWSER  │
  │      │ Please enable JavaScript"        │                              │
  ├──────┼──────────────────────────────────┼──────────────────────────────┤
  │ TV08 │ httpx response: HTTP 500         │ Result: FAILED               │
  ├──────┼──────────────────────────────────┼──────────────────────────────┤
  │ TV09 │ Broker AWAITING_VERIFICATION     │ Result: FAILED               │
  │      │ for 3700 seconds (> 1 hour)      │ error: "Verification timeout"│
  ├──────┼──────────────────────────────────┼──────────────────────────────┤
  │ TV10 │ IMAP connection drops mid-poll   │ Exception caught, reconnect  │
  │      │                                  │ after 60s, loop continues    │
  ├──────┼──────────────────────────────────┼──────────────────────────────┤
  │ TV11 │ Raw URL from HTML:               │ Cleaned:                     │
  │      │ "https://broker.com/verify?      │ https://broker.com/verify    │
  │      │  token=abc123&amp;x=y\">"        │ ?token=abc123&x=y            │
  │      │ (trailing quote, bracket, HTML   │                              │
  │      │  entity in query string)         │                              │
  └──────┴──────────────────────────────────┴──────────────────────────────┘

  TV12: HTML-only email body
    Input: Multipart email with text/html part, NO text/plain part
    Output: _extract_body returns HTML source (URL regex works on it)

  TV13: asyncio.to_thread wrapping
    Input: poll_unread() called from async context
    Output: self._conn.search wrapped in await asyncio.to_thread()
            (verified by test — event loop not blocked during IMAP call)

================================================================================
7. INTERFACES
================================================================================

7.1 Input (Configuration)

  .env variables:
    IMAP_SERVER        — e.g., imap.gmail.com
    IMAP_PORT          — e.g., 993
    IMAP_USERNAME      — e.g., jane.optout+sentinel@gmail.com
    IMAP_PASSWORD      — App-specific password (not account password)
    SENTINEL_EMAIL     — Must match profile.json sentinel_email

  Playbook (from SPEC-001):
    Used for domain matching. Each broker entry has a seed_url whose
    netloc is compared against email sender domains.

  SQLite (from SPEC-001):
    broker_ledger table — read AWAITING_VERIFICATION brokers,
    update to SCRUBBED or FAILED.

7.2 Output (to Orchestrator)

  Callback: on_broker_complete(broker_id, result_dict)
    result_dict = {
        "broker_id": str,
        "outcome": str,            # "SCRUBBED" or "FAILED"
        "verification_method": str, # "httpx" or None
        "error": str | None,
    }

7.3 External Protocols

  IMAP (SSL): Standard IMAP4rev1 over TLS on port 993.
  HTTP: httpx async client for verification link clicking.
  No LLM calls. No browser automation. No CapSolver.

================================================================================
8. OPEN QUESTIONS
================================================================================

  Q1: Should V1.0 implement browser escalation or defer to V1.1?
      The PRD says V1.0 does NOT include browser escalation for
      verification links. The sentinel marks JS-required links as FAILED.
      Recommendation: Ship V1.0 with FAILED for JS-required. Add escalation
      in V1.1 after Browser Operator is proven stable.

  Q2: What IMAP providers should we test against?
      Gmail (imap.gmail.com) is the most common for consumer email aliases.
      Recommendation: V1.0 targets Gmail IMAP. Add Fastmail, ProtonMail
      Bridge, and generic IMAP as community contributions.

  Q3: Should the sentinel handle CAPTCHA on verification pages?
      No. If a verification page has a CAPTCHA, the sentinel marks it FAILED
      with error "Verification page CAPTCHA." This is a broker hostile pattern
      that V1.0 does not address.

================================================================================
9. BUILD VERIFICATION CHECKLIST
================================================================================

  [ ] imap_client.py — IMAP connection, poll, mark_read, disconnect
  [ ] verification.py — URL extraction, domain matching, httpx click, JS heuristic
  [ ] sentinel.py — polling loop, timeout enforcement, shutdown
  [ ] All 10 test vectors pass
  [ ] Integration test: mock IMAP → verification → SCRUBBED
  [ ] Integration test: verification timeout → FAILED
  [ ] Integration test: IMAP disconnect → reconnect → loop continues
  [ ] Manual test: real Gmail inbox with test verification email
