================================================================================
SPEC-006: V1.1 — BROWSER HARDENING, MULTI-STAGE FLOWS & FALLBACK STABILITY
================================================================================
Author: Zoey (Architect)
Status: DRAFT — awaiting review
Date: 2026-07-21
Live-test basis: 7 runs across 5 brokers (ThatsThem, FPS, TPS, Radaris, PeekYou)
Replaces: sections of SPEC-002 (§ browser_factory) + SPEC-003 (§ sentinel)
Depends on: SPEC-001 (core engine — unchanged)

═══════════════════════════════════════════════════════════════════════════════════
§1 CONTEXT: WHAT V1.0 TAUGHT US
═══════════════════════════════════════════════════════════════════════════════════

V1.0 demonstrated a working DIRECT_FORM pipeline: ThatsThem scrubbed in 66.8
seconds with CapSolver Turnstile solve + HITL fallback. The engine, state
machine, vault, and CLI are all production-quality.

However, live testing against 4 additional brokers revealed three systematic gaps:

  1. Cloudflare Turnstile blocks 3 of 5 brokers before any form interaction.
     Our stealth Chromium config (Layer 1 TLS bypass) works sometimes but not
     reliably enough for headless server automation. Error 600010 from CapSolver
     indicates the Turnstile challenge itself is flagging the browser instance.

  2. SEARCH_AND_CLAIM flows require multi-stage navigation that the current
     single-string prompt cannot express. Radaris needs: search main directory
     → extract profile URL → navigate to control/privacy → paste URL → solve
     CAPTCHA → submit. A flat prompt with one seed_url can't encode this.

  3. The Ollama Gemma4 fallback works for text but wraps structured output in
     markdown code fences, breaking browser-use's AgentOutput parser.
     dont_force_structured_output mitigates this but reduces output quality.

═══════════════════════════════════════════════════════════════════════════════════
§2 SCOPE & NON-SCOPE
═══════════════════════════════════════════════════════════════════════════════════

IN SCOPE FOR V1.1:
  - Browser fingerprint hardening (Camoufox or CDP-stealth patches)
  - Multi-stage playbook entries with separate search + form phases
  - Fallback LLM provider validation (structured output testing)
  - ReCAPTCHA v2 sitekey auto-detection improvements
  - SEARCH_AND_CLAIM agent timeout increase (300s → 600s)
  - Inbox Sentinel activation for email verification links
  - --dry-run mode (validate config + playbook without executing agents)
  - Agent step budget increase (25 → 50 for complex flows)

OUT OF SCOPE (V1.2+):
  - DataDome (Layer 3) behavioral bypass (Whitepages)
  - Residential proxy rotation
  - Multi-profile support
  - Local web dashboard (Flask + SSE)
  - Scheduled recurring scrubs via cron
  - Community playbook registry

═══════════════════════════════════════════════════════════════════════════════════
§3 TECHNICAL DESIGN
═══════════════════════════════════════════════════════════════════════════════════

§3.1 — BROWSER FINGERPRINT HARDENING

  Problem:
    Cloudflare Turnstile returns error 600010 ("bot detection") on 60% of runs.
    Our Chromium stealth args bypass Layer 1 (TLS fingerprint — 403 confirmed
    gone) but Layer 2 behavioral detection sees CDP traces, navigator.webdriver,
    and WebGL fingerprint anomalies.

  Solution: Camoufox engine swap.
    Camoufox is a stealth Firefox fork with native Playwright integration.
    It patches navigator.webdriver, WebGL fingerprint, and CDP traces at the
    binary level — not via Chrome flags. It integrates as a drop-in Playwright
    browser channel:

    ```python
    from camoufox import Camoufox
    browser = await camoufox.launch(headless=True, ...)
    ```

    browser-use doesn't natively support Camoufox, so we need a thin adapter.
    In practice this means a new `browser_factory.py` implementation that
    creates a Camoufox browser instance instead of Chromium, and a
    `BrowserProfile` subclass or wrapper that browser-use can consume.

  Risk:
    Camoufox is Firefox-based. Our CapSolver CDP token injection uses Chrome
    DevTools Protocol — Firefox uses a different protocol. We'll need to
    verify that Camoufox's CDP emulation layer supports Runtime.evaluate
    (it likely does — the standard Firefox CDP subset includes it).

  Acceptance criteria:
    - 3 consecutive headless runs against FPS or TPS without 600010
    - CapSolver token injection still functional via CDP
    - chromium_sandbox=False equivalent flag identified for Firefox
    - Stealth config documented in playbook entries

