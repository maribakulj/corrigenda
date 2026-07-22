# ALTO / PAGE version support matrix

What corrigenda accepts, what it writes back, and what it can check
against an official schema (ROADMAP V3 Phase 0). "Parse/rewrite" is
namespace-tolerant: the parsers accept any root namespace matching the
format marker (`loc.gov/standards/alto` / `primaresearch.org/PAGE`) and
the rewriter re-emits the document in its ORIGINAL namespace. "XSD
bundled" means `corrigenda.formats.validation` can validate that
namespace offline (schemas + provenance: `../src/corrigenda/formats/xsd/`).

| Format | Root namespace | Parse / rewrite | XSD bundled |
|---|---|---|---|
| ALTO v2 | `http://www.loc.gov/standards/alto/ns-v2#` | yes | `alto-2-1.xsd` |
| ALTO v3 | `http://www.loc.gov/standards/alto/ns-v3#` | yes | `alto-3-1.xsd` |
| ALTO v4 | `http://www.loc.gov/standards/alto/ns-v4#` | yes | `alto-4-4.xsd` |
| PAGE 2013 | `…/PAGE/gts/pagecontent/2013-07-15` | yes | `pagecontent_2013-07-15.xsd` |
| PAGE 2019 | `…/PAGE/gts/pagecontent/2019-07-15` | yes | `pagecontent_2019-07-15.xsd` |
| PAGE 2024 | `…/PAGE/gts/pagecontent/2024-07-15` | yes | `pagecontent_2024-07-15.xsd` |
| PAGE, other dates | any other `pagecontent/…` namespace | yes (tolerant) | no — validation raises `ParseError` |

## Validation roles

- **Input — diagnostic.** Real-world exports carry dialect extensions:
  Transkribus writes a `TranskribusMetadata` element that the official
  2013-07-15 schema does not know (pinned by
  `tests/test_xsd_validation.py`). A host should SURFACE input
  violations, not refuse the document — the manifest builds fine.
- **Output — gate.** A rewrite must never *introduce* a violation:
  zero violations when the source was clean, no new messages when the
  source carried a dialect. Enforced in the default test suite (the
  identity and slow-path/rebuild cases), fully offline — the xlink
  import inside ALTO schemas resolves to the bundled copy, never the
  network.

## API

```python
from corrigenda.formats.validation import validate_file, validate_bytes

violations = validate_file(Path("scan.xml"))   # [] == valid
violations = validate_bytes(xml_bytes, source_name="scan.xml")
```

Both raise `ParseError` (classified, §8.4) for malformed XML or a root
namespace with no bundled schema.
