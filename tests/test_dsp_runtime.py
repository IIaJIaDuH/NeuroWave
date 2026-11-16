"""Рантайм DSP: применение, сверка с фактическим состоянием и откат.

Проверки нацелены на главный риск из ADR-0005: интерфейс не должен показывать
«Enhanced включён», если mpv фильтр не принял. Поэтому часть тестов имитирует
отказ и подмену ответа, а не только успешный путь.
"""

from __future__ import annotations

import json

import pytest

from neurowave.audio import NullBackend
from neurowave.audio.dsp_controller import DspController
from neurowave.audio.dsp_profiles import BUILTIN_PROFILES, DspMode
from neurowave.audio.mpv import MpvBackend
from neurowave.cli import _handle_key, main
from neurowave.config import ConfigStore, Paths, Settings
from neurowave.player import PlayerService
from tests.conftest import FakeAdapter, make_track


def _mpv(fake_mpv, tmp_path, plan: str) -> MpvBackend:
    return MpvBackend(
        binary=str(fake_mpv),
        socket_path=tmp_path / f"mpv-{plan}.sock",
        extra_args=[f"--fake-plan={plan}"],
    )


class SpyBackend(NullBackend):
    """Считает применения фильтра: sync() не должен спамить IPC."""

    def __init__(self) -> None:
        super().__init__()
        self.pushes: list[str] = []

    def set_audio_filter(self, graph: str) -> None:
        self.pushes.append(graph)
        super().set_audio_filter(graph)


class LyingBackend(NullBackend):
    """Принимает команду, но держит в цепи совсем другой граф."""

    def set_audio_filter(self, graph: str) -> None:
        self.calls.append(("af", graph))

    def audio_filter(self) -> str | None:
        return "equalizer=f=999:g=1"


def test_apply_enhanced_confirms_graph(fake_mpv, tmp_path) -> None:
    backend = _mpv(fake_mpv, tmp_path, "idle")
    events: list[tuple[str, dict]] = []
    settings = Settings(dsp_profile="flat")
    controller = DspController(backend, settings=settings, listener=lambda e, p: events.append((e, p)))
    try:
        backend.load("fake://track")
        status = controller.apply("bluetooth")
        assert status.confirmed is True and status.fallback is False
        assert status.graph and "alimiter" in status.graph
        assert status.mode is DspMode.ENHANCED
        assert settings.dsp_profile == "bluetooth"
        assert backend.audio_filter() == status.graph[len("lavfi=[") : -1]
        assert events[-1][0] == "dsp_applied"
    finally:
        backend.shutdown()


def test_pure_clears_filters_but_remembers_choice(fake_mpv, tmp_path) -> None:
    backend = _mpv(fake_mpv, tmp_path, "idle")
    settings = Settings(dsp_profile="night")
    controller = DspController(backend, settings=settings)
    try:
        backend.load("fake://track")
        controller.apply("night")
        assert backend.audio_filter() is not None
        status = controller.set_mode("pure")
        assert status.mode is DspMode.PURE and status.confirmed is True
        assert backend.audio_filter() is None
        back = controller.set_mode("enhanced")
        assert back.mode is DspMode.ENHANCED and back.profile.id == "night"
    finally:
        backend.shutdown()


def test_refusal_rolls_back_to_previous_profile(fake_mpv, tmp_path) -> None:
    """mpv отказал в новом графе — прежний профиль должен вернуться."""
    backend = _mpv(fake_mpv, tmp_path, "deny-af-graph")
    settings = Settings(dsp_profile="flat")
    controller = DspController(backend, settings=settings)
    try:
        backend.load("fake://track")
        status = controller.apply("bluetooth")
        assert status.fallback is True
        assert status.confirmed is True  # откат удался
        assert status.profile is not None and status.profile.id == "flat"
        assert status.requested == "bluetooth"
        assert "af" in (status.reason or "")
        assert settings.dsp_profile == "flat", "сохранённый выбор не должен меняться при отказе"
        assert backend.audio_filter() is None
    finally:
        backend.shutdown()


def test_failed_rollback_is_reported_as_unconfirmed(fake_mpv, tmp_path) -> None:
    """Даже снять фильтры не удалось — нельзя делать вид, что всё хорошо."""
    backend = _mpv(fake_mpv, tmp_path, "deny-af")
    controller = DspController(backend, settings=Settings(dsp_profile="flat"))
    try:
        backend.load("fake://track")
        status = controller.apply("hifi")
        assert status.confirmed is False
        assert status.graph is None
        assert "откат не удался" in (status.reason or "")
        assert controller.verified is False
    finally:
        backend.shutdown()


def test_mismatched_readback_is_detected() -> None:
    controller = DspController(LyingBackend(), settings=Settings())
    status = controller.apply("hifi")
    assert status.fallback is True and status.confirmed is False
    assert "другой граф" in (status.reason or "")


def test_sync_is_idempotent() -> None:
    backend = SpyBackend()
    controller = DspController(backend, settings=Settings(dsp_profile="bluetooth"))
    controller.sync()
    controller.sync()
    controller.sync()
    assert len(backend.pushes) == 1


