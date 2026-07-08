"""Pin the configuration fingerprint and the §8.2 policy surface (audit
Phase 1 — filet for audit Problem 7).

Problem 7: ``GuardConfig`` carries 21 knobs, two of which are duplicated
concepts (``part2_collapse_ratio`` / ``pair_drift_part2_collapse_ratio``
and ``part1_max_word_growth`` / ``pair_drift_part1_word_growth``). Merging
the duplicates is a **public-API break** (§11 provenance fingerprints
every policy field) and must never happen silently. This test freezes:

  * the exact ``config_fingerprint()`` stamped into corrected XML today;
  * every policy's ``policy_fingerprint()``;
  * the full ``GuardConfig`` field set (so removing a field trips here);
  * the current values of the duplicated knobs (so the Phase-4 merge is a
    conscious edit to this file, tied to a SemVer decision).

If a change here is intentional, update the pinned values AND record the
version bump / CHANGELOG entry in the same commit.
"""

from __future__ import annotations

from corrigenda.core.schemas import (
    ChunkPlannerConfig,
    GuardConfig,
    PairingPolicy,
    RetryPolicy,
)
from corrigenda.core.pipeline import CorrectionPipeline


class _Noop:
    wants_geometry = False
    wants_image = False
    requires_full_coverage = True

    async def produce(self, payload, *, policy):  # pragma: no cover
        from corrigenda.core.editing import EditScript

        return EditScript(ops=[]), None

    def on_event(self, event_type, payload):  # pragma: no cover
        pass

    def write_corrected(self, *, source_stem, xml_bytes):  # pragma: no cover
        pass

    def write_trace(self, *, traces_payload):  # pragma: no cover
        pass


def _default_pipeline() -> CorrectionPipeline:
    noop = _Noop()
    return CorrectionPipeline(
        producer=noop,
        observer=noop,
        output_writer=noop,
    )


# --- Fingerprints -----------------------------------------------------------


def test_config_fingerprint_is_pinned():
    """The composite fingerprint stamped into every corrected XML's
    processingStep. Changing any default policy value changes this."""
    assert _default_pipeline().config_fingerprint() == "3a06d0a93ac4eedc"


def test_each_policy_fingerprint_is_pinned():
    assert GuardConfig().policy_fingerprint() == "48fef9e0d6feb681"
    # The remaining three are pinned by-shape: any default change trips the
    # composite above, and these lock each policy independently.
    for policy in (ChunkPlannerConfig(), PairingPolicy(), RetryPolicy()):
        fp = policy.policy_fingerprint()
        assert isinstance(fp, str) and len(fp) == 16


# --- GuardConfig surface (Problem 7 target) ---------------------------------


_GUARD_FIELDS = {
    "absorption_concat_similarity",
    "absorption_length_ratio",
    "boundary_len_ratio_max",
    "boundary_len_ratio_min",
    "boundary_prefix_len",
    "duplicate_source_min_diff",
    "duplicate_threshold",
    "edit_line_max_changed_chars",
    "edit_span_max_growth_ratio",
    "min_source_similarity",
    "neighbour_margin",
    "pair_drift_part1_word_growth",
    "pair_drift_part2_collapse_ratio",
    "pair_drift_part2_min_words",
    "part1_char_growth_ratio",
    "part1_char_growth_slack",
    "part1_last_word_char_growth",
    "part1_max_word_growth",
    "part2_collapse_ratio",
    "part2_expansion_floor",
    "part2_expansion_ratio",
}


def test_guard_config_field_set_is_frozen():
    """21 knobs today. Removing one (the Problem-7 dedup) must be a
    deliberate edit here, paired with a version bump."""
    assert set(GuardConfig.model_fields) == _GUARD_FIELDS
    assert len(_GUARD_FIELDS) == 21


def test_duplicated_ratio_knobs_still_present_and_documented():
    """The two duplicated-concept knob pairs the audit flagged for merge.
    Pinned so the Phase-4 consolidation is visible and intentional."""
    g = GuardConfig()
    # Same concept ("PART2 collapsed too far"), same value, two knobs.
    assert g.part2_collapse_ratio == 0.4
    assert g.pair_drift_part2_collapse_ratio == 0.4
    # Same concept ("PART1 grew too many words"), DIFFERENT value per stage.
    assert g.part1_max_word_growth == 1
    assert g.pair_drift_part1_word_growth == 2
