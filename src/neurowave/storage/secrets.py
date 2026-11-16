"""Хранение OAuth-токена.

Приоритет — системный Secret Service через ``keyring``. Если keyring
недоступен, токен сохраняется в файл с правами 0600 и приложение явно
сообщает об этом. Токен никогда не попадает в логи или отчёты.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..config import Paths

KEYRING_SERVICE = "neurowave"
KEYRING_USER = "yandex-oauth"


class TokenStore:
    def __init__(self, paths: Paths):
        self._file: Path = paths.token_file
        self._keyring = self._import_keyring()

    @staticmethod
    def _import_keyring():
        try:
            import keyring  # type: ignore

            keyring.get_keyring()
            return keyring
        except Exception:  # keyring отсутствует или нет backend
            return None

    @property
    def backend(self) -> str:
        return "keyring" if self._keyring is not None else "file"

    @property
    def file_path(self) -> Path:
        return self._file

    def save(self, token: str) -> None:
        token = (token or "").strip()
        if not token:
            raise ValueError("empty token")
        if self._keyring is not None:
            try:
                self._keyring.set_password(KEYRING_SERVICE, KEYRING_USER, token)
                self._remove_file()
                return
            except Exception:
                self._keyring = None
        self._write_file(token)

    def load(self) -> str | None:
        if self._keyring is not None:
            try:
                token = self._keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
                if token:
                    return token.strip()
            except Exception:
                pass
        return self._read_file()

    def delete(self) -> None:
        if self._keyring is not None:
            try:
                self._keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
            except Exception:
                pass
        self._remove_file()

    def present(self) -> bool:
        return bool(self.load())

    # --- файловый fallback -------------------------------------------
    def _write_file(self, token: str) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(token)
        finally:
            try:
                os.chmod(self._file, 0o600)
            except OSError:
                pass

    def _read_file(self) -> str | None:
        try:
            token = self._file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return token or None

    def _remove_file(self) -> None:
        try:
            self._file.unlink()
        except OSError:
            pass