§3.2 — MULTI-STAGE PLAYBOOK ENTRIES

  Problem:
    Radaris, FPS, and TPS all require the agent to search a directory first,
    then navigate to a separate opt-out form. The current playbook has a
    single seed_url and a single flow_type. The SEARCH_AND_CLAIM builder tries
    to express both stages in one prompt, but the agent gets stuck on the wrong
    page (as seen in run #7 — spent 25 steps searching from /control/privacy
    instead of the main directory).

  Solution: Add a `stages` array to playbook entries. Each stage has its own
    URL, instructions, and success anchor. The task builder concatenates them
    into a sequential prompt. The orchestrator doesn't change — stages are
    purely a prompt-level concept.

  New playbook schema (additive — existing entries unchanged):
    ```json
    {
      "broker_id": "radaris",
      "stages": [
        {
          "stage": "search",
          "url": "https://radaris.com/",
          "instructions": [
            "Search for {first_name} {last_name} in {city}, {state}",
            "Locate the profile matching ZIP {zip}",
            "Click 'View Profile' and copy the full URL from the address bar",
            "Store the URL — you'll paste it in the next stage"
          ],
          "success_anchor": "View Profile"
        },
        {
          "stage": "optout",
          "url": "https://radaris.com/control/privacy",
          "instructions": [
            "Paste the profile URL from Stage 1 into the form",
            "Enter email: {email}",
            "Solve any CAPTCHA challenge",
            "Click submit"
          ],
          "success_anchor": "request has been submitted"
        }
      ],
      "captcha_type": "recaptcha_v2",
      "known_blockers": ["recaptcha"]
    }
    ```

  Backward compatibility:
    If `stages` is absent, the builder falls back to the existing
    single-prompt behavior (DIRECT_FORM or SEARCH_AND_CLAIM). V1.0
    entries need no changes.

  Task builder changes:
    - New `_build_staged_task()` that loops `stages[]` and produces a
      numbered multi-part prompt with clear stage boundaries.
    - Agent step budget increased from 25 to 50 for staged flows.
    - Agent timeout increased from 300s to 600s for staged flows.

§3.3 — FALLBACK LLM STRUCTURED OUTPUT VALIDATION

  Problem:
    Gemma4:cloud wraps its JSON output in markdown code fences (```json...```).
    browser-use's AgentOutput expects raw JSON. Adding
    dont_force_structured_output=True tells browser-use to accept unstructured
    text, which works but produces lower-quality agent actions.

  Solution:
    1. Add an `--llm-test` flag that validates each configured LLM provider
       against browser-use's structured output format before a live run.
       Tests: text generation, vision (blank image), structured JSON output.
    2. Research alternative Ollama models that produce clean JSON without
       markdown wrapping. Candidates: Qwen 3.5 (vision-capable, thinking
       optional), Mistral Large 3 (vision, no thinking — unavailable via
       cloud yet).
    3. If no perfect Ollama fallback exists, document the trade-off and
       add a WARNING banner when dont_force_structured_output is active.

§3.4 — RECAPTCHA V2 SITEKEY AUTO-DETECTION

  Problem:
    Our playbook entries for FPS, TPS, and Radaris all have captcha_sitekey:
    null. The CapSolver action tries to detect sitekey via CDP DOM query
    (`document.querySelector('[data-sitekey]')`), but reCAPTCHA v2 uses
    different markup than Turnstile. The reCAPTCHA sitekey is embedded in the
    widget's data-sitekey attribute on a different element.

  Solution:
    Add a reCAPTCHA-specific sitekey extraction path in capsolver_action.py.
    Fallback query: `document.querySelector('.g-recaptcha')?.dataset?.sitekey`.
    If both fail, the action returns "CAPTCHA_BLOCKED: Could not determine
    sitekey" — which in headed mode triggers the HITL prompt. This is
    sufficient for V1.1 since HITL works reliably.

§3.5 — INBOX SENTINEL ACTIVATION

  Problem:
    SPEC-003 (Inbox Sentinel) was built but never wired into main.py or tested
    live. Brokers that send email verification links (FPS, TPS) are currently
    stuck at SUBMITTED because nothing clicks the verification link.

  Solution:
    The Inbox Sentinel is already built. Activating it requires:
    1. main.py: if IMAP credentials are present, start sentinel as a
       background asyncio task before the broker loop.
    2. When a broker hits SUBMITTED → AWAITING_VERIFICATION, the sentinel
       polls for the verification email and calls finish_broker() with
       VERIFICATION_SUCCESS.
    3. Timeout enforcement: the sentinel already has 1-hour timeout from
       SPEC-003 — wired to state machine transitions.

§3.6 — DRY-RUN MODE

  Problem:
    Every playbook change required a live run to validate. We burned ~$0.50
    in OpenAI tokens on FPS/TPS that were blocked at Cloudflare before any
    form interaction.

  Solution:
    `./start.sh --dry-run` validates without executing browser agents:
    1. Load vault, playbook, config
    2. Print config summary (same as normal run)
    3. For each broker: print seed_url + flow_type + known_blockers
    4. Skip agent execution entirely
    5. Exit 0 if all validations pass, exit 1 with errors

    This replaces the need for a dedicated spike script per broker.

═══════════════════════════════════════════════════════════════════════════════════
§4 IMPLEMENTATION PHASES
═══════════════════════════════════════════════════════════════════════════════════

Phase 1: Browser Hardening Spike (Camoufox)
  - Spike: install Camoufox, verify CDP token injection works
  - Spike: run 3× headless against FPS — each run counts as pass/fail
  - Spike: test reCAPTCHA v2 solve via Camoufox CDP
  - Decision: ship Camoufox or fall back to CDP stealth patches
  - Tests: 8 test vectors (TLS bypass, Turnstile solve, CDP injection,
    reCAPTCHA sitekey detection, headless + headed modes)

