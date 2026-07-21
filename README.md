# 🔒 Privacy Exorcist

**Local-first, AI-driven data broker opt-out engine.**

Privacy Exorcist autonomously identifies and purges your personally identifiable information (PII) from high-impact data brokers. It uses a multi-modal visual AI agent to reason through web forms, outmaneuver anti-consumer dark patterns, and execute data suppression requests — all running locally on your machine.

> **V1.0 is spike-validated**: 9 experimental runs against live brokers. ThatsThem automated deletion confirmed in 68 seconds using CapSolver + GPT-4o vision.

---

## How It Works

```
┌─────────────────────────────────────────────────┐
│                  start.sh                        │
│        (one command — that's it)                 │
└────────────────────┬────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────┐
│              Orchestrator Engine                 │
│  ┌──────────┐  ┌────────────┐  ┌─────────────┐  │
│  │ Browser  │  │  Inbox     │  │  Identity   │  │
│  │ Operator │  │  Sentinel  │  │  Vault      │  │
│  │          │  │            │  │  (encrypted) │  │
│  └──────────┘  └────────────┘  └─────────────┘  │
│       │              │                            │
│  ┌────▼──────────────▼──────────────────────┐    │
│  │         SQLite Ledger                     │    │
│  │    (broker states, retries, timing)       │    │
│  └───────────────────────────────────────────┘    │
└──────────────────────────────────────────────────┘
```

1. **Identity Vault**: Your PII is encrypted at rest with Fernet (AES-128 + HMAC), protected by a passphrase only you know. The plaintext never touches disk.

2. **Browser Operator**: A GPT-4o vision agent navigates broker websites, visually identifies form fields, fills them with your profile data, and solves CAPTCHAs via CapSolver — all through a stealth Chromium browser with anti-detection countermeasures.

3. **Inbox Sentinel**: Polls your verification email inbox via IMAP for broker confirmation links. Clicks them via lightweight HTTP — no browser needed for 80% of verifications.

4. **State Machine**: Every broker flows through a deterministic FSM. Retries with exponential backoff. CAPTCHA loop guards prevent token burn. Full audit trail in SQLite.

---

## Quickstart

### Prerequisites

- **Python 3.11+**
- **Chromium** (installed automatically by Playwright)
- **API keys**: OpenAI (GPT-4o) and optionally CapSolver (for automated CAPTCHA solving)

### Setup

```bash
# Clone
git clone https://github.com/benblends/Privacy-Exorcist.git
cd Privacy-Exorcist

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install browser-use python-dotenv cryptography rich httpx requests

# Install Chromium for browser automation
python3 -m playwright install chromium

# Configure API keys
mkdir -p ~/.hermes
cat > ~/.hermes/.env << 'EOF'
OPENAI_API_KEY=sk-your-key-here
CAPSOLVER_API_KEY=CAP-your-key-here
HEADLESS=true
EOF
```

### Run

```bash
./start.sh
```

On first run, you'll be guided through an interactive profile setup wizard. Your information is encrypted and stored in `profile.json.enc`.

### Flags

| Command | Purpose |
|---|---|
| `./start.sh` | Normal run (unlock vault → scrub brokers) |
| `./start.sh --status` | Show broker ledger (no passphrase needed) |
| `./start.sh --setup` | Re-run profile wizard |
| `./start.sh --help` | Usage |

---

## Architecture

