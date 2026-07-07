"""Backend's ``app.jobs`` namespace.

Mix of in-place backend code (``store``, ``runner``, ``observers``,
``task_registry``, ``orchestrator`` compat wrapper) and re-export
shims onto corrigenda (``chunk_planner``, ``validator``,
``line_acceptance``, ``correction_pipeline``).

Intentionally empty at the package level so the module attributes
(``app.jobs.runner``, ``app.jobs.store``, …) are resolved through
Python's normal package machinery.
"""
