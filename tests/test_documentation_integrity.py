"""Keep CURRENT project docs aligned with code and relative links valid.

Historical reports intentionally excluded: their statements were true at
their original snapshot dates and must not be rewritten to look current.
This test does not claim that GUI or hardware smoke has passed.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]

CURRENT_DOCS = (
    "README.md",
    "docs/ROADMAP.md",
    "docs/ARCHITECTURE.md",
    "docs/IMPLEMENTATION.md",
    "docs/BETA-CHECKLIST.md",
    "docs/STABILIZATION-PLAN-2026-11-17.md",
    "docs/AGENT-WORK-ORDERS-2026-11-17.md",
    "docs/REVIEW-GATES.md",
)
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


@pytest.mark.parametrize("document", CURRENT_DOCS)
def test_current_documentation_relative_links_resolve(document: str) -> None:
    source = ROOT / document
    text = source.read_text(encoding="utf-8")
    missing: list[str] = []

    for raw in MARKDOWN_LINK.findall(text):
        # External, mailto, or in-page links are not local files.
        url = urlsplit(raw.split(None, 1)[0])
        if url.scheme or url.netloc or raw.startswith("#"):
            continue
        name = unquote(url.path)
        if not name:
            continue
        target = (source.parent / name).resolve()
        # Don't accidentally validate links outside this checkout.
        if not target.is_relative_to(ROOT) or not target.is_file():
            missing.append(f"{raw} -> {target}")

    assert not missing, f"Broken links in {document}:\n" + "\n".join(missing)


def test_readme_reflects_existing_qt_and_soundlab() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "GUI, DSP и ИИ ещё не реализованы" not in readme
    assert "6-часовой soak (идёт)" not in readme
    assert "Qt Widgets" in readme
    assert "Sound Lab" in readme
    assert "neurowave app" in readme
    assert "AI" in readme or "ИИ" in readme
    assert (ROOT / "src/neurowave/ui/app.py").is_file()
    assert (ROOT / "src/neurowave/audio/dsp_controller.py").is_file()


def test_roadmap_does_not_reintroduce_old_license_and_comparison_claims() -> None:
    roadmap = (ROOT / "docs/ROADMAP.md").read_text(encoding="utf-8")
    assert "пока не выбрана лицензия" not in roadmap
    assert "не закрыт полностью: открыт пункт сравнения" not in roadmap
    assert "физический HDMI" in roadmap
    assert "стабилизац" in roadmap.lower()


def test_beta_checklist_does_not_call_swap_affected_memory_proven_clean() -> None:
    beta = (ROOT / "docs/BETA-CHECKLIST.md").read_text(encoding="utf-8")
    assert "MEMORY" in beta or "памяти" in beta
    assert "swap" in beta.lower()
    assert "fd 113" in beta
