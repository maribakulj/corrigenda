"""Security tests for corrigenda XML parsing — XXE / SSRF / entity expansion.

Roadmap L10 Phase 1 (B1) — these tests pin the contract that EVERY
lxml parser instantiation in corrigenda goes through the hardened
``make_safe_parser()`` helper in ``corrigenda.formats._xml`` (the
format-neutral home; each format's ``_ns`` re-exports it).

What the hardened parser actually buys us (and what it does NOT):

  - **DOES block** external entity expansion in TEXT NODES (e.g. a
    `<x>&xxe;</x>` referencing `file:///etc/passwd`). lxml 6.x's
    default already does this, but `resolve_entities=False` makes the
    behaviour explicit and version-stable.
  - **DOES block** external DTD network fetches (SSRF via
    `<!DOCTYPE r SYSTEM "http://attacker/x.dtd">`). lxml 6.x's default
    already does this; `no_network=True` + `load_dtd=False` pin it.
  - **DOES NOT block** internal entity expansion in ATTRIBUTE VALUES.
    lxml's ``resolve_entities=False`` flag, documented by upstream, has
    no effect on attribute-value entity expansion. The mitigation for
    that vector is lxml's built-in amplification cap
    (``xmlCtxtSetMaxAmplification``, ~5x), active regardless of parser
    flags. ALTO puts all OCR text inside ``CONTENT="..."`` attributes,
    so this is the practically relevant attack surface — and the
    defence is lxml's amp cap, not our parser config.

So the value of the L10/B1 fix is **defence in depth + explicit
contract**, not "fixes a previously-exploitable bug observable in
lxml 6.x". The grep-based contract test below is the durable
guarantee: every ``etree.parse``/``etree.fromstring`` call site
must opt in to the hardened parser explicitly. That survives
lxml downgrades, future default changes, and accidental refactors
that reintroduce ``etree.parse(p)``.
"""

from __future__ import annotations

from pathlib import Path

from corrigenda.formats.alto.parser import parse_alto_file
from corrigenda.formats.alto.rewriter import extract_output_texts


def _xxe_payload_text_node(secret_path: Path) -> bytes:
    """ALTO-like document with the external entity referenced from a
    TEXT NODE (not an attribute). This is the vector where
    ``resolve_entities=False`` actually applies. ALTO normally puts
    text in attributes, but the parser should still be hardened in
    case anyone ever produces ALTO-like XML with text-node content.
    """
    return f"""<?xml version="1.0"?>
<!DOCTYPE alto [
  <!ENTITY xxe SYSTEM "file://{secret_path}">
]>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v4#">
  <Layout>
    <Page ID="P1" WIDTH="100" HEIGHT="100">
      <PrintSpace>
        <TextBlock ID="B1">
          <TextLine ID="L1" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="10">
            <String CONTENT="placeholder" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="10"/>
            <!-- intentional text node carrying the entity reference -->
            &xxe;
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>""".encode()


