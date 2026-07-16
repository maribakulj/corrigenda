"""Fetch the external ALTO corpus from Gallica (V4.2 phase 3).

Downloads the per-page ALTO of every document pinned in manifest.json
into ``.cache/`` next to this file (or ``$CORRIGENDA_EXTERNAL_CORPUS_DIR``).
The corpus is EXTERNAL by design: real files from real OCR pipelines,
never used while developing the library, so the tests built on it do not
share the code's assumptions.

Integrity contract:

- every page may carry a pinned SHA-256 in the manifest
  (``documents[].sha256`` maps page number → hex digest). A pinned page
  whose payload diverges — Gallica re-OCR, truncated download, CDN
  error page — is DELETED from the cache and reported as drift: the
  script exits non-zero so the (non-blocking) CI job surfaces an
  explicit alert instead of silently testing different bytes.
- unpinned pages are accepted and their digests printed in manifest
  syntax, ready to be pasted in — pinning is one copy/paste after the
  first green run.
- ``max_missing`` (manifest, default 0) is the explicit tolerance for
  network flakiness: the script fails when MORE pages are missing, not
  only when nothing at all was fetched.

Stdlib only — this script runs in a bare CI job before the package's
test dependencies are installed.

Usage:  python tests/external_corpus/fetch.py
Source: gallica.bnf.fr / Bibliothèque nationale de France.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("CORRIGENDA_EXTERNAL_CORPUS_DIR", HERE / ".cache"))
ALTO_URL = "https://gallica.bnf.fr/RequestDigitalElement?O={ark}&E=ALTO&Deb={page}"
RETRIES = 3
TIMEOUT = 60


def _download(ark: str, page: int) -> bytes | None:
    """Fetch one page's ALTO bytes, or ``None`` after RETRIES failures."""
    url = ALTO_URL.format(ark=ark, page=page)
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "corrigenda-external-corpus/1.0"}
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                payload = resp.read()
            if b"<alto" not in payload[:2000] and b"<ALTO" not in payload[:2000]:
                print(f"  not ALTO {ark} p{page} (skipping)")
                return None
            return payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"  attempt {attempt}/{RETRIES} failed for {ark} p{page}: {exc}")
            time.sleep(2 * attempt)
    return None


def fetch_page(
    ark: str, page: int, dest: Path, expected_sha256: str | None
) -> tuple[bool, str | None]:
    """Ensure one page is cached and integrity-checked.

    Returns ``(ok, digest)``. A pinned page whose bytes diverge from the
    pin is deleted and reported failed — drifted corpus must never be
    silently tested.
    """
    if dest.exists() and dest.stat().st_size > 0:
        payload = dest.read_bytes()
        origin = "cached "
    else:
        downloaded = _download(ark, page)
        if downloaded is None:
            return False, None
        payload = downloaded
        origin = "fetched"

    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        dest.unlink(missing_ok=True)
        print(
            f"  DRIFT   {ark} p{page}: sha256 {digest} != pinned "
            f"{expected_sha256} — Gallica re-OCR or corrupt download; "
            "file discarded"
        )
        return False, digest
    dest.write_bytes(payload)
    print(f"  {origin} {dest.name} ({len(payload)} bytes)")
    return True, digest


def main() -> int:
    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    max_missing = int(manifest.get("max_missing", 0))
    CACHE.mkdir(parents=True, exist_ok=True)

    fetched = 0
    wanted = 0
    drifted = 0
    unpinned: list[str] = []
    for doc in manifest["documents"]:
        ark = doc["ark"]
        pins = {str(k): v for k, v in (doc.get("sha256") or {}).items()}
        print(f"{ark} — {doc['label']}")
        for page in doc["pages"]:
            wanted += 1
            expected = pins.get(str(page))
            dest = CACHE / f"{ark}_p{page:04d}.alto.xml"
            ok, digest = fetch_page(ark, page, dest, expected)
            if ok:
                fetched += 1
                if expected is None and digest is not None:
                    unpinned.append(f'{ark} p{page}: "{page}": "{digest}"')
            elif digest is not None:
                drifted += 1
            time.sleep(1)  # stay polite with Gallica

    missing = wanted - fetched
    print(f"\n{fetched}/{wanted} pages in {CACHE} (tolerance: {max_missing} missing)")
    if unpinned:
        print(
            "\nUNPINNED pages — paste these digests into manifest.json "
            "(documents[].sha256) to pin the corpus:"
        )
        for line in unpinned:
            print(f"  {line}")
    if drifted:
        print(
            f"\n{drifted} page(s) DRIFTED from their pinned sha256 — "
            "inspect the new content, then re-pin deliberately if the "
            "re-OCR is legitimate."
        )
        return 1
    if missing > max_missing:
        print(f"\n{missing} page(s) missing exceeds the tolerance of {max_missing}.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
