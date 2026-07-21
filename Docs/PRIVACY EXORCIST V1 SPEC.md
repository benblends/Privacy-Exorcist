## PRIVACY EXORCIST_V1_SPEC-005 Complete ✅ — 5,623 total lines, 147 tests, zero failures

### What shipped

| File | Lines | Purpose |
|---|---|---|
| `privacy_exorcist/vault.py` | 199 | Fernet + PBKDF2 encrypted vault with `[salt][ciphertext]` file format |
| `privacy_exorcist/cli/wizard.py` | 223 | Interactive profile builder with field validation |
| `start.sh` | 41 | One-command launcher with venv + dependency checks |
| `main.py` | 385 (rewrite) | Vault integration, `--status`, `--setup`, plaintext migration |
| `privacy_exorcist/cli/config.py` | +5 | Accepts pre-loaded Profile instead of file path |
| `tests/test_spec005.py` | 233 | 19 tests covering TV01–TV18 |

### How it works now

```
./start.sh              → vault unlock → engine runs
./start.sh --status     → read SQLite directly, no passphrase
./start.sh --setup      → force re-run profile wizard
./start.sh --help       → usage
```

**First run**: no vault exists → interactive wizard → encrypted vault created → engine runs.

**Plaintext migration**: existing `profile.json` detected → prompt to encrypt → delete plaintext.

**Wrong passphrase**: 3 attempts → `InvalidPassphraseError` → exit 1.

### Vault internals

- **Format**: `[16-byte random salt][Fernet(AES-128-CBC+HMAC) ciphertext]`
- **Key derivation**: `PBKDF2HMAC(SHA-256, 600K iterations, salt)`
- **Wrong passphrase detection**: `InvalidToken` from Fernet → `InvalidPassphraseError`
- **No external dependencies**: `cryptography` already in venv (browser-use transitive dep)

### Full project status

| Phase | Spec | Tests |
|---|---|---|
| ✅ Phase 1 | SPEC-001 Core Engine | 67 |
| ✅ Phase 2 | SPEC-002 + SPEC-003 Browser + Inbox | 42 |
| ✅ Phase 3 | SPEC-004 CLI/HMI + main.py | 19 |
| ✅ Phase 5 | SPEC-005 Vault + Startup + Wizard | 19 |
| **Total** | **PrivacyExorcist V1.0** | **147** |
