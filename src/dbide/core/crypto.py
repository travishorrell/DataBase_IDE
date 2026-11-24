"""Password hashing (one-way) and field encryption (reversible).

Hashing is for passwords: you can check a guess against a hash but never get
the password back. Encryption is for data you must read again (API keys,
personal details) and is only as safe as the key.

Every stored value is self-describing, so the algorithm can be detected later:
  bcrypt   $2b$12$...
  argon2id $argon2id$v=19$m=65536,t=3,p=4$...
  pbkdf2   pbkdf2_sha256$600000$<salt>$<hash>
  scrypt   scrypt$16384$8$1$<salt>$<hash>
  encrypted values: enc:v1:<algo>:<base64 payload>
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass

# ---- algorithms -------------------------------------------------------------


@dataclass(frozen=True)
class Algorithm:
    key: str
    label: str
    kind: str  # "hash" or "encrypt"
    summary: str


HASH_ALGORITHMS = [
    Algorithm("argon2id", "Argon2id", "hash",
              "Current best practice (winner of the Password Hashing Competition). "
              "Deliberately slow and memory-hungry, so guessing passwords with GPUs is expensive."),
    Algorithm("bcrypt", "bcrypt", "hash",
              "Battle-tested since 1999 and supported everywhere. Only uses the first 72 bytes of a password."),
    Algorithm("scrypt", "scrypt", "hash",
              "Memory-hard like Argon2 and built into Python. A solid choice when Argon2 isn't available."),
    Algorithm("pbkdf2_sha256", "PBKDF2-SHA256", "hash",
              "Many rounds of SHA-256. FIPS-approved and universally available, but cheaper for attackers "
              "with GPUs than Argon2 or scrypt."),
]

ENCRYPT_ALGORITHMS = [
    Algorithm("aes256gcm", "AES-256-GCM", "encrypt",
              "The industry standard. Encrypts and also detects tampering (authenticated encryption)."),
    Algorithm("chacha20", "ChaCha20-Poly1305", "encrypt",
              "Authenticated encryption that is fast without AES hardware; used by TLS 1.3 and WireGuard."),
    Algorithm("fernet", "Fernet (AES-128-CBC + HMAC)", "encrypt",
              "A simple, hard-to-misuse format from the Python 'cryptography' library."),
]

ALGORITHMS = {a.key: a for a in HASH_ALGORITHMS + ENCRYPT_ALGORITHMS}

PBKDF2_ITERATIONS = 600_000  # OWASP 2023 recommendation for PBKDF2-HMAC-SHA256
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s + "=" * (-len(s) % 4))


# ---- hashing ----------------------------------------------------------------


def hash_password(password: str, algo: str = "argon2id") -> str:
    pw = password.encode("utf-8")
    if algo == "bcrypt":
        import bcrypt

        if len(pw) > 72:
            raise ValueError("bcrypt only supports passwords up to 72 bytes. Use Argon2id for longer ones.")
        return bcrypt.hashpw(pw, bcrypt.gensalt(rounds=12)).decode()
    if algo == "argon2id":
        from argon2 import PasswordHasher

        return PasswordHasher().hash(password)
    if algo == "pbkdf2_sha256":
        salt = os.urandom(16)
        dk = hashlib.pbkdf2_hmac("sha256", pw, salt, PBKDF2_ITERATIONS)
        return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${_b64(salt)}${_b64(dk)}"
    if algo == "scrypt":
        salt = os.urandom(16)
        dk = hashlib.scrypt(pw, salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
        return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"
    raise ValueError(f"Unknown hash algorithm: {algo}")


def detect_hash(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    if value.startswith(("$2a$", "$2b$", "$2y$")) and len(value) == 60:
        return "bcrypt"
    if value.startswith("$argon2"):
        return "argon2id"
    if value.startswith("pbkdf2_sha256$") and value.count("$") == 3:
        return "pbkdf2_sha256"
    if value.startswith("scrypt$") and value.count("$") == 5:
        return "scrypt"
    return None


def verify_password(password: str, stored: str) -> bool:
    algo = detect_hash(stored)
    pw = password.encode("utf-8")
    try:
        if algo == "bcrypt":
            import bcrypt

            return bcrypt.checkpw(pw[:72], stored.encode())
        if algo == "argon2id":
            from argon2 import PasswordHasher
            from argon2.exceptions import VerificationError

            try:
                return PasswordHasher().verify(stored, password)
            except VerificationError:
                return False
        if algo == "pbkdf2_sha256":
            _, iters, salt, dk = stored.split("$")
            calc = hashlib.pbkdf2_hmac("sha256", pw, _unb64(salt), int(iters))
            return hmac.compare_digest(calc, _unb64(dk))
        if algo == "scrypt":
            _, n, r, p, salt, dk = stored.split("$")
            calc = hashlib.scrypt(pw, salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=len(_unb64(dk)))
            return hmac.compare_digest(calc, _unb64(dk))
    except (ValueError, TypeError):
        return False
    raise ValueError("That value isn't a recognised password hash.")


# ---- encryption --------------------------------------------------------------

ENC_PREFIX = "enc:v1:"


def new_key() -> bytes:
    """A fresh 256-bit key (Fernet uses half for signing and half for encrypting)."""
    return os.urandom(32)


def encrypt_value(plaintext: str, key: bytes, algo: str = "aes256gcm") -> str:
    data = plaintext.encode("utf-8")
    if algo == "aes256gcm":
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        payload = nonce + AESGCM(key).encrypt(nonce, data, None)
    elif algo == "chacha20":
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

        nonce = os.urandom(12)
        payload = nonce + ChaCha20Poly1305(key).encrypt(nonce, data, None)
    elif algo == "fernet":
        from cryptography.fernet import Fernet

        payload = Fernet(base64.urlsafe_b64encode(key)).encrypt(data)
    else:
        raise ValueError(f"Unknown encryption algorithm: {algo}")
    return f"{ENC_PREFIX}{algo}:{_b64(payload)}"


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(ENC_PREFIX)


def encrypted_algo(value: str) -> str | None:
    return value[len(ENC_PREFIX):].split(":", 1)[0] if is_encrypted(value) else None


def decrypt_value(token: str, key: bytes) -> str:
    if not is_encrypted(token):
        raise ValueError("Value is not encrypted.")
    algo, b64 = token[len(ENC_PREFIX):].split(":", 1)
    payload = _unb64(b64)
    try:
        if algo == "aes256gcm":
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            return AESGCM(key).decrypt(payload[:12], payload[12:], None).decode("utf-8")
        if algo == "chacha20":
            from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

            return ChaCha20Poly1305(key).decrypt(payload[:12], payload[12:], None).decode("utf-8")
        if algo == "fernet":
            from cryptography.fernet import Fernet

            return Fernet(base64.urlsafe_b64encode(key)).decrypt(payload).decode("utf-8")
    except Exception as exc:  # wrong key or tampered data
        raise ValueError("Could not decrypt: wrong key, or the value was modified.") from exc
    raise ValueError(f"Unknown encryption algorithm: {algo}")


# ---- master-password key derivation (for the IDE's own vault) ------------------


def derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**15, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
