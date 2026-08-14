"""Encryption for TOTP secrets at rest, and the constant-time digest helpers around them.

THE ONE IMPLEMENTATION, DELIBERATELY
    Both the backend (verifying a login) and `bin/manage_operator.py` (enrolling an operator) need
    to encrypt and decrypt `operator_totp.secret_encrypted`. Two implementations only have to
    disagree by a byte — nonce length, concatenation order, base64 variant, associated data — and
    the failure is that a seeded operator cannot log in, with nothing visibly wrong at either end.
    The CLI imports from here rather than reimplementing.

STORAGE FORMAT
    base64(nonce ‖ ciphertext ‖ tag), standard base64 with padding.

    - nonce: 12 bytes from `os.urandom`. 12 is the GCM-recommended size; a nonce reused under the
      same key destroys GCM's guarantees entirely, so it is generated per-encryption and never
      derived from anything.
    - ciphertext ‖ tag: exactly what `AESGCM.encrypt` returns (the 16-byte tag is appended).
    - No associated data. There is nothing here worth binding: the row is reached by primary key and
      an attacker who can swap one operator's ciphertext for another's already holds UPDATE on
      `operator_totp`, which `rh_auth` has only column-wise and `rh_app` does not have at all.

    A base32 TOTP secret is 32 chars; encrypted and base64'd it lands near 60+, which is why
    migration 012's CHECK on this column has a length floor — a plaintext secret written here by
    mistake is rejected by the database rather than silently stored in the clear.

THE KEY
    `TOTP_SECRET_ENC_KEY`, base64 of exactly 32 bytes, from `backend/.env` (gitignored, excluded
    from the build context, already home to ANTHROPIC_API_KEY). Per AUTH_THREAT_MODEL §7 it must
    never appear in rh-db, the repo, an image layer, a compose file, or a log line — a key stored in
    the database it protects is disclosed by exactly the event it exists to survive.

    Validated to exactly 32 decoded bytes on load, so a truncated or misconfigured value fails at
    startup rather than at the first login attempt.

WHAT THIS DOES NOT DEFEND
    Host compromise (§6). The key sits on the same machine as the database. The asymmetry it buys is
    specific and real: backups leave the box, `backend/.env` does not. The threat it addresses is the
    leaked `pg_dump`, which is by a wide margin the most likely disclosure route here.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets

from argon2 import PasswordHasher
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "ARGON2_MEMORY_COST",
    "ARGON2_PARALLELISM",
    "ARGON2_TIME_COST",
    "PASSWORD_HASHER",
    "EncryptionKeyError",
    "SecretDecryptionError",
    "decrypt_totp_secret",
    "digest_token",
    "encrypt_totp_secret",
    "generate_token",
    "load_enc_key",
    "tokens_equal",
]

KEY_ENV_VAR = "TOTP_SECRET_ENC_KEY"
_KEY_BYTES = 32  # AES-256
_NONCE_BYTES = 12

# ── Argon2id password hashing — THE ONE HASHER, same reasoning as the cipher above ────────────
#
# §5.1 parameters (9b ADR-002, ported): 64 MiB memory cost collapses GPU/ASIC parallelism;
# parallelism is pinned to 1 so a single verification's wall-clock is deterministic.
#
# Both bin/manage_operator.py (hashing at seed/reset) and the backend service (verifying at
# login, and burning the §5.2 dummy verification on the unknown-email branch) MUST use this one
# instance. PHC strings are self-describing, so a CLI that hashed with different parameters
# would still VERIFY fine — but the verification would run at the stored hash's cost while the
# dummy verify runs at this hasher's cost, and the difference is a per-request timing oracle
# that answers "does this email exist?". (Measured on this host before the parameters were
# unified: argon2-cffi's default parallelism=4 verified in ~52 ms while the pinned p=1 dummy
# took ~132 ms — unknown addresses were 2.5x SLOWER, §5.2's defence inverted.) Changing any of
# these numbers is a deliberate migration: existing hashes keep their old parameters until
# rehashed, and the dummy path diverges from them for exactly that long.
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 65536  # KiB — 64 MiB
ARGON2_PARALLELISM = 1

PASSWORD_HASHER = PasswordHasher(
    time_cost=ARGON2_TIME_COST,
    memory_cost=ARGON2_MEMORY_COST,
    parallelism=ARGON2_PARALLELISM,
)


class EncryptionKeyError(RuntimeError):
    """The encryption key is missing or malformed. Raised at load, never mid-login."""


class SecretDecryptionError(RuntimeError):
    """Stored ciphertext could not be decrypted: wrong key, or the value was tampered with.

    Deliberately does not distinguish the two. GCM authenticates, so a tag failure means the bytes
    are not what we wrote — and telling a caller *which* failure occurred is an oracle we gain
    nothing from.
    """


def load_enc_key(raw: str | None = None) -> bytes:
    """Decode and validate the encryption key.

    Fails loudly and early. A truncated key would otherwise surface as a decryption failure on some
    future login, which reads as "TOTP is broken" rather than "the key is wrong".
    """
    value = raw if raw is not None else os.environ.get(KEY_ENV_VAR)
    if not value:
        raise EncryptionKeyError(
            f"{KEY_ENV_VAR} is not set. Generate one with: "
            f"openssl rand -base64 32   (then put it in backend/.env, mode 0600)"
        )
    try:
        key = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise EncryptionKeyError(f"{KEY_ENV_VAR} is not valid base64") from exc
    if len(key) != _KEY_BYTES:
        # Length only — the value itself is never echoed.
        raise EncryptionKeyError(
            f"{KEY_ENV_VAR} decodes to {len(key)} bytes, expected exactly {_KEY_BYTES}"
        )
    return key


def encrypt_totp_secret(plaintext: str, key: bytes | None = None) -> str:
    """Encrypt a base32 TOTP secret into the value stored in operator_totp.secret_encrypted."""
    if not plaintext:
        raise ValueError("refusing to encrypt an empty TOTP secret")
    k = key if key is not None else load_enc_key()
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(k).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + sealed).decode("ascii")


def decrypt_totp_secret(stored: str, key: bytes | None = None) -> str:
    """Inverse of encrypt_totp_secret. Raises SecretDecryptionError on tamper or wrong key."""
    k = key if key is not None else load_enc_key()
    try:
        blob = base64.b64decode(stored, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SecretDecryptionError("stored TOTP secret is not valid base64") from exc
    if len(blob) <= _NONCE_BYTES:
        raise SecretDecryptionError("stored TOTP secret is too short to contain a nonce")
    nonce, sealed = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    try:
        return AESGCM(k).decrypt(nonce, sealed, None).decode("utf-8")
    except InvalidTag as exc:
        raise SecretDecryptionError("stored TOTP secret failed authentication") from exc


def generate_token(n_bytes: int = 32) -> str:
    """A URL-safe CSPRNG token: session cookies, challenge tokens, verification tokens.

    32 bytes, not 16. These are bearer credentials with no second factor behind them once issued.
    """
    return secrets.token_urlsafe(n_bytes)


def digest_token(token: str) -> str:
    """SHA-256 hex of a bearer token — what the database stores, never the token itself.

    Plain SHA-256 rather than a password hash is correct HERE and wrong for passwords: these tokens
    are 32 CSPRNG bytes, so there is no dictionary to slow down, and login-path latency is a real
    cost. `operators.password_hash` uses Argon2id for the opposite reason.

    Matches the 64-hex CHECK that migration 012 puts on every *_hash column.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    """Constant-time comparison, so a lookup cannot be turned into a timing oracle."""
    return hmac.compare_digest(a, b)