def test_sync_retries_after_mpv_restart() -> None:
    backend = SpyBackend()
    controller = DspController(backend, settings=Settings(dsp_profile="bluetooth"))
    controller.sync()
    controller.invalidate()  # mpv перезапущен, фильтров в нём нет
    controller.sync()
    assert len(backend.pushes) == 2


def test_player_applies_saved_profile_on_first_play(fake_mpv, tmp_path) -> None:
    backend = _mpv(fake_mpv, tmp_path, "idle")
    player = PlayerService(
        adapter=FakeAdapter(),
        backend=backend,
        settings=Settings(dsp_profile="night"),
        prefetch=False,
    )
    try:
        player.play_tracks([make_track(1)])
        assert player.dsp.verified is True
        assert "alimiter" in (backend.audio_filter() or "")
        assert backend.audio_filter().startswith("volume=volume=-6dB")
    finally:
        player.close()


def test_player_applies_profile_changed_while_playing(fake_mpv, tmp_path) -> None:
    backend = _mpv(fake_mpv, tmp_path, "idle")
    player = PlayerService(
        adapter=FakeAdapter(), backend=backend, settings=Settings(dsp_profile="flat"), prefetch=False
    )
    try:
        player.play_tracks([make_track(1)])
        status = player.set_dsp_profile("hifi")
        assert status.confirmed is True
        assert "alimiter" in (backend.audio_filter() or "")
        assert player.settings.dsp_profile == "hifi"
    finally:
        player.close()


class NotRunningBackend(NullBackend):
    """mpv ещё не запущен: выбирать профиль можно, применять пока некуда."""

    is_running = False


def test_profile_selection_does_not_start_mpv() -> None:
    backend = NotRunningBackend()
    player = PlayerService(
        adapter=FakeAdapter(), backend=backend, settings=Settings(dsp_profile="flat"), prefetch=False
    )
    status = player.set_dsp_profile("bluetooth")
    assert status.confirmed is False
    assert backend.audio_filter_value is None  # mpv не трогали
    assert player.settings.dsp_profile == "bluetooth"


def test_close_invalidates_dsp_state(fake_mpv, tmp_path) -> None:
    backend = _mpv(fake_mpv, tmp_path, "idle")
    player = PlayerService(
        adapter=FakeAdapter(), backend=backend, settings=Settings(dsp_profile="hifi"), prefetch=False
    )
    player.play_tracks([make_track(1)])
    assert player.dsp.verified is True
    player.close()
    assert player.dsp.verified is False


def test_mode_toggle_can_be_driven_from_keyboard(fake_mpv, tmp_path) -> None:
    backend = _mpv(fake_mpv, tmp_path, "idle")
    player = PlayerService(
        adapter=FakeAdapter(), backend=backend, settings=Settings(dsp_profile="hifi"), prefetch=False
    )
    try:
        player.play_tracks([make_track(1)])
        assert _handle_key(player, "e") is True
        assert player.dsp.status.mode is DspMode.PURE
        assert backend.audio_filter() is None
        assert _handle_key(player, "e") is True
        assert player.dsp.status.mode is DspMode.ENHANCED
    finally:
        player.close()


def test_cli_sound_selects_and_reports_profile(tmp_path, capsys) -> None:
    assert main(["--home", str(tmp_path), "sound", "--profile", "night"]) == 0
    assert ConfigStore(Paths.resolve({"NEUROWAVE_HOME": str(tmp_path)})).load().dsp_profile == "night"
    capsys.readouterr()

    assert main(["--home", str(tmp_path), "--json", "sound"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["profile"] == "night" and data["limiter"]["ceiling_dbfs"] == -2.0
    assert data["graph"].startswith("lavfi=[") and "alimiter" in data["graph"]


def test_cli_sound_rejects_unknown_profile(tmp_path, capsys) -> None:
    assert main(["--home", str(tmp_path), "sound", "--profile", "nope"]) == 2
    assert "Неизвестный профиль" in capsys.readouterr().err


def test_cli_pure_and_enhanced_switch_without_losing_choice(tmp_path, capsys) -> None:
    paths = Paths.resolve({"NEUROWAVE_HOME": str(tmp_path)})
    assert main(["--home", str(tmp_path), "sound", "--profile", "bluetooth"]) == 0
    assert main(["--home", str(tmp_path), "sound", "--pure"]) == 0
    assert ConfigStore(paths).load().dsp_profile == "flat"
    # --enhanced после --pure не должен оставаться в Pure
    assert main(["--home", str(tmp_path), "sound", "--enhanced"]) == 0
    assert ConfigStore(paths).load().dsp_profile == "hifi"
    capsys.readouterr()


@pytest.mark.parametrize("profile_id", sorted(BUILTIN_PROFILES))
def test_every_builtin_applies_to_double(fake_mpv, tmp_path, profile_id: str) -> None:
    backend = _mpv(fake_mpv, tmp_path, "idle")
    controller = DspController(backend, settings=Settings())
    try:
        backend.load("fake://track")
        status = controller.apply(profile_id)
        assert status.confirmed is True, profile_id
        if BUILTIN_PROFILES[profile_id].mode is DspMode.PURE:
            assert backend.audio_filter() is None
        else:
            assert backend.audio_filter() is not None
    finally:
        backend.shutdown()
