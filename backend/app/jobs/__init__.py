"""Backend's ``app.jobs`` namespace.

In-place backend infrastructure only: ``store`` (job state + SSE fan-out),
``runner`` (drives corrigenda's ``CorrectionPipeline``), ``observers``,
and ``task_registry``. The correction engine itself lives in the
``corrigenda`` library; nothing here re-implements or shims it.

Intentionally empty at the package level so the module attributes
(``app.jobs.runner``, ``app.jobs.store``, …) are resolved through
Python's normal package machinery.
"""
