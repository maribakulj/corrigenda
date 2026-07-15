"""Fetch the external ALTO corpus from Gallica (V4.2 phase 3).

Downloads the per-page ALTO of every document pinned in manifest.json
into ``.cache/`` next to this file (or ``$CORRIGENDA_EXTERNAL_CORPUS_DIR``).
The corpus is EXTERNAL by design: real files from real OCR pipelines,
never used while developing the library, so the tests built on it do not
share the code's assumptions.

Stdlib only — this script runs in a bare CI job before the package's
test dependencies are installed. Network failures on individual pages
are tolerated (the test suite skips what is missing); the exit code is
non-zero only when NOTHING could be fetched.

Usage:  python tests/external_corpus/fetch.py
Source: gallica.bnf.fr / Bibliothèque nationale de France.
"""

from __future__ import annotations

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


def fetch_page(ark: str, page: int, dest: Path) -> bool:
    """Download one page's ALTO; returns True on success."""
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  cached  {dest.name}")
        return True
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
                return False
            dest.write_bytes(payload)
            print(f"  fetched {dest.name} ({len(payload)} bytes)")
            return True
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"  attempt {attempt}/{RETRIES} failed for {ark} p{page}: {exc}")
            time.sleep(2 * attempt)
    return False


def main() -> int:
    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    CACHE.mkdir(parents=True, exist_ok=True)
    fetched = 0
    wanted = 0
    for doc in manifest["documents"]:
        ark = doc["ark"]
        print(f"{ark} — {doc['label']}")
        for page in doc["pages"]:
            wanted += 1
            dest = CACHE / f"{ark}_p{page:04d}.alto.xml"
            if fetch_page(ark, page, dest):
                fetched += 1
            time.sleep(1)  # stay polite with Gallica
    print(f"\n{fetched}/{wanted} pages in {CACHE}")
    return 0 if fetched > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