```
privacy_exorcist/
├── models.py              # 10 FSM states, 9 result codes, 5 dataclasses
├── database.py            # SQLite persistence (broker_ledger + run_history)
├── state_machine.py       # FSM with CAPTCHA branching + retry logic
├── loaders.py             # profile.json + playbook.json validation
├── engine.py              # Orchestrator (callback hooks, lifecycle)
├── vault.py               # Fernet + PBKDF2 encrypted identity vault
├── browser_operator/
│   ├── browser_factory.py  # Stealth Chromium profile
│   ├── task_builder.py     # GPT-4o prompt construction
│   ├── capsolver_action.py # Direct HTTP CapSolver + CDP injection
│   ├── result_mapper.py    # Agent output → structured result codes
│   └── operator.py         # BrowserOperator.execute()
├── inbox_sentinel/
│   ├── imap_client.py      # Async IMAP via asyncio.to_thread()
│   ├── verification.py     # URL extraction, domain matching, httpx click
│   └── sentinel.py         # 60s polling loop + timeout enforcement
└── cli/
    ├── formatter.py        # Rich terminal output + HITL prompts
    ├── config.py           # .env validation + IMAP all-or-nothing
    ├── wizard.py           # Interactive profile builder
    └── signals.py          # Double-Ctrl+C graceful shutdown
```

---

## Security Model

| Concern | Approach |
|---|---|
| **PII at rest** | Fernet (AES-128-CBC + HMAC) with PBKDF2 key derivation (600K iterations). Salt prepended to ciphertext. |
| **PII in transit** | Screenshots sent to GPT-4o. Sitekeys + page URLs sent to CapSolver. Disclosed in data boundary docs. |
| **Credential isolation** | IMAP credentials never touch the browser agent. Browser API keys never touch the IMAP client. |
| **Plaintext on disk** | Profile JSON never written unencrypted. Existing plaintext is detected and migrated on first run. |
| **Passphrase recovery** | None. Passphrase is the only key. Write it down. No backdoor. |

---

## Anti-Bot Defense Layers

| Layer | Mechanism | Status |
|---|---|---|
| 1 — TLS Fingerprinting | Stealth Chromium args + real Chrome UA string | ✅ Bypassed (spike-validated) |
| 2 — CAPTCHA (Turnstile, reCAPTCHA) | CapSolver API or Human-in-the-Loop fallback | ✅ Solved (spike-validated) |
| 3 — Behavioral (DataDome) | Proxy + UA synchronization | ⬜ V1.1 target |

---

## Playbook

Broker targets are defined in `playbook.json`. Each entry specifies the opt-out URL, known CAPTCHA type, sitekey, success anchor text, and flow type.

V1.0 includes a curated playbook. Community contributions welcome — the playbook is designed to be maintainable without code changes.

```json
{
  "brokers": [
    {
      "broker_id": "thatsthem",
      "seed_url": "https://thatsthem.com/optout",
      "success_anchor": "Request Submitted",
      "flow_type": "DIRECT_FORM",
      "captcha_type": "cloudflare_turnstile",
      "captcha_sitekey": "0x4AAAAAACiKzu913X3aFRkP"
    }
  ]
}
```

---

## Development

```bash
# Run tests (147 tests, zero failures)
source .venv/bin/activate
python -m pytest tests/ -v

# Run a specific spec's tests
python -m pytest tests/test_spec001.py -v
```

### Specs

Full technical specifications in `specs/`:

| Spec | Scope | Status |
|---|---|---|
| SPEC-001 | Core Engine & FSM | ✅ Implemented |
| SPEC-002 | Browser Operator & Anti-Bot | ✅ Implemented |
| SPEC-003 | Inbox Sentinel & Verification | ✅ Implemented |
| SPEC-004 | CLI, HMI & Configuration | ✅ Implemented |
| SPEC-005 | Identity Vault & Startup | ✅ Implemented |

---

## Roadmap

**V1.1** (planned):
- DataDome bypass via proxy + UA sync
- Browser escalation for JS-required verification links
- `--dry-run` mode
- `--quiet` mode
- Multi-profile support

**V2.0** (future):
- Local web dashboard (Flask + SSE)
- Community playbook registry
- Scheduled recurring scrubs via cron

---

## License

MIT. See [LICENSE](LICENSE).

---

*"Your PII stays on your machine. Screenshots of broker pages (which may contain PII) are sent to your configured LLM provider for visual reasoning."*
