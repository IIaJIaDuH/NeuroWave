"""Хранение OAuth-токена.

Приоритет — системный Secret Service через ``keyring``. Если keyring
недоступен или отключён (``NEUROWAVE_KEYRING=off``), токен сохраняется в файл
с правами 0600 и приложение явно сообщает об этом. Токен никогда не попадает
в логи или отчёты.

Изолированный профиль (``NEUROWAVE_HOME`` / ``--home``) получает
собственное имя записи в keyring: иначе песочницы, тесты и разные профили
делили бы один токен и могли его перезаписать.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Mapping

from ..config import Paths

KEYRING_SERVICE = "neurowave"
DEFAULT_PROVIDER = "yandex"
KEYRING_USER = f"{DEFAULT_PROVIDER}-oauth"  # обратная совместимость

# Значения, которыми можно принудительно увести токены в файл.
DISABLED_VALUES = frozenset({"0", "off", "false", "no", "file", "none"})


def keyring_disabled(env: Mapping[str, str] | None = None) -> bool:
    """Отключён ли keyring через окружение (CI, тесты, нет Secret Service)."""
    env = os.environ if env is None else env
    return str(env.get("NEUROWAVE_KEYRING", "")).strip().lower() in DISABLED_VALUES


def profile_digest(config_dir: Path) -> str | None:
    """Короткий отпечаток изолированного профиля или ``None`` для основного."""
    if not os.environ.get("NEUROWAVE_HOME"):
        return None
    raw = str(Path(config_dir).expanduser()).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


class TokenStore:
    """Токен одного провайдера. У каждого провайдера своё место хранения.

    Яндекс использует исторические имена (``yandex-oauth`` и файл ``token``),
    чтобы уже выполненные входы не терялись; остальные — ``<provider>-oauth``
    и ``token.<provider>``.
    """

    def __init__(self, paths: Paths, provider: str = DEFAULT_PROVIDER):
        self._provider = provider
        self._user = self._keyring_user(paths, provider)
        if provider == DEFAULT_PROVIDER:
            self._file: Path = paths.token_file
        else:
            self._file = paths.data_dir / f"token.{provider}"
        self._keyring = self._import_keyring()

    @staticmethod
    def _keyring_user(paths: Paths, provider: str) -> str:
        """Имя записи в keyring: основное — историческое, профили — с отпечатком."""
        base = f"{provider}-oauth"
        digest = profile_digest(paths.config_dir)
        return f"{base}@{digest}" if digest else base

    @property
    def provider(self) -> str:
        return self._provider

    @staticmethod
    def _import_keyring():
        if keyring_disabled():
            return None
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
                self._keyring.set_password(KEYRING_SERVICE, self._user, token)
                self._remove_file()
                return
            except Exception:
                self._keyring = None
        self._write_file(token)

    def load(self) -> str | None:
        if self._keyring is not None:
            try:
                token = self._keyring.get_password(KEYRING_SERVICE, self._user)
                if token:
                    return token.strip()
            except Exception:
                pass
        return self._read_file()

    def delete(self) -> None:
        if self._keyring is not None:
            try:
                self._keyring.delete_password(KEYRING_SERVICE, self._user)
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
