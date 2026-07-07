"""Tests for release / CI tooling regexes (and other workflow contracts).

These tests guard the *publishing pipeline* itself — not the runtime
code. The publish pipeline lives in three places that must stay in
sync:

    .github/workflows/ci.yml                  (version coherence gate)
    .github/workflows/publish-corrigenda.yml   (HEAD-tag + version check)
    scripts/release-corrigenda.sh              (local release rehearsal)

Each one extracts ``corrigenda.__version__`` from ``__init__.py`` with
the same regex. If the regex drifts in one file, the others silently
fall behind — a class of bug that only surfaces at release time, when
the cost of a fix is highest.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Files that all use the same `__version__` extraction regex. If you
# add another consumer (eg. a new GitHub Action), append it here.
_VERSION_REGEX_FILES: list[Path] = [
    _REPO_ROOT / ".github" / "workflows" / "ci.yml",
    _REPO_ROOT / ".github" / "workflows" / "publish-corrigenda.yml",
    _REPO_ROOT / "scripts" / "release-corrigenda.sh",
]


def test_version_regex_matches_plain_and_type_annotated_forms():
    """Roadmap remediation B-NEW-2 — the regex used by the publish
    pipeline must succeed on BOTH the plain form

        __version__ = "X.Y.Z"

    and a future type-annotated form

        __version__: Final[str] = "X.Y.Z"

    The first iteration of the version-coherence gate (L7) used a
    naive ``r"__version__\\s*=\\s*..."`` regex that returned ``None`` on
    the annotated form, aborting CI mid-run with an opaque
    "__version__ not found" message. The fix added an optional
    ``(?::\\s*[^=]+)?`` clause; this test pins it so a careless
    "simplification" cannot reintroduce the bug.
    """
    canonical = r"__version__\s*(?::\s*[^=]+)?=\s*['\"]([^'\"]+)['\"]"

    cases = [
        ('__version__ = "0.1.0a1"', "0.1.0a1"),
        ("__version__ = '0.1.0a1'", "0.1.0a1"),
        ('__version__: str = "0.1.0a1"', "0.1.0a1"),
        ('__version__: Final[str] = "0.1.0a1"', "0.1.0a1"),
        ("__version__: str='0.1.0a1'", "0.1.0a1"),
    ]
    for source, expected in cases:
        m = re.search(canonical, source)
        assert m is not None, f"regex must match {source!r}"
        assert m.group(1) == expected, (
            f"regex matched but extracted wrong group on {source!r}: "
            f"got {m.group(1)!r}, expected {expected!r}"
        )


@pytest.mark.parametrize("path", _VERSION_REGEX_FILES, ids=lambda p: p.name)
def test_release_tooling_file_uses_annotation_tolerant_regex(path: Path):
    """Every file that extracts ``__version__`` must contain the
    sentinel ``(?::`` clause — its absence is the exact signature of
    the B-NEW-2 regression (see the companion test above for the why).

    Reading the YAML/shell as text is intentionally crude: a full
    YAML round-trip parser would mask the bug if a future maintainer
    moved the regex into a different block or quoted it differently.
    The grep-style check stays robust to those local edits.
    """
    assert path.exists(), f"expected release-tooling file is missing: {path}"
    text = path.read_text(encoding="utf-8")
    assert "__version__" in text, (
        f"{path.relative_to(_REPO_ROOT)} no longer references __version__ — "
        f"either remove it from _VERSION_REGEX_FILES or restore the regex."
    )
    assert "(?::" in text, (
        f"{path.relative_to(_REPO_ROOT)} version regex is missing the "
        f"annotation-tolerant '(?::' clause. The B-NEW-2 regression has "
        f"slipped back in: a `__version__: Final[str] = '...'` declaration "
        f"will no longer be parsed by this file's regex, silently aborting "
        f"the release pipeline."
    )


# ---------------------------------------------------------------------------
# L10/R4 + R6 — every GitHub Actions workflow must declare a
# `permissions:` block (minimum required, not default repo-wide) AND a
# `concurrency:` block (prevent concurrent runs from racing or wasting
# OIDC slots / clobbering the HF mirror). Both are pinned at the
# top-level of the workflow YAML.
# ---------------------------------------------------------------------------


_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"


def _workflow_files() -> list[Path]:
    return sorted(_WORKFLOWS_DIR.glob("*.yml")) + sorted(_WORKFLOWS_DIR.glob("*.yaml"))


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_workflow_declares_explicit_permissions_block(path: Path):
    """L10/R4 — every workflow must declare top-level `permissions:`.
    Without it, GitHub falls back to the repo-level default which can
    be `contents: write` (or worse). Explicit is mandatory.
    """
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data.get("permissions"), (
        f"{path.name} is missing a top-level `permissions:` block. "
        f"Add at minimum `permissions: {{ contents: read }}`."
    )


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_workflow_declares_concurrency_block(path: Path):
    """L10/R6 — every workflow must declare top-level `concurrency:`
    to prevent two simultaneous runs racing (CI rebuilds twice on a
    rapid push, two operators publishing concurrently fight on PyPI,
    two force-pushes to the HF mirror clobber each other).
    """
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data.get("concurrency"), (
        f"{path.name} is missing a top-level `concurrency:` block. "
        f"Add at minimum `concurrency: {{ group: <workflow>-${{{{ github.ref }}}}, "
        f"cancel-in-progress: <true|false> }}`."
    )
