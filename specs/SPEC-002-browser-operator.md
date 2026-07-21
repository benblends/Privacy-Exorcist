================================================================================
SPEC-002: BROWSER OPERATOR & ANTI-BOT LAYER
================================================================================
PrivacyExorcist — V1.0
Status: Draft for Review
Date: 2026-07-19
Depends On: SPEC-001 (Core Engine & State Machine) — FSM interface locked
Build Phase: 2 (Browser Automation — depends on Phase 1 foundation)

================================================================================
1. PURPOSE
================================================================================

This spec defines the Browser Operator: the sub-agent that executes data broker
opt-out flows using browser-use, Playwright/Chromium, and CapSolver CAPTCHA
solving. It receives a high-level task from the Orchestrator (SPEC-001), executes
the flow, and returns a structured BrokerResult code.

This is the highest-risk component in the system. It must handle:
  - Three layers of anti-bot defense (TLS fingerprint, Turnstile/CAPTCHA, DataDome)
  - Multiple broker flow types (DIRECT_FORM, SEARCH_AND_CLAIM)
  - Dynamic form field mapping (profile.json → visual form fields)
  - CAPTCHA detection → CapSolver API → token injection → resubmission
  - Success anchor recognition (confirmation text scraping)
  - Graceful failure with actionable return codes

Every pattern in this spec has been validated against a live broker (ThatsThem)
and produced a confirmed automated deletion in 68 seconds.

================================================================================
2. CODEBASE GROUND TRUTH
================================================================================

2.1 What Exists Already (Spike Scripts)

