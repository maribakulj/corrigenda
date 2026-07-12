"""FilesystemOutputWriter — persists corrected ALTO and job trace to disk.

Implements the `OutputWriter` Protocol from `app.protocols`. Used by the
correction pipeline as the default sink; swap for another implementation
(S3, in-memory for tests, etc.) to retarget the pipeline's outputs.

P0-4 — writes are TRANSACTIONAL: every file lands in a hidden staging
directory first and only an explicit :meth:`commit` (called by the
JobRunner after the whole run succeeded) atomically renames the set into
the final directory. Historically each corrected XML was written
directly under its final name as the run progressed, so a failure on
file 2 of 3 left file 1 sitting in the output directory — and
``/download`` happily served the partial, trace-less result of a FAILED
job. :meth:`discard` (failure/timeout/cancellation path) removes the
staging tree; nothing partial ever becomes visible.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

#: Hidden staging directory name; leading dot keeps it out of
#: ``get_output_files`` (suffix-filtered) and of any glob of the output dir.
STAGING_DIRNAME = ".staging"


class FilesystemOutputWriter:
    """Writes corrected ALTO bytes and the job trace JSON to a directory.

    The writer owns its base directory; the pipeline does not need to
    pass paths around. Files staged then committed:
      - ``{base_dir}/{source_stem}_corrected.xml`` (one per source ALTO)
      - ``{base_dir}/trace.json`` (once per job)
    """

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self._staging = base_dir / STAGING_DIRNAME

    def write_corrected(self, *, source_stem: str, xml_bytes: bytes) -> None:
        self._staging.mkdir(parents=True, exist_ok=True)
        (self._staging / f"{source_stem}_corrected.xml").write_bytes(xml_bytes)

    def write_trace(self, *, traces_payload: str) -> None:
        self._staging.mkdir(parents=True, exist_ok=True)
        (self._staging / "trace.json").write_text(traces_payload, encoding="utf-8")

    def commit(self) -> None:
        """Promote every staged file into the final directory as a set.

        Called by the JobRunner ONLY after the pipeline returned
        successfully. Each file lands via a same-filesystem ``os.replace``,
        so a reader never observes a half-written file (it sees the old
        state or the new one). POSIX has no true multi-file atomic rename,
        so if a later file fails to promote (ENOSPC/EIO), the files already
        moved are rolled back out of the output directory — the set is
        all-or-nothing on any in-process error, never a partial promotion.
        (A hard process kill *between* two ``os.replace`` calls is the only
        residual window; the ``/download`` status guard, which refuses a
        non-terminal-success job, is the backstop there.) No-op when
        nothing was staged (dry-run)."""
        if not self._staging.is_dir():
            return
        self.base_dir.mkdir(parents=True, exist_ok=True)
        promoted: list[Path] = []
        try:
            for staged in sorted(self._staging.iterdir()):
                dest = self.base_dir / staged.name
                os.replace(staged, dest)
                promoted.append(dest)
        except OSError:
            # Roll back the already-promoted files so no partial set is left
            # visible in the output directory. The run is failing regardless;
            # discard() (failure path) then drops whatever remains in staging.
            for dest in promoted:
                try:
                    dest.unlink()
                except OSError:
                    pass
            raise
        self._staging.rmdir()

    def discard(self) -> None:
        """Drop the staging tree (failure/timeout/cancellation path) —
        nothing partial ever reaches the final directory."""
        if self._staging.is_dir():
            shutil.rmtree(self._staging, ignore_errors=True)
