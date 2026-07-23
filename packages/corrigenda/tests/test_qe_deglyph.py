"""Pure tests for the QE scorer's glyph-neutralization helpers.

No model, no onnxruntime: ``corrigenda.integrations.qe`` imports without
the ``qe`` extra (heavy deps are lazy), so these run in every environment
and lock the doctrine that scoring is glyph-neutral while the ORIGINAL
spelling is always recoverable for reporting (ROADMAP rule 3).
"""

from __future__ import annotations

from corrigenda.integrations.qe import _deglyph, _deglyph_with_map


def test_deglyph_maps_glyphs_not_language() -> None:
    # long-s and ligatures fold to ASCII; the period LANGUAGE (u-for-v in
    # "auoir") is left untouched — only typography is neutralized.
    assert _deglyph("qu'il eſt bon de les auoir") == "qu'il est bon de les auoir"
    assert _deglyph("richeſſes & œufs ﬁn") == "richesses & oeufs fin"


def test_deglyph_preserves_word_count_and_order() -> None:
    # The word→subword mapping relies on glyph substitution never adding or
    # removing a whitespace boundary.
    for text in ("qu'il eſt bon", "des richeſſes & œufs", "ﬁn ﬂeur æques"):
        assert len(_deglyph(text).split()) == len(text.split())


def test_deglyph_map_reconstructs_original_span() -> None:
    text = "œufs ﬁns"
    deglyphed, origin = _deglyph_with_map(text)
    assert deglyphed == "oeufs fins"
    assert len(origin) == len(deglyphed)
    # a deglyphed word span maps back to the ORIGINAL archaic spelling,
    # even though ﬁ→fi and œ→oe changed the length.
    assert text[origin[0] : origin[4] + 1] == "œufs"  # "oeufs" -> "œufs"


def test_deglyph_map_is_identity_on_modern_orthography() -> None:
    # For a 19th-c. press model the copy is byte-identical and the index
    # map is the identity — deglyph never disturbs already-modern text.
    plain, origin = _deglyph_with_map("chemin de fer et télégraphe")
    assert plain == "chemin de fer et télégraphe"
    assert origin == list(range(len(plain)))
