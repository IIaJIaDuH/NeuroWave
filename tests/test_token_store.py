"""Хранение токенов: отключение keyring и изоляция профилей."""

from __future__ import annotations

from neurowave.config import Paths
from neurowave.storage.secrets import TokenStore, keyring_disabled


def test_keyring_can_be_disabled_from_environment(monkeypatch) -> None:
    """CI и машины без Secret Service не должны зависеть от keyring."""
    monkeypatch.setenv("NEUROWAVE_KEYRING", "off")
    monkeypatch.setitem(__import__("sys").modules, "keyring", None)

    assert keyring_disabled() is True
    assert TokenStore._import_keyring() is None


def test_disabled_keyring_falls_back_to_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEUROWAVE_KEYRING", "file")
    paths = Paths.resolve({"NEUROWAVE_HOME": str(tmp_path)}).ensure()
    store = TokenStore(paths)

    store.save("token-value")

    assert store.backend == "file"
    assert store.load() == "token-value"
    assert store.file_path.stat().st_mode & 0o777 == 0o600


def test_default_profile_keeps_legacy_keyring_name(monkeypatch) -> None:
    """У основного профиля имя записи прежнее: выполненные входы не теряются."""
    monkeypatch.delenv("NEUROWAVE_HOME", raising=False)

    assert TokenStore._keyring_user(Paths.resolve({}), "yandex") == "yandex-oauth"
    assert TokenStore._keyring_user(Paths.resolve({}), "spotify") == "spotify-oauth"


def test_isolated_profile_gets_own_keyring_entry(tmp_path, monkeypatch) -> None:
    """Песочница не должна читать и перезаписывать токен основного профиля."""
    first = Paths.resolve({"NEUROWAVE_HOME": str(tmp_path / "one")})
    second = Paths.resolve({"NEUROWAVE_HOME": str(tmp_path / "two")})
    monkeypatch.setenv("NEUROWAVE_HOME", str(tmp_path / "one"))
    user_one = TokenStore._keyring_user(first, "yandex")
    user_one_again = TokenStore._keyring_user(first, "yandex")

    monkeypatch.setenv("NEUROWAVE_HOME", str(tmp_path / "two"))
    user_two = TokenStore._keyring_user(second, "yandex")

    assert user_one != user_two
    assert user_one == user_one_again  # стабильно между запусками
    assert user_one not in {"yandex-oauth", user_two}
    assert user_one.startswith("yandex-oauth@")


def test_sandbox_logout_keeps_main_profile_token(tmp_path, monkeypatch) -> None:
    """Logout в песочнице не должен удалять токен основного профиля.

    Раньше имя записи в keyring не зависело от --home, поэтому прогон тестов
    разлогинивал настоящий аккаунт.
    """
    calls: list[tuple[str, str]] = []

    class FakeKeyring:
        def get_keyring(self):
            return object()

        def get_password(self, service, user):
            return None

        def set_password(self, service, user, value):
            calls.append(("set", user))

        def delete_password(self, service, user):
            calls.append(("delete", user))

    monkeypatch.setenv("NEUROWAVE_HOME", str(tmp_path / "sandbox"))
    monkeypatch.setattr(TokenStore, "_import_keyring", staticmethod(FakeKeyring))
    paths = Paths.resolve({"NEUROWAVE_HOME": str(tmp_path / "sandbox")}).ensure()

    TokenStore(paths).delete()

    assert calls, "keyring не вызывался вовсе"
    assert all(user != "yandex-oauth" for _, user in calls), calls
    assert calls[0][1].startswith("yandex-oauth@")
