"""
SPEC-005 test vectors — vault encryption, wizard validation, CLI flags.

Tests TV01–TV18: vault create/unlock/change, wrong passphrase,
salt uniqueness, wizard field validation, --status, migration, corruption.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from privacy_exorcist.cli.wizard import (
    _validate_email,
    _validate_phone,
    _validate_state,
    _validate_zip,
)
from privacy_exorcist.models import Profile
from privacy_exorcist.vault import InvalidPassphraseError, Vault


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

VALID_PROFILE_DICT = {
    "first_name": "Jane",
    "last_name": "Doe",
    "current_street": "123 Main St",
    "current_city": "Austin",
    "current_state": "TX",
    "current_zip": "78701",
    "current_phone": "512-555-0147",
    "sentinel_email": "jane@example.com",
}

TEST_PASSPHRASE = "correct-horse-battery-staple"
WRONG_PASSPHRASE = "wrong-passphrase-123"


@pytest.fixture
def vault_path():
    fd, path = tempfile.mkstemp(suffix=".enc")
    os.close(fd)
    os.unlink(path)  # Remove the temp file — we just want a path
    yield Path(path)
    try:
        os.unlink(path)
    except OSError:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# TV01–TV05: Vault Core
# ═══════════════════════════════════════════════════════════════════════════════

class TestVaultCore:

    def test_tv01_create_writes_file(self, vault_path):
        """TV01: create() writes the vault and returns a Profile."""
        vault = Vault(vault_path)
        profile = vault.create(VALID_PROFILE_DICT, TEST_PASSPHRASE)
        assert vault_path.is_file()
        assert profile.first_name == "Jane"
        data = vault_path.read_bytes()
        assert len(data) > 16 + 50  # salt + at least Fernet overhead

    def test_tv02_unlock_correct_passphrase(self, vault_path):
        """TV02: unlock() with correct passphrase returns same data."""
        vault = Vault(vault_path)
        vault.create(VALID_PROFILE_DICT, TEST_PASSPHRASE)
        profile = vault.unlock(TEST_PASSPHRASE)
        assert profile.first_name == "Jane"
        assert profile.last_name == "Doe"
        assert profile.current_zip == "78701"

    def test_tv03_wrong_passphrase(self, vault_path):
        """TV03: Wrong passphrase → InvalidPassphraseError."""
        vault = Vault(vault_path)
        vault.create(VALID_PROFILE_DICT, TEST_PASSPHRASE)
        with pytest.raises(InvalidPassphraseError):
            vault.unlock(WRONG_PASSPHRASE)

    def test_tv04_salt_is_random(self, vault_path):
        """TV04: Two creates with same passphrase → different files (random salt)."""
        v1 = Vault(vault_path)
        v1.create(VALID_PROFILE_DICT, TEST_PASSPHRASE)
        data1 = vault_path.read_bytes()

        # Create second vault at different path
        fd2, path2 = tempfile.mkstemp(suffix=".enc")
        os.close(fd2)
        try:
            v2 = Vault(path2)
            v2.create(VALID_PROFILE_DICT, TEST_PASSPHRASE)
            data2 = Path(path2).read_bytes()
            # First 16 bytes (salt) should differ
            assert data1[:16] != data2[:16]
            # Unlock both with same passphrase — should both work
            p1 = v1.unlock(TEST_PASSPHRASE)
            p2 = v2.unlock(TEST_PASSPHRASE)
            assert p1.first_name == p2.first_name
        finally:
            os.unlink(path2)

    def test_tv05_change_passphrase(self, vault_path):
        """TV05: change_passphrase → old fails, new works."""
        vault = Vault(vault_path)
        vault.create(VALID_PROFILE_DICT, TEST_PASSPHRASE)
        vault.change_passphrase(TEST_PASSPHRASE, "new-password-456")

        # Old passphrase fails
        with pytest.raises(InvalidPassphraseError):
            vault.unlock(TEST_PASSPHRASE)

        # New passphrase works
        profile = vault.unlock("new-password-456")
        assert profile.first_name == "Jane"


# ═══════════════════════════════════════════════════════════════════════════════
# TV06–TV10: Wizard Validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestWizardValidation:

    def test_tv06_state_valid(self):
        assert _validate_state("TX") is True
        assert _validate_state("CA") is True

    def test_tv07_state_invalid(self):
        assert _validate_state("Texas") is False
        assert _validate_state("tx") is False
        assert _validate_state("T") is False
        assert _validate_state("") is False

    def test_tv08_zip_valid(self):
        assert _validate_zip("78701") is True
        assert _validate_zip("78701-1234") is True

    def test_zip_invalid(self):
        assert _validate_zip("abcde") is False
        assert _validate_zip("7870") is False
        assert _validate_zip("") is False

    def test_phone_valid(self):
        assert _validate_phone("5125550147") is True
        assert _validate_phone("512-555-0147") is True
        assert _validate_phone("(512) 555-0147") is True

    def test_phone_invalid(self):
        assert _validate_phone("512") is False
        assert _validate_phone("abc") is False

    def test_email_valid(self):
        assert _validate_email("user@example.com") is True
        assert _validate_email("a@b.co") is True

    def test_email_invalid(self):
        assert _validate_email("not-an-email") is False
        assert _validate_email("@example.com") is False
        assert _validate_email("user@") is False


# ═══════════════════════════════════════════════════════════════════════════════
# TV15–TV18: Edge Cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestVaultEdgeCases:

    def test_exists(self, vault_path):
        vault = Vault(vault_path)
        assert vault.exists() is False
        vault.create(VALID_PROFILE_DICT, TEST_PASSPHRASE)
        assert vault.exists() is True

    def test_tv18_corrupted_file(self, vault_path):
        """TV18: Flipping a byte in the ciphertext → InvalidPassphraseError."""
        vault = Vault(vault_path)
        vault.create(VALID_PROFILE_DICT, TEST_PASSPHRASE)

        data = bytearray(vault_path.read_bytes())
        # Flip a byte in the ciphertext (after salt)
        data[20] ^= 0xFF
        vault_path.write_bytes(data)

        with pytest.raises(InvalidPassphraseError):
            vault.unlock(TEST_PASSPHRASE)

    def test_file_too_short(self, vault_path):
        """File shorter than salt → InvalidPassphraseError."""
        vault_path.write_bytes(b"short")
        vault = Vault(vault_path)
        with pytest.raises(InvalidPassphraseError):
            vault.unlock(TEST_PASSPHRASE)

    def test_empty_passphrase(self, vault_path):
        """Empty passphrase still encrypts/decrypts (wizard warns, not vault)."""
        vault = Vault(vault_path)
        vault.create(VALID_PROFILE_DICT, "")
        profile = vault.unlock("")
        assert profile.first_name == "Jane"

    def test_unlock_nonexistent_file(self):
        vault = Vault("/tmp/nonexistent_vault_xyz.enc")
        with pytest.raises(FileNotFoundError):
            vault.unlock(TEST_PASSPHRASE)


# ═══════════════════════════════════════════════════════════════════════════════
# Legacy profile loading still works via config fallback
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfigBackwardCompat:

    def test_load_config_with_preloaded_profile(self, tmp_path, monkeypatch):
        """Config accepts a pre-loaded Profile (no file needed)."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

        profile = Profile.from_dict(VALID_PROFILE_DICT)
        playbook = tmp_path / "playbook.json"
        playbook.write_text('{"brokers":[{"broker_id":"test","seed_url":"https://t.com","success_anchor":"OK"}]}')

        from privacy_exorcist.cli.config import load_config
        config = load_config(profile=profile, playbook_path=str(playbook))
        assert config.is_valid
        assert config.profile.first_name == "Jane"
