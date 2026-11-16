"""Политика громкости: без проверенного лимитера выше 100 % не применяем.

Смысл тестов — не «код что-то вернул», а два обещания:

* усиление выше безопасного предела не попадает в звук, даже если в
  настройках или MPRIS запрошено 130 %;
* пользователь не остаётся думать, что громкость просто не работает: причина
  сообщается один раз, а запрошенное значение не теряется.
"""

from __future__ import annotations

import math

import pytest

from neurowave.audio import NullBackend
from neurowave.audio.mpv import MpvBackend, _volume_value
from neurowave.config import MAX_VOLUME, SAFE_VOLUME, Settings
from neurowave.errors import PlayerError
from neurowave.integrations.mpris import PlayerTarget
from neurowave.player import PlayerService
from tests.conftest import FakeAdapter, make_track


def _service(**settings) -> tuple[PlayerService, NullBackend, list[tuple[str, dict]]]:
    backend = NullBackend()
    events: list[tuple[str, dict]] = []
    service = PlayerService(
        adapter=FakeAdapter(),
        backend=backend,
        settings=Settings(**settings),
        listener=lambda name, payload: events.append((name, payload)),
        prefetch=False,
    )
    return service, backend, events


def test_settings_cap_boost_until_limiter_is_verified() -> None:
    assert Settings(volume=130).effective_volume() == SAFE_VOLUME
    assert Settings(volume=130, boost_verified=True).effective_volume() == 130
    assert Settings(volume=70).effective_volume() == 70


def test_settings_report_limited_state_and_keep_request() -> None:
    settings = Settings(volume=130)
    assert settings.volume == 130
    assert settings.volume_limited() is True
    settings.boost_verified = True
    assert settings.volume_limited() is False


def test_service_applies_safe_volume_and_warns_once() -> None:
    service, backend, events = _service(volume=100)
    applied = service.set_volume(130)

    assert applied == SAFE_VOLUME
    assert backend.volume == SAFE_VOLUME  # в звук ушло 100 %, а не 130 %
    notices = [e for e in events if e[0] == "volume_limited"]
    assert len(notices) == 1
    assert notices[0][1]["requested"] == 130
    assert notices[0][1]["applied"] == SAFE_VOLUME

    service.set_volume(120)  # повторное ограничение не спамит
    assert len([e for e in events if e[0] == "volume_limited"]) == 1


def test_service_persists_requested_volume_not_applied(monkeypatch) -> None:
    service, _, _ = _service(volume=100)
    saved: list[tuple[str, object]] = []

    class Store:
        def set_setting(self, key: str, value: object) -> None:
            saved.append((key, value))

    service.store = Store()  # type: ignore[assignment]
    service.set_volume(130)

    assert saved == [("volume", 130)]
    assert service.settings.volume == 130  # желание пользователя сохранено
    assert service.applied_volume == SAFE_VOLUME


def test_verified_limiter_unlocks_boost() -> None:
    service, backend, events = _service(volume=100, boost_verified=True)
    applied = service.set_volume(MAX_VOLUME)

    assert applied == MAX_VOLUME
    assert backend.volume == MAX_VOLUME
    assert not [e for e in events if e[0] == "volume_limited"]


def test_start_applies_safe_volume_and_explains_why() -> None:
    service, backend, events = _service(volume=130)
    service.play_tracks([make_track(1)])

    assert backend.volume == SAFE_VOLUME
    notices = [e for e in events if e[0] == "volume_limited"]
    assert len(notices) == 1 and notices[0][1]["limit"] == SAFE_VOLUME


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "loud", None, True])
def test_invalid_gain_is_rejected_before_ipc(value) -> None:
    # NaN в mpv вешает процесс: на живой проверке ответ на `volume NaN`
    # не пришёл вообще. Поэтому отказ обязан происходить до отправки.
    with pytest.raises(PlayerError):
        _volume_value(value)


def test_gain_is_clamped_to_scale_upper_bound() -> None:
    assert _volume_value(10_000) == MAX_VOLUME
    assert _volume_value(-5) == 0
    assert _volume_value(99.6) == 100


def test_backend_rejects_invalid_gain_without_starting_mpv(tmp_path) -> None:
    backend = MpvBackend(binary="/nonexistent/mpv", socket_path=tmp_path / "no.sock")
    with pytest.raises(PlayerError):
        backend.set_volume(math.nan)
    assert backend.is_running is False  # mpv даже не пытались запустить


def test_mpris_reports_applied_volume_not_request() -> None:
    service, _, _ = _service(volume=130)
    snapshot = PlayerTarget(service).snapshot()
    assert snapshot.volume_percent == SAFE_VOLUME


def test_audio_filter_normalizes_real_mpv_shapes() -> None:
    """Формы ответа `af` у настоящего mpv 0.42 отличаются от списка строк."""
    from neurowave.audio.mpv import _filter_text

    single = {"name": "lavfi", "enabled": True, "params": {"graph": "equalizer=f=1000:g=6"}}
    assert _filter_text(single) == "equalizer=f=1000:g=6"
    assert _filter_text([single]) == "equalizer=f=1000:g=6"
    assert _filter_text(["lavfi=[anull]"]) == "lavfi=[anull]"
    assert _filter_text([]) is None
    assert _filter_text(None) is None