def _external_dtd_payload() -> bytes:
    """DOCTYPE pointing at a likely-unroutable host. A buggy parser
    that ignores ``no_network=True`` would attempt the TCP connect and
    block until the kernel's connect-timeout (typically several
    seconds). A hardened parser returns instantly with the document
    parsed and the DOCTYPE reference left dangling.
    """
    return b"""<?xml version="1.0"?>
<!DOCTYPE alto SYSTEM "http://10.255.255.1:1/blackhole.dtd">
<alto xmlns="http://www.loc.gov/standards/alto/ns-v4#">
  <Layout>
    <Page ID="P1" WIDTH="100" HEIGHT="100">
      <PrintSpace>
        <TextBlock ID="B1">
          <TextLine ID="L1" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="10">
            <String CONTENT="hello" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="10"/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""


# ---------------------------------------------------------------------------
# Behaviour tests — verify the hardened parser blocks the vectors it
# claims to block.
# ---------------------------------------------------------------------------


def test_extract_output_texts_does_not_leak_xxe_text_node_content(tmp_path: Path):
    """L10/B1 — text-node entity references must not yield the file
    contents. Acceptable post-fix behaviour: the parser either raises
    cleanly OR returns whatever attribute-only content is present
    without expanding the entity. Unacceptable: the secret string
    appears anywhere in the result.
    """
    secret = tmp_path / "credentials.txt"
    secret.write_text("ULTRASECRET_LEAKED_FROM_DISK")

    payload = _xxe_payload_text_node(secret)
    try:
        result = extract_output_texts(payload, {"L1"})
    except Exception:
        # Clean parser-level rejection is acceptable.
        result = {}

    for line_id, text in result.items():
        assert "ULTRASECRET" not in text, (
            f"XXE leaked file contents through extract_output_texts: "
            f"line {line_id!r} contains {text!r}."
        )


def test_parse_alto_file_does_not_leak_xxe_text_node_content(tmp_path: Path):
    """Regression guard symmetric with the rewriter test above."""
    secret = tmp_path / "secret.txt"
    secret.write_text("ULTRASECRET_FROM_PARSER_PATH")

    alto_path = tmp_path / "malicious.xml"
    alto_path.write_bytes(_xxe_payload_text_node(secret))

    try:
        pages, _root = parse_alto_file(alto_path, "malicious.xml")
    except Exception:
        pages = []

    leaked: list[tuple[str, str]] = []
    for page in pages:
        for lm in page.lines:
            if "ULTRASECRET" in lm.ocr_text:
                leaked.append((lm.line_id, lm.ocr_text))
    assert not leaked, f"XXE leaked through parse_alto_file: {leaked!r}"


def test_extract_output_texts_returns_quickly_on_external_dtd(tmp_path: Path):
    """L10/B1 — even when the DOCTYPE points at an external URL, the
    parser must not attempt the HTTP fetch. We use a likely-unroutable
    address and bound the call to 2 seconds via SIGALRM. The default
    lxml 6.x parser already declines to fetch, so this test passes
    pre-fix too — its value is regression protection.
    """
    import signal

    payload = _external_dtd_payload()

    def _alarm(_signum, _frame):
        raise TimeoutError("parser attempted a network fetch")

    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(2)
    try:
        result = extract_output_texts(payload, {"L1"})
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)

    assert result.get("L1") == "hello", (
        f"unexpected text extracted (parser may have fetched the DTD "
        f"and processed its contents): {result!r}"
    )


# ---------------------------------------------------------------------------
# Source-AST contract pin — every lxml call site under corrigenda/alto/
# must pass an explicit parser argument. Independent of lxml runtime
# behaviour, this catches future refactors that reintroduce
# `etree.parse(p)` with no parser kwarg.
# ---------------------------------------------------------------------------


def test_no_alto_lxml_call_site_uses_default_parser():
    """Source-AST contract — `etree.parse(...)` or
    `etree.fromstring(...)` without an explicit `parser=...` argument
    silently uses lxml's defaults. Defaults DO change between lxml
    versions; we cannot rely on them.

    AST-based check survives nested parens
    (``etree.parse(str(xml_path), make_safe_parser())``) and any
    other rewrite of the call site.

    A call is considered safe if:
      - it has a ``parser=`` keyword argument, OR
      - it passes 2+ positional args (path + parser).
    """
    import ast

    # §10 — the safe-parser contract covers EVERY format backend, present
    # and future (PAGE XML lands in the same tree).
    src_root = Path(__file__).resolve().parents[1] / "src" / "corrigenda" / "formats"
    offenders: list[tuple[str, int, str]] = []
    for py in src_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in {"parse", "fromstring"}
                and isinstance(func.value, ast.Name)
                and func.value.id == "etree"
            ):
                has_parser_kwarg = any(kw.arg == "parser" for kw in node.keywords)
                has_parser_positional = len(node.args) >= 2
                if not (has_parser_kwarg or has_parser_positional):
                    offenders.append((py.name, node.lineno, f"etree.{func.attr}(...)"))
    assert not offenders, (
        "Found etree.parse/fromstring calls without an explicit parser "
        f"argument — XXE/SSRF risk: {offenders}"
    )


def test_make_safe_parser_returns_fresh_instance_per_call():
    """L10/B1 — `make_safe_parser()` returns a fresh parser each call
    (lxml parsers are not documented as thread-safe). A future "cache
    the parser" optimisation that breaks this would surface here.
    """
    from corrigenda.formats._xml import make_safe_parser

    p1 = make_safe_parser()
    p2 = make_safe_parser()
    assert p1 is not p2, (
        "make_safe_parser() returned the same instance twice — "
        "lxml parsers are not thread-safe; the factory must return fresh ones."
    )


def test_make_safe_parser_enables_all_four_safety_flags():
    """L10/B1 — `make_safe_parser()` MUST set every documented flag.
    Tests the constructor call, not the runtime behaviour: a future
    refactor that drops `load_dtd=False` (for example) is caught
    here even if the corresponding attack happens to be blocked by
    some other lxml mechanism on the current version.
    """
    import ast

    xml_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "corrigenda"
        / "formats"
        / "_xml.py"
    )
    tree = ast.parse(xml_path.read_text(encoding="utf-8"), filename=str(xml_path))
    flags: dict[str, ast.expr] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "make_safe_parser":
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "XMLParser"
                ):
                    for kw in inner.keywords:
                        if kw.arg:
                            flags[kw.arg] = kw.value
            break

    required = {
        "resolve_entities": False,
        "no_network": True,
        "load_dtd": False,
        "dtd_validation": False,
    }
    for flag, expected in required.items():
        assert flag in flags, (
            f"make_safe_parser() does not set {flag!r}; required for L10/B1"
        )
        actual = flags[flag]
        assert isinstance(actual, ast.Constant) and actual.value is expected, (
            f"make_safe_parser() sets {flag!r} to {ast.unparse(actual)!r}, "
            f"expected {expected!r}"
        )