Working spike scripts in ~/DATA_Broker_Breaker_July_2026/:

  capsolver_v3.py (6,752 bytes)
    — Full end-to-end ThatsThem flow with direct HTTP CapSolver integration
    — Uses CDP for Turnstile token injection
    — Produced confirmed SUCCESS: "Request Submitted" (run #9)
    — Key patterns:
        * BrowserProfile with stealth args + chromium_sandbox=False
        * @controller.action with explicit special_context params (no **kwargs)
        * CDP Runtime.evaluate for DOM interaction
        * Direct HTTP to CapSolver (not SDK — parameter mismatch bug)
        * AntiTurnstileTaskProxyLess task type

  stealth_spike.py (5,207 bytes)
    — Proved TLS fingerprint bypass (ThatsThem: 403 → page loads)
    — Stealth BrowserProfile configuration

  e2e_thatsthem.py (7,191 bytes)
    — Full form fill + submit + success anchor detection
    — Agent task construction pattern

Framework skill (authoritative quirks reference):
  ~/.hermes/skills/software-development/privacy-exorcist-spike-campaign/SKILL.md

PRD (spike-validated architecture):
  ~/DATA_Broker_Breaker_July_2026/PRD_PrivacyExorcist.txt
    — Anti-bot defense layers (§3.2), playbook schema (§5.2), return code taxonomy (§3.4)

SPEC-001 (FSM contract):
  ~/DATA_Broker_Breaker_July_2026/specs/SPEC-001-core-engine.md
    — BrokerResult enum, state transition guards, TaskContext schema (§7.1)

2.2 What Does NOT Exist

  - Production-grade BrowserOperator class
  - Task construction from playbook entry + profile
  - Playbook-driven controller action selection
  - Agent result → BrokerResult code mapping
  - CapSolver attempt counter and loop guard (per SPEC-001 §3.3)
  - Browser session lifecycle management (create, reuse, teardown)

2.3 Dependencies

  SPEC-001 (Core Engine) — imports BrokerResult, receives TaskContext,
    calls on_broker_complete with result dict.
  SPEC-003 (Inbox Sentinel) — VERIFICATION_REQUIRED flow triggers inbox poll.
  SPEC-004 (CLI/HMI) — terminal output during execution.

================================================================================
3. DESIGN
================================================================================

3.1 Architecture

The Browser Operator is a single class that wraps browser-use:

  ┌─────────────────────────────────────────────────────┐
  │                 BrowserOperator                     │
  │                                                     │
  │  ┌─────────────────┐  ┌──────────────────────────┐  │
  │  │ BrowserFactory   │  │ TaskBuilder              │  │
  │  │ (BrowserProfile  │  │ (profile + playbook      │  │
  │  │  + stealth args) │  │  → agent task string)    │  │
  │  └────────┬────────┘  └───────────┬──────────────┘  │
  │           │                       │                  │
  │  ┌────────┴───────────────────────┴──────────────┐  │
  │  │           Agent Runner                        │  │
  │  │  (browser-use Agent + Controller + CapSolver) │  │
  │  └───────────────────────┬───────────────────────┘  │
  │                          │                           │
  │  ┌───────────────────────┴───────────────────────┐  │
  │  │         Result Mapper                          │  │
  │  │  (agent final_result → BrokerResult enum)     │  │
  │  └───────────────────────────────────────────────┘  │
  └─────────────────────────────────────────────────────┘

The Orchestrator calls:
  result = await browser_operator.execute(task_context)
  → returns dict with outcome, duration, final_state, captcha_solved, error

3.2 Browser Configuration (Stealth Baseline)

Every browser session uses this configuration (spike-validated, run #4):

  from browser_use import BrowserProfile

  STEALTH_USER_AGENT = (
      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
  )

  browser_profile = BrowserProfile(
      headless=True,                # V1.0 runs headless
      disable_security=True,        # Required for restricted Linux
      chromium_sandbox=False,       # MANDATORY — avoids 30s BrowserStartEvent timeout
      user_agent=STEALTH_USER_AGENT,
      args=[
          "--disable-blink-features=AutomationControlled",  # Strips webdriver flag
          "--disable-infobars",                              # Removes "Chrome is controlled"
          "--no-sandbox",
          "--disable-dev-shm-usage",
      ],
  )

CRITICAL: chromium_sandbox=False is MANDATORY. Omitting it causes the 30-second
BrowserStartEvent timeout that killed spike runs #1-4. This is a system-level
requirement, not optional.

3.3 CapSolver Controller Action

The browser-use Agent carries a Controller with one custom action. The Controller
maintains a per-broker-run `captcha_attempts` counter as an instance attribute to
enforce the loop guard independently of the Orchestrator (defense in depth):

  class CapSolverController:
      def __init__(self, capsolver_key: str | None, playbook_entry: dict):
          self._key = capsolver_key
          self._playbook = playbook_entry
          self.captcha_attempts = 0  # Reset per broker execution

      def create_controller(self) -> Controller:
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
          ):
              # LOOP GUARD: prevent infinite CapSolver retries
              self.captcha_attempts += 1
              if self.captcha_attempts > 2:
                  return (
                      "CAPTCHA_LOOP_GUARD_TRIGGERED: CapSolver token rejected "
                      "multiple times. Do not attempt further CAPTCHA solves. "
                      "Report CAPTCHA_BLOCKED or request human intervention."
                  )

              # 1. Get CDP session for current page
              # 2. Check playbook_entry for known captcha_type + sitekey
              # 3. If playbook has sitekey → call CapSolver directly
              # 4. If playbook has no sitekey → use CDP Runtime.evaluate to detect
              # 5. Call CapSolver API (direct HTTP, not SDK)
              # 6. Poll for solution (20 attempts × 2s = 40s max)
              # 7. Inject token via CDP Runtime.evaluate
              # 8. Return status string to agent

          return controller

The loop guard fires AT THE ACTION LEVEL — solve_captcha itself refuses to
execute beyond 2 attempts. This is defense in depth: even if the Orchestrator's
captcha_attempts counter (SPEC-001 §3.3) fails to catch the loop, the Browser
Operator's own guard catches it at the controller boundary.

CRITICAL FRAMEWORK QUIRKS (from 5 failed runs):
  - All special_context params must be listed EXPLICITLY — no **kwargs
  - Use CDP (cdp_client.send.Runtime.evaluate), not Playwright Page
  - Get CDP session via: browser_session.session_manager._get_session_for_target()
  - CapSolver Python SDK v1.0.7 has a parameter name mismatch — use direct HTTP

3.4 Task Construction

The TaskBuilder converts a TaskContext (from SPEC-001 §7.1) into a natural
language task string for the browser-use Agent:

  def build_task(ctx: TaskContext) -> str:
      profile = ctx["profile"]
      playbook = ctx["playbook_entry"]

      base = f"""
  CRITICAL: If you see a CAPTCHA or "verify you are human" challenge,
  you MUST use the 'Solve CAPTCHA' tool. DO NOT click it yourself.

  GOAL: Complete the opt-out flow on {playbook['broker_id']}.
  1. Navigate to {playbook['seed_url']}
  2. Fill the form with:
     - Name: {profile.first_name} {profile.last_name}
     - Street: {profile.street}
     - City: {profile.city}
     - State: {profile.state}
     - Zip: {profile.zip}
     - Phone: {profile.phone}
     - Email: {profile.sentinel_email}
  3. Check required consent checkboxes
  4. Use 'Solve CAPTCHA' if you see any challenge
  5. Click submit
  6. Look for success confirmation: "{playbook['success_anchor']}"
  7. Report SUCCESS if confirmed, or describe what blocked you.
  SYNTHETIC test data only.
  """

Prompt engineering rules (spike-validated, runs #7-9):
  - CAPTCHA instruction MUST be in a CRITICAL block BEFORE numbered steps
  - Use "MUST" and "DO NOT" language for CAPTCHA handling
  - Include the exact success_anchor text from the playbook
  - Mention "Solve CAPTCHA" action by name (matches @controller.action string)

3.5 Agent Execution

  async def _run_agent(self, task: str) -> AgentHistory:
      llm = ChatOpenAI(model="gpt-4o", api_key=self._openai_key, temperature=0.1)
      agent = Agent(
          task=task,
          llm=llm,
          browser_profile=self._browser_profile,
          controller=self._controller,
      )
      return await agent.run()  # NOTE: async — agent.run() returns coroutine

Temperature is 0.1 for deterministic behavior. The agent must follow the
script consistently, not explore creative paths.

3.6 Result Mapping

The agent's final_result() is a natural language string. The ResultMapper
classifies it into a BrokerResult enum:

  def map_result(self, final_text: str, playbook: dict) -> str:
      text = final_text.lower()

      # Success anchors (check playbook.success_anchor first)
      if playbook['success_anchor'].lower() in text:
          if "verification" in text or "email" in text:
              return "VERIFICATION_REQUIRED"
          return "SUCCESS"

      # CAPTCHA outcomes
      if "captcha_blocked" in text:
          return "CAPTCHA_BLOCKED"
      if ("captcha_loop_guard_triggered" in text or
          ("captcha" in text and "token rejected" in text)):
          return "CAPTCHA_BLOCKED"
      if "captcha" in text and ("solve" in text or "failed" in text):
          return "CAPTCHA_DETECTED"

      # Error outcomes
      if "403" in text or "forbidden" in text:
          return "BLOCKED_403"
      if "no match" in text or "no results" in text or "not found" in text:
          return "NO_RECORD"
      if "timeout" in text or "unreachable" in text or "503" in text:
          return "BROKER_UNREACHABLE"
      if "form" in text and ("rejected" in text or "error" in text):
          return "FORM_SUBMIT_FAILED"
      if "multiple match" in text or "too many" in text:
          return "MULTIPLE_MATCH"

      # Default: partial completion
      return "FAILED"

The mapping order matters — success_anchor check comes first to avoid false
negatives when the page says both "submitted" and "captcha."

3.7 Security Boundary

The Browser Operator:
  - Receives a sanitized TaskContext (profile fields, NOT raw profile.json path)
  - Does NOT access SQLite directly (returns result to Orchestrator)
  - Does NOT access IMAP credentials (Inbox Sentinel is separate)
  - Sends screenshots to LLM endpoint (disclosed in PRD data boundary)
  - Sends sitekey + page_url to CapSolver API (disclosed)

================================================================================
4. SCENARIO WALKTHROUGHS
================================================================================

4.1 Happy Path — Direct Form, Turnstile, CapSolver (ThatsThem)

  Given: TaskContext for ThatsThem with captcha_type=cloudflare_turnstile,
         captcha_sitekey=0x4AAAAAACiKzu913X3aFRkP, CAPSOLVER_API_KEY set
  When:  BrowserOperator.execute(task_context) is called
  Then:
    1. BrowserProfile created with stealth args
    2. Task string built with all profile fields + success_anchor
    3. Agent navigates to https://thatsthem.com/optout (~2s)
    4. Agent visually identifies form fields, fills all 7 fields (~15s)
    5. Agent calls 'Solve CAPTCHA' action (~2s reasoning)
    6. CapSolver API called: AntiTurnstileTaskProxyLess → token (~8s)
    7. Token injected via CDP → Turnstile passes (~1s)
    8. Agent clicks submit (~1s)
    9. Page shows "Request Submitted" → agent reports SUCCESS (~2s)
    10. ResultMapper returns BrokerResult.SUCCESS
    11. Duration: ~30-70s (spike-validated: 68.4s for run #9)

4.2 CAPTCHA Detected → CapSolver Rejected → Loop Guard

  Given: TaskContext for broker with Turnstile, CAPSOLVER_API_KEY set,
         but CapSolver returns invalid token (e.g., expired sitekey)
  When:  BrowserOperator.execute() is called
  Then:
    1. Agent fills form → hits CAPTCHA → calls 'Solve CAPTCHA'
    2. CapSolver returns token → token injected → form submitted
    3. Server rejects token → page reloads with CAPTCHA again
    4. Agent calls 'Solve CAPTCHA' again (captcha_attempts=1)
    5. CapSolver returns second token → token injected → form submitted
    6. Server rejects again → page reloads with CAPTCHA
    7. Agent calls 'Solve CAPTCHA' again (captcha_attempts=2)
    8. LOOP GUARD: captcha_attempts >= 2 → return CAPTCHA_DETECTED
       (Orchestrator handles fallback per SPEC-001 §3.3)

  Note: The loop guard is enforced by the Orchestrator, not the Browser
  Operator. The Browser Operator returns CAPTCHA_DETECTED each time. The
  Orchestrator tracks captcha_attempts and forces escalation at >= 2.

4.3 No CAPTCHA — Direct Success

  Given: TaskContext for a broker with no known CAPTCHA
  When:  BrowserOperator.execute() is called
  Then:
    1. Agent navigates, fills form, clicks submit
    2. Page shows success_anchor text → agent reports SUCCESS
    3. No CapSolver calls (captcha_solves = 0 in ledger)
    4. Duration: ~20-30s

4.4 Broker Unreachable

  Given: TaskContext with seed_url that returns 503
  When:  BrowserOperator.execute() is called
  Then:
    1. Agent navigates → page load fails
    2. Agent reports error → ResultMapper maps to BROKER_UNREACHABLE
    3. Orchestrator applies retry with backoff (SPEC-001)

4.5 Edge Cases

  - Form field mismatch: Agent fills email in newsletter field instead of
    opt-out field → form validation error → ResultMapper returns
    FORM_SUBMIT_FAILED.
  - Page layout change: Agent cannot find submit button → reports FAILED
    with description of what it sees.
  - Browser crash: Agent.run() raises exception → caught, mapped to
    BROKER_UNREACHABLE with crash trace in error field.
  - CapSolver timeout: 40s polling timeout → return CAPTCHA_BLOCKED.
  - Headless Chromium crash: Process exits → caught by browser-use watchdog
    → raise RuntimeError → mapped to BROKER_UNREACHABLE.
  - Agent hallucination: Agent claims SUCCESS but success_anchor not in
    final text → ResultMapper returns FAILED (success_anchor check is the
    primary classifier).

================================================================================
5. IMPLEMENTATION PLAN
================================================================================

All files under:
  /home/benblends2/DATA_Broker_Breaker_July_2026/privacy_exorcist/

Phase 1: Browser factory + stealth config (~80 lines)

  Files to create:
    privacy_exorcist/browser_operator/__init__.py     (empty)
    privacy_exorcist/browser_operator/browser_factory.py  (~60 lines)

  browser_factory.py contents:
    - STEALTH_USER_AGENT constant
    - STEALTH_ARGS constant (list of Chromium flags)
    - create_browser_profile(headless: bool) → BrowserProfile
      - Always applies stealth args + chromium_sandbox=False
      - Returns configured BrowserProfile

  Test verification:
    - Unit test: BrowserProfile created with correct headless setting
    - Unit test: stealth args include --disable-blink-features
    - Unit test: chromium_sandbox is False

Phase 2: Task builder (~100 lines)

  Files to create:
    privacy_exorcist/browser_operator/task_builder.py  (~80 lines)

  task_builder.py contents:
    - build_direct_form_task(ctx: TaskContext) → str
      - Builds task for DIRECT_FORM flow_type
      - Includes CRITICAL CAPTCHA block, profile fields, success_anchor
    - build_search_and_claim_task(ctx: TaskContext) → str
      - Builds task for SEARCH_AND_CLAIM flow_type
      - Includes search instructions + profile identification + form fill
    - build_task(ctx: TaskContext) → str
      - Dispatches to correct builder based on playbook_entry.flow_type

  Test verification:
    - Unit test: DIRECT_FORM task includes all 7 profile fields
    - Unit test: CRITICAL CAPTCHA block appears before numbered steps
    - Unit test: SEARCH_AND_CLAIM task includes search instructions
    - Unit test: success_anchor text present in task

Phase 3: CapSolver integration (~120 lines)

  Files to create:
    privacy_exorcist/browser_operator/capsolver_action.py  (~100 lines)

  capsolver_action.py contents:
    - solve_turnstile_via_api(sitekey, page_url, api_key) → token
      - Direct HTTP POST to api.capsolver.com/createTask
      - Polls getTaskResult every 2s, max 20 attempts (40s)
      - Raises CapSolverError on failure
    - create_capsolver_controller() → Controller
      - Registers 'Solve CAPTCHA' action
      - Action body: detect CAPTCHA type → call CapSolver → inject token
      - Returns configured Controller instance

  Test verification:
    - Unit test: solve_turnstile_via_api with mock HTTP → returns token
    - Unit test: create_capsolver_controller returns Controller with action
    - Integration test: mock CapSolver response → token injected via CDP

Phase 4: Result mapper (~80 lines)

  Files to create:
    privacy_exorcist/browser_operator/result_mapper.py  (~60 lines)

  result_mapper.py contents:
    - map_result(final_text, playbook_entry) → BrokerResult enum value
    - Classification order (see §3.6)

  Test verification:
    - Unit test: "Request Submitted" + success_anchor → SUCCESS
    - Unit test: "verify your email" + success_anchor → VERIFICATION_REQUIRED
    - Unit test: "captcha_blocked" → CAPTCHA_BLOCKED
    - Unit test: "403 forbidden" → BLOCKED_403
    - Unit test: "no results found" → NO_MATCH_FOUND
    - Unit test: "connection timeout" → BROKER_UNREACHABLE
    - Unit test: empty string → FAILED

Phase 5: Browser Operator class (~200 lines)

  Files to create:
    privacy_exorcist/browser_operator/operator.py  (~180 lines)

  operator.py contents:
    - BrowserOperator class
      - __init__(openai_key, capsolver_key, headless)
      - async execute(task_context) → dict (result for Orchestrator)
      - _build_agent(task) → Agent
      - _run_with_timeout(agent, timeout=300) → AgentHistory
    - Integration: wires BrowserFactory + TaskBuilder + CapSolver + ResultMapper

  Test verification:
    - Integration test: mock agent returning "Request Submitted" →
      execute returns SUCCESS
    - Integration test: mock agent returning "captcha" →
      execute returns CAPTCHA_DETECTED
    - Integration test: agent.run() raises exception →
      execute returns BROKER_UNREACHABLE with error
    - Integration test: timeout (300s) → execute returns BROKER_UNREACHABLE

================================================================================
6. TEST VECTORS
================================================================================

Each test vector is a (final_text, playbook_success_anchor, flow_type) tuple
mapped to an expected BrokerResult.

  ┌──────┬──────────────────────────────┬──────────────────┬──────────────┐
  │ #    │ Agent final_result()         │ success_anchor   │ Expected     │
  ├──────┼──────────────────────────────┼──────────────────┼──────────────┤
  │ TV01 │ "I see 'Request Submitted'   │ Request          │ SUCCESS      │
  │      │  on the page. SUCCESS."      │ Submitted        │              │
  │ TV02 │ "Form submitted. A           │ submitted        │ VERIFICATION_│
  │      │  verification email was      │                  │ REQUIRED     │
  │      │  sent to the address."       │                  │              │
  │ TV03 │ "CAPTCHA_BLOCKED: challenge  │ any              │ CAPTCHA_     │
  │      │  could not be solved."       │                  │ BLOCKED      │
  │ TV04 │ "I see a CAPTCHA challenge.  │ any              │ CAPTCHA_     │
  │      │  solve_captcha failed."      │                  │ DETECTED     │
  │ TV05 │ "403 Forbidden error.        │ any              │ BLOCKED_403  │
  │      │  Cannot access page."        │                  │              │
  │ TV06 │ "Search returned no results  │ any              │ NO_RECORD    │
  │      │  for John Smith."            │                  │              │
  │ TV07 │ "Connection timeout.         │ any              │ BROKER_      │
  │      │  Site unreachable."          │                  │ UNREACHABLE  │
  │ TV08 │ "Form submitted but got      │ any              │ FORM_SUBMIT_ │
  │      │  validation error."          │                  │ FAILED       │
  │ TV09 │ "Too many matching records   │ any              │ MULTIPLE_    │
  │      │  found. Cannot pick one."    │                  │ MATCH        │
  │ TV10 │ "I completed the task."      │ done             │ SUCCESS      │
  │      │  (success_anchor: done)      │                  │              │
  └──────┴──────────────────────────────┴──────────────────┴──────────────┘

  TV11: Task construction — DIRECT_FORM
    Input: TaskContext with flow_type=DIRECT_FORM, 7 profile fields
    Output: Task string containing all 7 fields, CRITICAL CAPTCHA block,
            success_anchor text, "Solve CAPTCHA" by name

  TV12: Task construction — SEARCH_AND_CLAIM
    Input: TaskContext with flow_type=SEARCH_AND_CLAIM
    Output: Task string containing search instructions + form fill

  TV13: CapSolver API integration
    Input: sitekey="0x4AAAAAACiKzu913X3aFRkP", page_url="https://thatsthem.com/optout"
    Output: Token string (non-empty, ~500+ chars)

  TV14: Stealth browser config
    Input: headless=True
    Output: BrowserProfile with chromium_sandbox=False, stealth args,
            user_agent matching Chrome 131 Linux

  TV15: CAPTCHA loop guard (controller-level)
    Input: solve_captcha called 3 times on same broker run
    Output: 1st call → normal CapSolver flow
            2nd call → normal CapSolver flow
            3rd call → "CAPTCHA_LOOP_GUARD_TRIGGERED: ..."
            ResultMapper classifies this as CAPTCHA_BLOCKED

================================================================================
7. INTERFACES
================================================================================

7.1 Input (from Orchestrator — SPEC-001 §7.1)

  TaskContext dict:
    {
        "broker_id": str,
        "seed_url": str,
        "profile": Profile dataclass,
        "playbook_entry": PlaybookEntry dataclass,
        "capsolver_key": str | None,
        "headless": bool,
    }

7.2 Output (to Orchestrator)

  Result dict:
    {
        "broker_id": str,
        "outcome": str,          # BrokerResult enum value
        "duration_seconds": float,
        "final_state": str,      # Agent's final_result() text, truncated to 3KB
        "captcha_solved": bool,
        "captcha_attempts": int, # Number of CapSolver calls this run
        "error": str | None,
    }

7.3 External APIs

  OpenAI Chat Completions:
    - Model: gpt-4o
    - Called by: browser-use Agent (internal)
    - Purpose: Vision reasoning, form field identification, navigation

  CapSolver HTTP API:
    - Endpoint: POST https://api.capsolver.com/createTask
    - Endpoint: POST https://api.capsolver.com/getTaskResult
    - Task types: AntiTurnstileTaskProxyLess, ReCaptchaV2TaskProxyLess,
      HCaptchaTaskProxyLess
    - Purpose: CAPTCHA token generation

================================================================================
8. OPEN QUESTIONS
================================================================================

  Q1: Should the Browser Operator support non-Turnstile CAPTCHA types in V1.0?
      The spike only validated AntiTurnstileTaskProxyLess (ThatsThem).
      reCAPTCHA v2 and hCaptcha are supported by CapSolver but untested.
      Recommendation: Include stubs for all three. Mark untested types
      with a warning in the playbook. Add as brokers are encountered.

  Q2: Should browser sessions be reused across brokers?
      Reusing saves startup time (~3s per broker) but risks cookie/session
      contamination. Recommendation: Fresh session per broker for V1.0.
      Session reuse as V1.1 optimization.

  Q3: How to handle MULTIPLE_MATCH results?
      The agent finds multiple records but can't auto-disambiguate.
      Recommendations: V1.0 marks MULTIPLE_MATCH → FAILED (terminal).
      V1.1 adds a selection sub-routine (ask user which record).

================================================================================
9. BUILD VERIFICATION CHECKLIST
================================================================================

  [ ] browser_factory.py — stealth BrowserProfile creation
  [ ] task_builder.py — DIRECT_FORM + SEARCH_AND_CLAIM task builders
  [ ] capsolver_action.py — CapSolver 'Solve CAPTCHA' controller action
  [ ] result_mapper.py — agent text → BrokerResult enum
  [ ] operator.py — BrowserOperator.execute() with full lifecycle
  [ ] All 14 test vectors pass
  [ ] Integration test: mock agent → SUCCESS
  [ ] Integration test: mock agent → CAPTCHA_DETECTED
  [ ] Integration test: agent crash → BROKER_UNREACHABLE
  [ ] Live integration test: ThatsThem end-to-end (requires API keys)
