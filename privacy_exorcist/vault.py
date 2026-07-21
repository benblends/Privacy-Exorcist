"""
Encrypted identity vault for PrivacyExorcist.

SPEC-005 §3.2: Passphrase-protected storage for profile PII using
Fernet (AES-128-CBC + HMAC-SHA256) with PBKDF2HMAC key derivation.

File format:
    [16-byte random salt][Fernet ciphertext]

Key derivation:
    passphrase → PBKDF2HMAC(SHA-256, 600K iterations, salt) → Fernet key
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from privacy_exorcist.models import Profile

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

SALT_LENGTH = 16
PBKDF2_ITERATIONS = 600_000  # OWASP 2023 recommendation
PBKDF2_KEY_LENGTH = 32       # 256 bits for Fernet


# ═══════════════════════════════════════════════════════════════════════════════
# Exceptions
# ═══════════════════════════════════════════════════════════════════════════════

class InvalidPassphraseError(ValueError):
    """Raised when vault unlock fails due to wrong passphrase.

    Covers both wrong passphrase and corrupted file — they are
    intentionally indistinguishable to prevent oracle attacks.
    """
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# Vault
# ═══════════════════════════════════════════════════════════════════════════════

class Vault:
    """Encrypted identity vault backed by a single file on disk.

    Usage:
        vault = Vault("profile.json.enc")

        # First run: create vault from profile dict
        profile = vault.create({"first_name": "Jane", ...}, "my-passphrase")

        # Normal run: unlock and return Profile
        profile = vault.unlock("my-passphrase")

        # Change passphrase
        vault.change_passphrase("old", "new")
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    # ── Public API ──────────────────────────────────────────────────────

    def exists(self) -> bool:
        """True if the vault file already exists on disk."""
        return self._path.is_file()

    def create(self, profile_dict: dict, passphrase: str) -> Profile:
        """Encrypt profile_dict and write the vault file.

        Args:
            profile_dict: Raw profile data (matches profile.json schema).
            passphrase: User-chosen passphrase (min 8 chars recommended).

        Returns:
            Validated Profile dataclass.

        Raises:
            ProfileValidationError: If profile_dict fails validation.
        """
        # Validate first — don't write anything if invalid
        profile = Profile.from_dict(profile_dict)

        # Serialize to JSON bytes
        plaintext = json.dumps(profile_dict, indent=2).encode("utf-8")

        # Generate random salt
        salt = os.urandom(SALT_LENGTH)

        # Derive key and encrypt
        key = _derive_key(passphrase, salt)
        fernet = Fernet(key)
        ciphertext = fernet.encrypt(plaintext)

        # Write: [salt][ciphertext]
        self._path.write_bytes(salt + ciphertext)

        return profile

    def unlock(self, passphrase: str) -> Profile:
        """Decrypt the vault and return a validated Profile.

        Args:
            passphrase: User's passphrase.

        Returns:
            Profile dataclass.

        Raises:
            InvalidPassphraseError: Wrong passphrase or corrupted file.
            FileNotFoundError: Vault file does not exist.
        """
        if not self._path.is_file():
            raise FileNotFoundError(f"Vault not found: {self._path}")

        data = self._path.read_bytes()

        if len(data) < SALT_LENGTH + 1:
            raise InvalidPassphraseError("Vault file is too short (corrupted?)")

        salt = data[:SALT_LENGTH]
        ciphertext = data[SALT_LENGTH:]

        try:
            key = _derive_key(passphrase, salt)
            fernet = Fernet(key)
            plaintext = fernet.decrypt(ciphertext)
        except InvalidToken:
            raise InvalidPassphraseError("Wrong passphrase")

        profile_dict = json.loads(plaintext.decode("utf-8"))
        return Profile.from_dict(profile_dict)

    def change_passphrase(
        self, old_passphrase: str, new_passphrase: str
    ) -> None:
        """Re-encrypt the vault with a new passphrase.

        Decrypts with old, re-encrypts with new.

        Raises:
            InvalidPassphraseError: Old passphrase is wrong.
        """
        # Unlock with old passphrase to get plaintext
        profile = self.unlock(old_passphrase)

        # Serialize and re-encrypt
        plaintext = json.dumps(
            {
                "first_name": profile.first_name,
                "last_name": profile.last_name,
                "middle_name": profile.middle_name,
                "aliases": profile.aliases,
                "current_street": profile.current_street,
                "current_city": profile.current_city,
                "current_state": profile.current_state,
                "current_zip": profile.current_zip,
                "current_phone": profile.current_phone,
                "past_zips": profile.past_zips,
                "birth_year": profile.birth_year,
                "sentinel_email": profile.sentinel_email,
            },
            indent=2,
        ).encode("utf-8")

        salt = os.urandom(SALT_LENGTH)
        key = _derive_key(new_passphrase, salt)
        fernet = Fernet(key)
        ciphertext = fernet.encrypt(plaintext)

        self._path.write_bytes(salt + ciphertext)


# ═══════════════════════════════════════════════════════════════════════════════
# Key Derivation
# ═══════════════════════════════════════════════════════════════════════════════

def _derive_key(passphrase: str, salt: bytes) -> bytes:
    """Derive a 256-bit Fernet key from a passphrase and salt.

    Uses PBKDF2HMAC with SHA-256 and 600,000 iterations.
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=PBKDF2_KEY_LENGTH,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))
