"""Шифрование сохранённых сессий. Используем SessionCrypto из pymirea (Fernet + HKDF,
с ротацией ключей), а не собственную криптографию."""

from __future__ import annotations

from .moodle.client import StoredCookie


class Vault:
    def __init__(self, secret_keys: str):
        from pymirea import SessionCrypto

        self._crypto = SessionCrypto(secret_keys)

    def seal(self, cookies: list[StoredCookie]) -> str:
        return self._crypto.encrypt_session({"v": 1, "cookies": [c.to_dict() for c in cookies]})

    def open(self, blob: str | None) -> list[StoredCookie] | None:
        """None — расшифровать не удалось (сменили ключ или данные повреждены)."""
        if not blob:
            return None
        data = self._crypto.decrypt_session(blob)
        if not data or "cookies" not in data:
            return None
        return [StoredCookie.from_dict(c) for c in data["cookies"]]
