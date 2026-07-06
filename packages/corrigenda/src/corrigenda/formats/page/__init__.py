"""PAGE XML (PRImA) support — parser, rewriter and format adapter.

PAGE is the native format of Transkribus and eScriptorium. The pure core
(manifests, planner, guards, reconciliation, protocol) is reused as-is;
only this package is format-specific (spec 6.2). See ``_ns`` for the
namespace/geometry helpers, ``parser`` for parsing and ``rewriter`` for
the correction write-back.
"""

from __future__ import annotations

__all__: list[str] = []