Phase 2: Multi-Stage Playbook (staged SEARCH_AND_CLAIM)
  - models.py: add BrokerStage dataclass
  - loaders.py: parse stages array, backward-compat with flat entries
  - task_builder.py: _build_staged_task()
  - engine.py: stage_count → agent_step_budget (25→50) + timeout (300→600)
  - Tests: 10 test vectors (stage parsing, prompt construction, backward
    compat, step budget, timeout)

Phase 3: Fallback LLM Validation
  - cli/formatter.py: --llm-test flag handler
  - operator.py: structured output test function
  - Tests: 5 test vectors (text gen, vision, JSON output, markdown fence
    detection, temperature sensitivity)

Phase 4: Inbox Sentinel Activation
  - main.py: async sentinel task + orchestrator wiring
  - engine.py: AWAITING_VERIFICATION → SCRUBBED transition on sentinel success
  - Tests: 6 test vectors (IMAP polling, email match, link click, timeout,
    shutdown)

Phase 5: Dry-Run + Polish
  - main.py: --dry-run flag
  - cli/wizard.py: no changes
  - Tests: 3 test vectors (config validation, playbook summary, exit codes)

═══════════════════════════════════════════════════════════════════════════════════
§5 TEST VECTORS
═══════════════════════════════════════════════════════════════════════════════════

Phase 1 (Browser Hardening):
  TV01: Camoufox launches headless without sandbox errors
  TV02: Turnstile solve returns token (not 600010)
  TV03: CDP Runtime.evaluate injects Turnstile token
  TV04: reCAPTCHA v2 sitekey auto-detected via .g-recaptcha selector
  TV05: Headed mode — browser window opens, user sees Firefox
  TV06: Viewport jitter still works with Camoufox
  TV07: 3 consecutive headless runs without Cloudflare block
  TV08: Token injection triggers ___turnstileCallback when present

Phase 2 (Multi-Stage):
  TV09: Staged entry parses correctly from playbook.json
  TV10: _build_staged_task() produces numbered multi-part prompt
  TV11: Missing stages field → fallback to flat task builder
  TV12: Agent step budget increased for staged entries
  TV13: Agent timeout increased for staged entries
  TV14: Stage-specific success_anchor used per stage
  TV15: Two-stage prompt includes URL from stage 1 in stage 2 context
  TV16: Single-stage entry (DIRECT_FORM) unchanged from V1.0
  TV17: _build_staged_task includes captcha_sitekey per stage
  TV18: Empty stages array → falls back to flat task builder

Phase 3 (Fallback Validation):
  TV19: Gemma4:cloud text generation works
  TV20: Gemma4:cloud vision (image input) works
  TV21: dont_force_structured_output=True parses Gemma4 output
  TV22: OpenAI gpt-4o structured output works (unchanged)
  TV23: --llm-test flag validates all providers

Phase 4 (Inbox Sentinel):
  TV24: Sentinel polls IMAP and finds verification email
  TV25: Email with non-matching sender is ignored
  TV26: Verification link click → SCRUBBED
  TV27: Sentinel timeout → AWAITING_VERIFICATION → FAILED
  TV28: Sentinel graceful shutdown on SIGINT
  TV29: No IMAP credentials → sentinel skipped (no crash)

Phase 5 (Dry-Run):
  TV30: --dry-run validates config without agent execution
  TV31: --dry-run prints broker summary (seed_url + blockers)
  TV32: --dry-run exit code reflects validation result

═══════════════════════════════════════════════════════════════════════════════════
§6 PLAYBOOK: V1.1 TARGET MATRIX
═══════════════════════════════════════════════════════════════════════════════════

| Broker | Flow | Challenge | V1.1 Path |
|---|---|---|---|
| thatsthem | DIRECT_FORM | ✅ Done | Retained, no changes |
| fastpeoplesearch | staged SEARCH_AND_CLAIM | Cloudflare Turnstile | Camoufox bypass → auto-solve |
| truepeoplesearch | staged SEARCH_AND_CLAIM | Cloudflare Turnstile | Camoufox bypass → auto-solve |
| radaris | staged SEARCH_AND_CLAIM | reCAPTCHA v2 | Multi-stage + auto-detect sitekey |
| whitepages | staged SEARCH_AND_CLAIM | DataDome (L3) | Deferred to V1.2 |

Removed (V1.0): PeekYou (NXDOMAIN — domain is dead)
Parked (V1.2): Whitepages, Nuwber — require Layer 3 behavioral bypass

═══════════════════════════════════════════════════════════════════════════════════
§7 TEST SUITE IMPACT
═══════════════════════════════════════════════════════════════════════════════════

V1.0: 147 tests, zero failures.
V1.1 estimated: 147 + 32 new = ~179 tests.

Existing tests (SPEC-001 through SPEC-005): ALL preserved. No breaking changes.
New test files: test_spec006_phase1.py through test_spec006_phase5.py.
