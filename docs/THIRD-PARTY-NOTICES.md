# NeuroWave — inventory of third-party licenses

**Status:** preliminary compliance inventory for the MIT decision (ADR-0006). This is **not** a final audited binary distribution manifest. Actual licenses/versions of anything included in a release must be verified against its build artifacts.

NeuroWave's own source code is distributed under **MIT** (see [LICENSE](../LICENSE)). External software is distributed under **its own licenses**, not the NeuroWave MIT license.

| Component | Typical usage | Upstream / license | Delivery condition |
|---|---|---|---|
| **TinyTag** | optional Python local metadata reader, `[local]` | [tinytag](https://github.com/tinytag/tinytag) · MIT | include upstream notice as required when bundled |
| **yandex-music** | core Python client | [yandex-music](https://github.com/MarshalX/yandex-music-api) · LGPL-3.0 | verify installed distribution terms; ensure required notices/library-replacement rights |
| **PySide6-Essentials / Qt** | optional Qt Widgets UI | [Qt for Python](https://doc.qt.io/qtforpython-6/) · LGPL/commercial/GPL options | verify exact installed build and all relevant LGPL obligations |
| **mpv** | external executable, **not** Python linked | [mpv](https://mpv.io/) · GPL-2.0-or-later for typical distribution | when bundling binary, meet corresponding license/source obligations |
| **FFmpeg libraries used by mpv** | components of external mpv build | [FFmpeg](https://ffmpeg.org/legal.html) · depends on compiled modules/configuration | audit actual binary and enabled features |
| **dbus-next** | optional MPRIS | [dbus-next](https://github.com/altdesktop/python-dbus-next) · MIT | include applicable notice when bundled |
| **keyring** | optional secure token storage | [keyring](https://github.com/jaraco/keyring) · MIT | review transitive deps when bundled |
| **requests and transitive HTTP stack** | provider dependency | Apache-2.0 / MIT / MPL-2.0 may occur | verify actual lockfile / installed wheels |
| **setuptools / build tools** | packaging | varies | normally build-time; distinguish shipping vs build-only |

**Mutagen is no longer a declared or imported dependency** in the MIT migration PR. It must not be reintroduced for the official distribution without a documented compatibility/review decision.

## Release gates

1. Capture exact dependency versions and licenses from a clean built/installed wheel/sdist, including transitive dependencies, OS packages, plugin entry points, and optional extras.
2. Verify that wheel/sdist includes NeuroWave `LICENSE` and correct SPDX metadata; inspect generated `METADATA` and `RECORD`.
3. Check third-party attribution, reproduction of required notices, and availability of linked library/source as required by the specific license. This document's links alone may not satisfy package-specific notice requirements.
4. If bundling an mpv executable, its libraries or codecs, conduct a **separate binary-license audit**. Merely running mpv via IPC does not waive the obligation to respect mpv's GPL terms when distributing mpv.
5. If publishing only Python source/wheel requiring users to install system mpv separately, make the boundary explicit in release notes.
6. Verify contributor rights before claiming ownership of contributions not written by the named copyright holder.

**Avoid false certainty:** MIT on the NeuroWave source does not automatically prove a combined redistributed bundle is MIT-only, LGPL-compliant, GPL-compliant, or suitable for all markets. Public release approval is a separate explicit checklist item.
