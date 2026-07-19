"""P2-5 — configuration models validate invariants, not just types.

Every policy knob used to be a bare ``int``/``float``: negative backoffs,
zero chunk limits, out-of-range similarity ratios, invalid temperatures
and contradictory manifest counters were all silently accepted, then
produced arithmetic nonsense deep inside the pipeline. Configuration is
user input — it fails fast at construction now.

Deliberate exception: ``Coords`` (and the other *data* models fed from
wild heritage XML) stay tolerant per the F5 policy — a scan with a
slightly negative position must not abort the file; geometry consumers
(e.g. ``PairingPolicy``) treat degenerate boxes defensively instead.
"""

from __future__ import annotations

import pytest

from corrigenda.core.schemas import (
    ChunkGranularity,
    ChunkPlannerConfig,
    ChunkRequest,
    Coords,
    DocumentManifest,
    GuardConfig,
    LineManifest,
    PageManifest,
    PairingPolicy,
    RetryPolicy,
)


# ---------------------------------------------------------------------------
# ChunkPlannerConfig
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_input_chars_per_request": 0},
        {"max_input_chars_per_request": -1},
        {"max_lines_per_request": 0},
        {"line_window_size": 0},
        {"line_window_overlap": -1},
        {"line_window_size": 4, "line_window_overlap": 4},  # cannot advance
        {"line_window_size": 4, "line_window_overlap": 9},
    ],
)
def test_planner_config_rejects_nonsense(kwargs):
    with pytest.raises(ValueError):
        ChunkPlannerConfig(**kwargs)


def test_planner_config_defaults_still_valid():
    assert ChunkPlannerConfig().line_window_size == 12


# ---------------------------------------------------------------------------
# GuardConfig
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_source_similarity": -0.1},
        {"min_source_similarity": 1.5},
        {"duplicate_threshold": 2.0},
        {"part2_collapse_ratio": -0.4},
        {"part1_max_word_growth": -1},
        {"edit_span_max_growth_ratio": 0.0},
        {"edit_line_max_changed_chars": -5},
        {"boundary_len_ratio_min": 3.0, "boundary_len_ratio_max": 2.0},  # inverted
    ],
)
def test_guard_config_rejects_nonsense(kwargs):
    with pytest.raises(ValueError):
        GuardConfig(**kwargs)


def test_guard_config_defaults_still_valid():
    assert GuardConfig().duplicate_threshold == 0.85


# ---------------------------------------------------------------------------
# RetryPolicy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"transient_backoff_base": -2.0},
        {"output_backoff_base": -1.0},
        {"per_chunk_budget": 0},
        {"temperatures": (0.0, -0.5)},
        {"temperatures": (2.5,)},
    ],
)
def test_retry_policy_rejects_nonsense(kwargs):
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


def test_retry_policy_defaults_still_valid():
    assert RetryPolicy().max_attempts == 3


# ---------------------------------------------------------------------------
# PairingPolicy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_vertical_gap": -1},
        {"max_gap_line_heights": -0.5},
        {"max_rise_line_heights": -0.1},
    ],
)
def test_pairing_policy_rejects_nonsense(kwargs):
    with pytest.raises(ValueError):
        PairingPolicy(**kwargs)


# ---------------------------------------------------------------------------
# ChunkRequest — target ⊆ lines
# ---------------------------------------------------------------------------


def _chunk(**overrides):
    base = dict(
        document_id="d1",
        page_id="p1",
        granularity=ChunkGranularity.WINDOW,
        line_ids=["l1", "l2", "l3"],
    )
    base.update(overrides)
    return ChunkRequest(**base)


def test_chunk_targets_must_be_subset_of_lines():
    with pytest.raises(ValueError, match="l9"):
        _chunk(target_line_ids=["l1", "l9"])


def test_chunk_targets_subset_accepted():
    assert _chunk(target_line_ids=["l2"]).targets() == ["l2"]


def test_chunk_negative_attempt_rejected():
    with pytest.raises(ValueError):
        _chunk(attempt=-1)


# ---------------------------------------------------------------------------
# DocumentManifest — counters must match content
# ---------------------------------------------------------------------------


def _page(n_lines: int = 1) -> PageManifest:
    lines = [
        LineManifest(
            line_id=f"l{i}",
            page_id="p1",
            block_id="b1",
            line_order_global=i,
            line_order_in_block=i,
            coords=Coords(hpos=0, vpos=20 * i, width=100, height=15),
            ocr_text="texte",
        )
        for i in range(n_lines)
    ]
    return PageManifest(
        page_id="p1",
        source_file="a.xml",
        page_index=0,
        page_width=1000,
        page_height=1000,
        blocks=[],
        lines=lines,
    )


def test_document_manifest_counters_are_computed():
    """ADR-011 — the counters derive from the pages. A stored copy could
    contradict the content (the retired validator existed to catch
    exactly that lie); a computed one cannot, and a caller passing
    (even lying) legacy kwargs cannot skew them."""
    page = _page(2)
    doc = DocumentManifest(source_files=["a.xml"], pages=[page])
    assert doc.total_pages == 1
    assert doc.total_blocks == 0
    assert doc.total_lines == 2

    lying = DocumentManifest(
        source_files=["a.xml"],
        pages=[page],
        total_pages=3,
        total_blocks=7,
        total_lines=99,
    )
    assert lying.total_pages == 1
    assert lying.total_lines == 2
    # The counters stay part of the serialized shape.
    assert lying.model_dump()["total_lines"] == 2


# ---------------------------------------------------------------------------
# Deliberate tolerance: data models stay permissive (F5)
# ---------------------------------------------------------------------------


def test_coords_stays_tolerant_for_heritage_data():
    """A slightly negative position from a skewed scan must not abort the
    file — geometry consumers handle degenerate boxes defensively."""
    c = Coords(hpos=-3, vpos=-1, width=0, height=0)
    assert c.hpos == -3
