"""Lightweight symmetric encryption for cached API keys.

Design choice: stay stdlib-only so the portable distribution does not have to
ship `cryptography`. We use AES-GCM via `pyca/cryptography` if it's installed
(preferred for authenticated encryption), else fall back to a PBKDF2-derived
key + HMAC-SHA256 envelope using `hashlib` / `hmac` / `os.urandom` — which are
stdlib.

Callers:
    store = SecretStore(Path(...))
    store.unlock("password")
    store.set("openai", "sk-…")
    token = store.get("openai")

If the user never sets a password (`unlock(None)`), the store operates in
plaintext mode and simply round-trips the JSON — keeping backwards
compatibility with the existing plain `local_llm_prefs.json`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
from pathlib import Path
from typing import Optional

from .errors import ConfigError
from .log_config import get_logger

_log = get_logger("vlv.secrets")


_MAGIC = b"VLV1"
_SALT_LEN = 16
_IV_LEN = 12
_PBKDF2_ITERS = 200_000


class SecretStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, str] = {}
        self._key: Optional[bytes] = None
        self._locked: bool = True

    # ---------- lifecycle ----------

    def unlock(self, password: Optional[str]) -> None:
        """Decrypt (or load plaintext) from disk. Call before read/write.
        Passing `None` opts out of encryption entirely."""
        if not self.path.is_file():
            self._data = {}
            self._key = self._derive_key(password) if password else None
            self._locked = False
            return
        raw = self.path.read_bytes()
        if raw.startswith(_MAGIC):
            if password is None:
                raise ConfigError(
                    "Secret store is encrypted but no password was supplied.",
                    hint="Provide the master password to decrypt saved API keys.",
                )
            self._key = self._derive_key(password, salt=raw[len(_MAGIC) : len(_MAGIC) + _SALT_LEN])
            self._data = self._decrypt(raw)
        else:
            # Legacy plaintext file: load as-is, offer to upgrade on next write.
            try:
                self._data = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._data = {}
            self._key = self._derive_key(password) if password else None
        self._locked = False

    def save(self) -> None:
        if self._locked:
            raise ConfigError("Secret store is locked; call unlock() first.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._key is not None:
            self.path.write_bytes(self._encrypt())
        else:
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if os.name != "nt":
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    # ---------- ops ----------

    def set(self, key: str, value: str) -> None:
        if self._locked:
            raise ConfigError("Secret store is locked.")
        self._data[key] = value

    def get(self, key: str) -> Optional[str]:
        if self._locked:
            raise ConfigError("Secret store is locked.")
        return self._data.get(key)

    def delete(self, key: str) -> None:
        if self._locked:
            raise ConfigError("Secret store is locked.")
        self._data.pop(key, None)

    def keys(self) -> list[str]:
        if self._locked:
            return []
        return list(self._data.keys())

    # ---------- internals ----------

    @staticmethod
    def _derive_key(password: str, *, salt: bytes | None = None) -> bytes:
        if salt is None:
            salt = secrets.token_bytes(_SALT_LEN)
        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS, dklen=32)
        return salt + key  # prepend salt so we can recover it on next open

    def _encrypt(self) -> bytes:
        assert self._key is not None
        salt, key = self._key[:_SALT_LEN], self._key[_SALT_LEN:]
        plaintext = json.dumps(self._data, ensure_ascii=False).encode("utf-8")
        # Try AES-GCM via cryptography if available, else HMAC-envelope fallback.
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            iv = secrets.token_bytes(_IV_LEN)
            ct = AESGCM(key).encrypt(iv, plaintext, None)
            return _MAGIC + salt + b"G" + iv + ct
        except Exception:
            return _MAGIC + salt + b"H" + self._hmac_envelope_encrypt(key, plaintext)

    def _decrypt(self, raw: bytes) -> dict:
        assert self._key is not None
        salt, key = self._key[:_SALT_LEN], self._key[_SALT_LEN:]
        body = raw[len(_MAGIC) + _SALT_LEN :]
        if not body:
            return {}
        scheme = body[:1]
        payload = body[1:]
        if scheme == b"G":
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM

                iv, ct = payload[:_IV_LEN], payload[_IV_LEN:]
                plaintext = AESGCM(key).decrypt(iv, ct, None)
            except Exception as e:
                raise ConfigError(f"Failed to decrypt secret store: {e}") from e
        elif scheme == b"H":
            plaintext = self._hmac_envelope_decrypt(key, payload)
        else:
            raise ConfigError(f"Unknown secret-store scheme: {scheme!r}")
        try:
            return json.loads(plaintext.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ConfigError("Secret store contains invalid JSON.") from e

    @staticmethod
    def _hmac_envelope_encrypt(key: bytes, plaintext: bytes) -> bytes:
        """XOR-stream with HMAC-SHA256(counter) + MAC. NOT AEAD-grade but
        adequate for local at-rest protection when `cryptography` is missing."""
        nonce = secrets.token_bytes(_IV_LEN)
        stream = _keystream(key, nonce, len(plaintext))
        ct = bytes(a ^ b for a, b in zip(plaintext, stream))
        mac = hmac.new(key, nonce + ct, hashlib.sha256).digest()
        return struct.pack(">I", len(ct)) + nonce + ct + mac

    @staticmethod
    def _hmac_envelope_decrypt(key: bytes, body: bytes) -> bytes:
        n = struct.unpack(">I", body[:4])[0]
        nonce = body[4 : 4 + _IV_LEN]
        ct = body[4 + _IV_LEN : 4 + _IV_LEN + n]
        mac = body[4 + _IV_LEN + n : 4 + _IV_LEN + n + 32]
        expected = hmac.new(key, nonce + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            raise ConfigError("Secret store MAC mismatch; file may be tampered.")
        stream = _keystream(key, nonce, n)
        return bytes(a ^ b for a, b in zip(ct, stream))


def _keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        block = hmac.new(
            key, nonce + counter.to_bytes(4, "big"), hashlib.sha256
        ).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:n])
