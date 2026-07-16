"""Mechanics of the external-corpus fetcher (no network involved).

The fetch script is the gate that decides whether the corpus job tests
REAL bytes: its exit code and integrity checks are contracts. Historic
behaviour returned success as soon as ONE page of the whole manifest was
fetched, and never verified what the cache actually contained.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

_FETCH_PATH = Path(__file__).parent / "external_corpus" / "fetch.py"


@pytest.fixture()
def fetch_mod(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("corpus_fetch", _FETCH_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "HERE", tmp_path)
    monkeypatch.setattr(mod, "CACHE", tmp_path / ".cache")
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    return mod


def _write_manifest(tmp_path: Path, documents: list[dict], max_missing: int) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps({"max_missing": max_missing, "documents": documents}),
        encoding="utf-8",
    )


_ALTO = b'<?xml version="1.0"?><alto xmlns="http://www.loc.gov/standards/alto/ns-v3#"/>'
_ALTO_V2 = _ALTO + b"<!-- re-OCR -->"


def test_exit_nonzero_when_missing_exceeds_tolerance(fetch_mod, tmp_path):
    """1 page out of 3 fetched with a tolerance of 1 missing is a FAILURE
    — the historic script declared success whenever fetched > 0."""
    _write_manifest(
        tmp_path,
        [{"ark": "bpt6kTEST", "label": "t", "pages": [1, 2, 3]}],
        max_missing=1,
    )
    calls = {"n": 0}

    def one_success(ark, page):
        calls["n"] += 1
        return _ALTO if calls["n"] == 1 else None

    fetch_mod._download = one_success
    assert fetch_mod.main() == 1


def test_missing_within_tolerance_passes(fetch_mod, tmp_path):
    _write_manifest(
        tmp_path,
        [{"ark": "bpt6kTEST", "label": "t", "pages": [1, 2]}],
        max_missing=1,
    )
    calls = {"n": 0}

    def one_success(ark, page):
        calls["n"] += 1
        return _ALTO if calls["n"] == 1 else None

    fetch_mod._download = one_success
    assert fetch_mod.main() == 0


def test_pinned_drift_fails_and_discards_the_file(fetch_mod, tmp_path, capsys):
    """A pinned page whose bytes changed (Gallica re-OCR) must alert and
    leave NO drifted file behind for the tests to silently consume."""
    pin = hashlib.sha256(_ALTO).hexdigest()
    _write_manifest(
        tmp_path,
        [{"ark": "bpt6kTEST", "label": "t", "pages": [1], "sha256": {"1": pin}}],
        max_missing=0,
    )
    fetch_mod._download = lambda ark, page: _ALTO_V2
    assert fetch_mod.main() == 1
    assert "DRIFT" in capsys.readouterr().out
    assert not (tmp_path / ".cache" / "bpt6kTEST_p0001.alto.xml").exists()


def test_cached_file_is_checksum_verified_too(fetch_mod, tmp_path):
    """A stale/corrupt cache must not bypass the pin: the drifted cached
    file is discarded and the run fails."""
    pin = hashlib.sha256(_ALTO).hexdigest()
    _write_manifest(
        tmp_path,
        [{"ark": "bpt6kTEST", "label": "t", "pages": [1], "sha256": {"1": pin}}],
        max_missing=0,
    )
    cache = tmp_path / ".cache"
    cache.mkdir()
    (cache / "bpt6kTEST_p0001.alto.xml").write_bytes(_ALTO_V2)
    fetch_mod._download = lambda ark, page: pytest.fail("no re-download expected")
    assert fetch_mod.main() == 1


def test_429_backoff_honours_retry_after(fetch_mod, monkeypatch):
    """Gallica rate-limits with 429: the retry must wait what the server
    asked (or a hard 15s/attempt ramp), not the generic 2s/attempt one —
    which provably kept tripping the limiter."""
    import urllib.error

    sleeps: list[float] = []
    monkeypatch.setattr(fetch_mod.time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return _ALTO

    def urlopen(req, timeout):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise urllib.error.HTTPError(
                "u", 429, "Too Many Requests", {"Retry-After": "7"}, None
            )
        return _Resp()

    monkeypatch.setattr(fetch_mod.urllib.request, "urlopen", urlopen)
    payload = fetch_mod._download("bpt6kTEST", 1)
    assert payload == _ALTO
    assert sleeps == [7, 7], "Retry-After must be honoured on each 429"


def test_matching_pin_passes_and_unpinned_digest_is_printed(
    fetch_mod, tmp_path, capsys
):
    pin = hashlib.sha256(_ALTO).hexdigest()
    _write_manifest(
        tmp_path,
        [
            {
                "ark": "bpt6kTEST",
                "label": "t",
                "pages": [1, 2],
                "sha256": {"1": pin},
            }
        ],
        max_missing=0,
    )
    fetch_mod._download = lambda ark, page: _ALTO
    assert fetch_mod.main() == 0
    out = capsys.readouterr().out
    # The unpinned page's digest is printed in manifest syntax, ready to pin.
    assert f'"2": "{pin}"' in out
