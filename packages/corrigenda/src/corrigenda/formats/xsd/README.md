# Bundled ALTO / PAGE XSD schemas (ROADMAP V3 Phase 0)

Official schemas bundled VERBATIM so `corrigenda.formats.validation`
works fully offline — the `xs:import` of xlink that ALTO schemas
reference by URL is remapped to the local `xlink.xsd`, never fetched.
Never edit these files; a re-pin is a reviewed change naming the
upstream release it tracks.

| file | targetNamespace | canonical source | fetched via | date |
|---|---|---|---|---|
| `alto-2-1.xsd` | `http://www.loc.gov/standards/alto/ns-v2#` | loc.gov/standards/alto | github.com/altoxml/schema `master/v2/alto-2-1.xsd` | 2026-07-22 |
| `alto-3-1.xsd` | `http://www.loc.gov/standards/alto/ns-v3#` | loc.gov/standards/alto | github.com/altoxml/schema `master/v3/alto-3-1.xsd` | 2026-07-22 |
| `alto-4-4.xsd` | `http://www.loc.gov/standards/alto/ns-v4#` | loc.gov/standards/alto | github.com/altoxml/schema `master/v4/alto-4-4.xsd` | 2026-07-22 |
| `pagecontent_2013-07-15.xsd` | `…/PAGE/gts/pagecontent/2013-07-15` | schema.primaresearch.org (PRImA Research Lab) | github.com/Transkribus/TranskribusCore `src/main/resources/xsd/pagecontent.xsd` (verbatim mirror) | 2026-07-22 |
| `pagecontent_2019-07-15.xsd` | `…/PAGE/gts/pagecontent/2019-07-15` | schema.primaresearch.org (PRImA Research Lab) | github.com/PRImA-Research-Lab/PAGE-XML tag `2019-07-15` | 2026-07-22 |
| `pagecontent_2024-07-15.xsd` | `…/PAGE/gts/pagecontent/2024-07-15` | schema.primaresearch.org (PRImA Research Lab) | github.com/PRImA-Research-Lab/PAGE-XML `master` | 2026-07-22 |
| `xlink.xsd` | `http://www.w3.org/1999/xlink` | w3.org / loc.gov/standards/xlink | PyPI `ocrd_validators` 2.67.1 (verbatim mirror) | 2026-07-22 |

ALTO schemas: Library of Congress ALTO standard (altoxml). PAGE
schemas: © PRImA Research Lab, University of Salford — redistributed
unmodified for validation purposes with attribution. xlink: W3C.
