from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


@dataclass(frozen=True)
class FieldCrypto:
    """Field-level encryption and keyed hashes.

    `SUBSHELF_ENCRYPTION_KEY` may be a Fernet key. For local development, a raw
    passphrase is also accepted and converted to a Fernet key with SHA-256.
    """

    key_material: bytes
    fernet: Fernet

    @classmethod
    def from_key(cls, key: str) -> "FieldCrypto":
        raw = key.encode("utf-8")
        try:
            Fernet(raw)
            fernet_key = raw
        except (ValueError, TypeError):
            fernet_key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
        return cls(key_material=raw, fernet=Fernet(fernet_key))

    def encrypt(self, value: object | None) -> str | None:
        if value is None:
            return None
        text = str(value)
        return self.fernet.encrypt(text.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str | None) -> str | None:
        if token is None:
            return None
        try:
            return self.fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Encrypted data could not be decrypted with the configured key") from exc

    def user_key_hash(self, telegram_user_id: int | str) -> str:
        return hmac.new(
            self.key_material,
            str(telegram_user_id).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()


if __name__ == "__main__":
    print(generate_key())
