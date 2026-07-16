"""§3 import-rule contract — the module graph is law, not convention.

Rules enforced:
  1. ``corrigenda.core`` (and ``errors``) never import lxml, formats or
     producers — statically NOR at import time (subprocess-verified: no
     ``lxml`` in ``sys.modules`` after importing every core module).
  2. Exactly TWO pinned lazy functions, both composition boundaries in
     ``core/pipeline.py``, both with function-local imports only:
     ``_adapter_for_format`` (resolves the adapter the MANIFEST declares
     — one import per supported format, no implicit default) and
     ``for_provider`` (lazy ``LLMEditProducer`` wrap — the §5.1
     resorption moved the prompt/schema seam into the producer, so the
     old ``_default_llm_contract`` exception is gone).
  3. ``formats`` never imports producers; ``producers`` never imports
     formats or lxml.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "corrigenda"

FORBIDDEN_IN_CORE = ("lxml", "corrigenda.formats", "corrigenda.producers")


def _imports(tree: ast.AST) -> list[tuple[str, int]]:
    """Every imported module name in the tree, with its line number."""
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((a.name, node.lineno) for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append((node.module, node.lineno))
    return out


def _violations(path: Path, forbidden: tuple[str, ...]) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        f"{path.name}:{lineno} imports {name}"
        for name, lineno in _imports(tree)
        if any(name == f or name.startswith(f + ".") for f in forbidden)
    ]


def test_core_has_no_forbidden_imports_except_pinned_lazy_default():
    core_files = sorted((SRC / "core").glob("*.py")) + [SRC / "errors.py"]
    assert core_files
    all_violations: list[str] = []
    for f in core_files:
        all_violations.extend(_violations(f, FORBIDDEN_IN_CORE))
    # The only allowed sites: the two pinned lazy functions in pipeline.py
    # (_adapter_for_format imports one adapter per supported format).
    assert len(all_violations) == 3, f"unexpected core imports: {all_violations}"
    assert all("pipeline.py" in v for v in all_violations), all_violations

    # And those imports must be FUNCTION-LOCAL, inside the pinned names.
    tree = ast.parse((SRC / "core" / "pipeline.py").read_text(encoding="utf-8"))
    lazy_funcs = sorted(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            n
            for n, _ in _imports(node)
            if n.startswith(("corrigenda.formats", "corrigenda.producers"))
        )
    )
    assert lazy_funcs == ["_adapter_for_format", "for_provider"], lazy_funcs


def test_importing_core_never_loads_lxml():
    """Runtime guarantee, not just static: a consumer that only wants the
    pure algorithms (guards, planner, schemas, reconciliation, pipeline)
    pays zero lxml import cost — and can run where lxml isn't installed."""
    code = (
        "import sys; "
        "import corrigenda.core.pipeline, corrigenda.core.schemas, "
        "corrigenda.core.guards, corrigenda.core.validator, "
        "corrigenda.core.hyphenation, corrigenda.core.planner, "
        "corrigenda.core.protocols, corrigenda.errors; "
        "sys.exit(1 if 'lxml' in sys.modules else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, f"importing core loaded lxml\n{proc.stderr}"


def test_producers_are_pure_and_formats_ignore_producers():
    for f in sorted((SRC / "producers").glob("*.py")):
        bad = _violations(f, ("lxml", "corrigenda.formats"))
        assert not bad, bad
    for f in sorted((SRC / "formats").rglob("*.py")):
        bad = _violations(f, ("corrigenda.producers",))
        assert not bad, bad
