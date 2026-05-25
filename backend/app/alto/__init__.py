"""Backend's ``app.alto`` namespace — re-export shims onto alto-core.

The implementation lives in :mod:`alto_core.alto`. The ``parser``,
``rewriter``, ``hyphenation``, ``_norm`` and ``_ns`` sub-modules of
this package are thin shims that re-export the documented public API
of their alto-core counterparts (see each shim's docstring for the
canonical import path).

This file is intentionally empty of code: existing call sites
``from app.alto.X import Y`` resolve through Python's normal package
machinery without needing re-exports at the package level. A future
cleanup may delete the whole shim layer once consumers migrate to
``from alto_core.alto.X import Y`` directly.
"""
