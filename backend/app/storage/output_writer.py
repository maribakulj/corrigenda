"""FilesystemOutputWriter — persists corrected ALTO and job trace to disk.

Implements the `OutputWriter` Protocol from `app.protocols`. Used by the
correction pipeline as the default sink; swap for another implementation
(S3, in-memory for tests, etc.) to retarget the pipeline's outputs.
"""

from __future__ import annotations

from pathlib import Path


class FilesystemOutputWriter:
    """Writes corrected ALTO bytes and the job trace JSON to a directory.

    The writer owns its base directory; the pipeline does not need to
    pass paths around. Files written:
      - ``{base_dir}/{source_stem}_corrected.xml`` (one per source ALTO)
      - ``{base_dir}/trace.json`` (once per job)
    """

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir

    def write_corrected(self, *, source_stem: str, xml_bytes: bytes) -> None:
        out_path = self.base_dir / f"{source_stem}_corrected.xml"
        out_path.write_bytes(xml_bytes)

    def write_trace(self, *, traces_payload: str) -> None:
        trace_path = self.base_dir / "trace.json"
        trace_path.write_text(traces_payload, encoding="utf-8")
