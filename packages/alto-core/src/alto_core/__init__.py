"""alto-core — pure ALTO XML correction pipeline.

Sub-packages:

- :mod:`alto_core.alto` — ALTO XML parsing/rewriting and the
  Hyphenation Reconciler.
- :mod:`alto_core.pipeline` — chunk planning, validation, line
  acceptance, and the orchestrating ``CorrectionPipeline``.
- :mod:`alto_core.schemas` — Pydantic models shared across the pipeline.
- :mod:`alto_core.protocols` — ports (``BaseProvider``,
  ``PipelineObserver``, ``OutputWriter``) consumers implement.
"""

__version__ = "0.1.0a1"
