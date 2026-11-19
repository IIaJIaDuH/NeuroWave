"""QA-04: сеть и Яндекс Музыка.

Разрешённой тестовой авторизации нет (в профиле нет токена, keyring в
системе не установлен), поэтому сценарии сети воспроизводятся через
контролируемые заглушки транспорта — как и предписано в ТЗ. Системная сеть
не отключается.

Проверяем: задержку ответа, timeout, отсутствие сети, обрыв соединения во
время работы, ошибку сервиса, недействительный токен, а также то, что после
отказа провайдера проигрывается локальный трек и плеер не уходит в
бесконечные повторы.

Запуск: python qa/qa04_network.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402

from yandex_music import exceptions as ym  # noqa: E402

from neurowave.yandex.adapter import YandexMusicAdapter  # noqa: E402


class FakeClient:
    """Клиент-заглушка: задержка, обрыв, ошибки — по сценарию."""

    def __init__(self, behaviour: str, delay: float = 0.0, fail_after: int | None = None):
        self.behaviour = behaviour
        self.delay = delay
        self.fail_after = fail_after
        self.calls = 0

    def _maybe_fail(self) -> None:
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise ym.NetworkError("соединение оборвано")
        if self.behaviour == "timeout":
            raise ym.TimedOutError()
        if self.behaviour == "network":
            raise ym.NetworkError("сеть недоступна")
        if self.behaviour == "service":
            raise ym.YandexMusicError("внутренняя ошибка сервиса")
        if self.behaviour == "unauthorized":
            raise ym.UnauthorizedError("токен недействителен")
        if self.behaviour == "notfound":
            raise ym.NotFoundError()
        if self.delay:
            time.sleep(self.delay)

    def search(self, *args: Any, **kwargs: Any) -> Any:
        self._maybe_fail()
        return type("Result", (), {"tracks": None, "artists": None, "albums": None,
                                   "playlists": None})()

    @property
    def me(self) -> Any:
        self._maybe_fail()
        return type("Me", (), {"account": None})()

    def account_status(self) -> Any:
        return type("Status", (), {"plus": False, "product": None})()

    def rotor_station_tracks(self, *args: Any, **kwargs: Any) -> Any:
        self._maybe_fail()
        return type("Rotor", (), {"batch_id": "qa-batch", "sequence": []})()

    def rotor_station_feedback(self, *args: Any, **kwargs: Any) -> Any:
        return True


def run_case(label: str, adapter: YandexMusicAdapter, action: str) -> dict:
    started = time.monotonic()
    out: dict = {"case": label, "action": action}
    try:
        if action == "search":
            result = adapter.search("тон")
            out["ok"] = True
            out["tracks"] = len(result.tracks)
        elif action == "account":
            info = adapter.account()
            out["ok"] = True
            out["has_uid"] = bool(info.uid)
        elif action == "wave":
            tracks = adapter.wave_tracks()
            out["ok"] = True
            out["tracks"] = len(tracks)
        out["seconds"] = round(time.monotonic() - started, 2)
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["seconds"] = round(time.monotonic() - started, 2)
    return out


def section_stubs() -> list[dict]:
    """Негативные сценарии на заглушках."""
    cases: list[dict] = []
    scenarios = [
        ("задержка ответа 3 с", FakeClient("ok", delay=3.0), "search"),
        ("timeout от сервиса", FakeClient("timeout"), "search"),
        ("сеть недоступна", FakeClient("network"), "search"),
        ("ошибка сервиса", FakeClient("service"), "search"),
        ("недействительный токен", FakeClient("unauthorized"), "search"),
        ("не найдено", FakeClient("notfound"), "search"),
        ("обрыв после первой попытки", FakeClient("network", fail_after=0), "wave"),
        ("обрыв в account()", FakeClient("network", fail_after=0), "account"),
    ]
    for label, client, action in scenarios:
        adapter = YandexMusicAdapter(token="qa-fake-token", client=client, retries=3, backoff=0.3)
        cases.append(run_case(label, adapter, action))
    return cases


def section_local_after_failure() -> dict:
    """После отказа провайдера должен проигрываться локальный трек."""
    from neurowave.domain import Track, Quality, StreamInfo
    from neurowave.player import PlayerService
    from neurowave.errors import TrackUnavailable

    class FailingAdapter:
        provider = "yandex"
        token = "qa-fake-token"

        def __init__(self, good: Track):
            self.good = good

        def wave_tracks(self) -> list[Track]:
            return []

        def stream_url(self, track_id: str) -> StreamInfo:
            if track_id == "broken":
                raise TrackUnavailable("трек недоступен")
            return self.good_url

        def wave_feedback(self, *args: Any, **kwargs: Any) -> None:
            return None

    good = Track(id="local-ok", title="QA local", provider="local", duration_ms=20000)
    adapter = FailingAdapter(good)
    adapter.good_url = StreamInfo(url=str(q.MEDIA / "qa_tone_b220_short.mp3"),
                                  quality=Quality(codec="mp3"))
    harness = q.build_local_player("qa04_local", repeat="off", volume=30)
    harness.player.adapter = adapter
    out: dict = {}
    try:
        broken = Track(id="broken", title="broken", provider="yandex", duration_ms=20000)

        # Путь A: очередь начинается с недоступного трека.
        out["case_a_start_with_unavailable"] = {}
        try:
            harness.player.play_tracks([broken, good], start=0)
            out["case_a_start_with_unavailable"]["raised"] = False
        except Exception as exc:  # noqa: BLE001
            out["case_a_start_with_unavailable"]["raised"] = True
            out["case_a_start_with_unavailable"]["error"] = f"{type(exc).__name__}: {exc}"
        time.sleep(1.5)
        out["case_a_start_with_unavailable"]["current"] = (
            harness.player.current_track.id if harness.player.current_track else None)
        out["case_a_start_with_unavailable"]["status"] = harness.player.state.status.value
        out["case_a_start_with_unavailable"]["local_played"] = (
            out["case_a_start_with_unavailable"]["current"] == "local-ok")

        # Путь B: переход на недоступный трек в середине очереди.
        good2 = Track(id="local-ok-2", title="QA local 2", provider="local", duration_ms=20000)
        out["case_b_transition_to_unavailable"] = {}
        harness.player.play_tracks([good, broken, good2], start=0)
        time.sleep(1.5)
        before = harness.recorder.count("track")
        harness.player.next_track()
        time.sleep(2.0)
        out["case_b_transition_to_unavailable"]["current"] = (
            harness.player.current_track.id if harness.player.current_track else None)
        out["case_b_transition_to_unavailable"]["status"] = harness.player.state.status.value
        out["case_b_transition_to_unavailable"]["track_events"] = (
            harness.recorder.count("track") - before)
        out["case_b_transition_to_unavailable"]["errors"] = [
            e for e in harness.recorder.as_list() if e["event"] in {"error", "failed"}][-3:]

        out["mpv_alive"] = harness.backend.is_running
        out["error_events_total"] = sum(1 for e in harness.recorder.as_list() if e["event"] == "error")
        time.sleep(2.0)
        out["error_events_after_2s"] = sum(
            1 for e in harness.recorder.as_list() if e["event"] == "error")
        out["no_infinite_retry"] = out["error_events_after_2s"] - out["error_events_total"] <= 1
    finally:
        harness.close()
    return out


def section_offline_start() -> dict:
    """Старт без сети: провайдер недоступен, локальный провайдер работает."""
    out: dict = {}
    home = q.qa_home("qa04_offline")
    env = {"NEUROWAVE_HOME": str(home),
           "PATH": f"{q.MPV.parent}:{__import__('os').environ['PATH']}",
           "PYTHONPATH": str(q.ROOT / "src")}
    adapter = YandexMusicAdapter(token="qa-fake-token", client=FakeClient("network"), retries=2, backoff=0.2)
    out["adapter_network_error"] = run_case("сеть недоступна при старте", adapter, "account")
    # локальный провайдер в том же профиле
    code, stdout, stderr = q.run(
        [str(q.ROOT / ".venv" / "bin" / "neurowave"), "--provider", "local",
         "search", "tone"], timeout=60, env=env)
    out["local_provider_offline"] = {"code": code, "stdout": stdout.strip()[:300],
                                     "stderr": stderr.strip()[:300]}
    return out


def main() -> int:
    q.ensure_dirs()
    result = {
        "scenario": "QA-04 сеть и Яндекс Музыка",
        "real_account": {
            "status": "NOT AVAILABLE",
            "reason": "разрешённая тестовая авторизация не предоставлена: "
                       "в профиле нет токена, keyring в системе не установлен",
        },
        "stub_cases": section_stubs(),
        "local_after_failure": section_local_after_failure(),
        "offline_start": section_offline_start(),
    }
    path = q.write_result("qa04_network", result)
    print(json.dumps(result, ensure_ascii=False, indent=2)[:12000])
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
