"""The shipped quickstart example must actually run (P5 — executable docs).

Runs ``examples/quickstart.py`` in a subprocess against the repo sample
and checks its outputs: both producer passes complete offline and
``result.write(dir)`` persists a corrected ALTO + the versioned
CorrectionReport (``report.json``).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_PKG = Path(__file__).resolve().parents[1]
_QUICKSTART = _PKG / "examples" / "quickstart.py"


def test_quickstart_runs_offline_and_writes_outputs(tmp_path: Path):
    proc = subprocess.run(
        [sys.executable, str(_QUICKSTART), str(tmp_path / "out")],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"quickstart failed:\n{proc.stderr}"
    assert "Done." in proc.stdout

    for variant in ("rules", "llm"):
        out = tmp_path / "out" / variant
        assert (out / "sample.xml").exists(), f"{variant}: no corrected XML"
        trace = json.loads((out / "report.json").read_text(encoding="utf-8"))
        # report.json IS the CorrectionReport (§9 unification).
        assert trace["report_version"] == "2.0"
        assert trace["run_id"] == f"quickstart-{variant}"
        assert trace["total_lines"] > 0

    # The rules pass genuinely edited lines (the demo e→3 substitution).
    rules_trace = json.loads(
        (tmp_path / "out" / "rules" / "report.json").read_text(encoding="utf-8")
    )
    edited = [
        ln
        for ln in rules_trace["lines"]
        if ln["decision"]["final_text"] != ln["source_text"]
    ]
    assert edited, "rules pass edited nothing — demo substitution broken"
