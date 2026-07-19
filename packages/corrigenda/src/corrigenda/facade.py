"""The three-line happy path (P3.12, §2): load → correct → write.

::

    document = corrigenda.load("page.xml")            # format by namespace
    result = await corrigenda.correct(document, producer=producer)
    result.write("out/")

Observer, format adapter, manifest plumbing: optional, never required
for the simple case. :func:`load` sniffs each file's root namespace and
dispatches to the matching parser (ALTO or PAGE — one format per
document); :func:`correct` / :func:`correct_sync` wrap a default
:class:`~corrigenda.core.pipeline.CorrectionPipeline` around any
:class:`~corrigenda.core.protocols.EditProducer` with a no-op observer.
Power users keep constructing the pipeline directly — every knob this
façade hides stays available there.

This module imports the format packages (lxml), so it is exported
LAZILY from the top level: ``import corrigenda`` alone stays pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lxml import etree

from corrigenda.core.pipeline import CorrectionPipeline, CorrectionResult
from corrigenda.core.protocols import EditProducer
from corrigenda.core.schemas import DocumentManifest, ImageRef
from corrigenda.errors import ParseError
from corrigenda.formats._xml import (
    classified_parse_errors,
    detect_namespace,
    make_safe_parser,
)

_ALTO_MARKER = "loc.gov/standards/alto"
_PAGE_MARKER = "primaresearch.org/PAGE"


class _NullObserver:
    """The façade's default: the simple path needs no event sink."""

    def on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        pass


@dataclass(frozen=True)
class LoadedDocument:
    """What :func:`load` hands to :func:`correct`: the parsed manifest
    plus the name → path map a run needs to render its outputs."""

    manifest: DocumentManifest
    source_paths: dict[str, Path]


def _sniff_format(path: Path) -> str:
    """ "alto" / "page" from the file's root namespace (§3 — the document
    says what it is; nobody passes a format flag on the happy path)."""
    with classified_parse_errors(path.name):
        root = etree.parse(str(path), make_safe_parser()).getroot()
    ns = detect_namespace(root)
    if _ALTO_MARKER in ns:
        return "alto"
    if _PAGE_MARKER in ns:
        return "page"
    raise ParseError(
        f"{path.name!r}: root namespace {ns!r} is neither ALTO nor PAGE — "
        "corrigenda.load() only speaks those two; parse other formats "
        "through their own adapter."
    )


def load(*paths: str | Path) -> LoadedDocument:
    """Parse one or more ALTO/PAGE files into a single document.

    The format is detected per file from the root namespace; all files
    of one document must share it (a mixed ALTO+PAGE document has no
    single rewriter). File basenames become the artefact keys
    (``result.corrected_files``), so they must be unique across paths.
    """
    if not paths:
        raise ParseError("corrigenda.load() needs at least one file path")
    resolved = [Path(p) for p in paths]

    by_name: dict[str, Path] = {}
    for path in resolved:
        if path.name in by_name:
            raise ParseError(
                f"two source files share the basename {path.name!r} "
                f"({by_name[path.name]} and {path}) — basenames key the "
                "corrected artefacts and must be unique; rename one file."
            )
        by_name[path.name] = path

    formats = {path: _sniff_format(path) for path in resolved}
    distinct = set(formats.values())
    if len(distinct) > 1:
        detail = ", ".join(f"{p.name}: {f}" for p, f in formats.items())
        raise ParseError(
            f"one document, one format — got a mix ({detail}). "
            "Load ALTO and PAGE files as separate documents."
        )

    if distinct == {"page"}:
        from corrigenda.formats.page.parser import build_document_manifest
    else:
        from corrigenda.formats.alto.parser import build_document_manifest

    manifest = build_document_manifest([(p, p.name) for p in resolved])
    return LoadedDocument(manifest=manifest, source_paths=by_name)


async def correct(
    document: LoadedDocument,
    *,
    producer: EditProducer,
    run_id: str | None = None,
    should_abort: Callable[[], bool] | None = None,
    page_images: dict[str, ImageRef] | None = None,
) -> CorrectionResult:
    """Run the correction pipeline over a loaded document (§2).

    Wraps a default :class:`CorrectionPipeline` (no-op observer, default
    policies, provenance from the producer's own declared metadata)
    around ``producer``. Every knob — policies, observer, explicit
    metadata — lives on the pipeline constructor for callers who need
    it; this function is the three-line path, not a second surface.
    """
    pipeline = CorrectionPipeline(producer=producer, observer=_NullObserver())
    return await pipeline.run(
        document_manifest=document.manifest,
        source_files=document.source_paths,
        run_id=run_id,
        should_abort=should_abort,
        page_images=page_images,
    )


def correct_sync(
    document: LoadedDocument,
    *,
    producer: EditProducer,
    run_id: str | None = None,
    should_abort: Callable[[], bool] | None = None,
    page_images: dict[str, ImageRef] | None = None,
) -> CorrectionResult:
    """Synchronous twin of :func:`correct` (scripts, notebooks, CLIs).

    Must not be called from within a running event loop — use
    ``await corrigenda.correct(...)`` there.
    """
    pipeline = CorrectionPipeline(producer=producer, observer=_NullObserver())
    return pipeline.run_sync(
        document_manifest=document.manifest,
        source_files=document.source_paths,
        run_id=run_id,
        should_abort=should_abort,
        page_images=page_images,
    )


__all__ = [
    "LoadedDocument",
    "correct",
    "correct_sync",
    "load",
]
