"""Validator for LLM structured responses."""

from __future__ import annotations

from app.alto._norm import ncfold
from app.schemas import LLMLineOutput, LLMResponse


def validate_llm_response(
    raw: dict,
    expected_line_ids: list[str],
    hyphen_pairs: dict[str, str] | None = None,
    ocr_texts: dict[str, str] | None = None,
    hyphen_subs: dict[str, str] | None = None,
) -> LLMResponse:
    """
    Validate an LLM response dict and return a typed LLMResponse.

    Parameters
    ----------
    raw:
        Parsed JSON dict from the LLM.
    expected_line_ids:
        The line IDs the LLM was asked to correct.
    hyphen_pairs:
        Mapping of PART1 line_id → PART2 line_id (and vice-versa).
        When provided, additional hyphen-integrity checks are performed.
    ocr_texts:
        Mapping of line_id → original OCR text.  Used together with
        hyphen_pairs for semantic drift checks on hyphen-pair lines.
    hyphen_subs:
        Mapping of PART1 line_id → subs_content (the expected full word
        for the hyphen pair).  Used for fusion detection: if PART1
        corrected text equals the full word, the LLM illegally merged
        the pair.

    Raises
    ------
    ValueError
        On any validation failure, with a descriptive message.
        Hyphen-integrity violations use message prefix
        "hyphen_integrity_violation".
    """
    # --- Basic structure ---
    if "lines" not in raw:
        raise ValueError("Missing key 'lines' in LLM response")

    lines_raw = raw["lines"]
    if not isinstance(lines_raw, list):
        raise ValueError("'lines' must be a list")

    expected_set = set(expected_line_ids)

    # --- Count ---
    if len(lines_raw) != len(expected_line_ids):
        raise ValueError(
            f"Line count mismatch: expected {len(expected_line_ids)}, got {len(lines_raw)}"
        )

    seen_ids: set[str] = set()
    outputs: list[LLMLineOutput] = []

    for entry in lines_raw:
        if not isinstance(entry, dict):
            raise ValueError(f"Each line entry must be a dict, got {type(entry)}")

        line_id = entry.get("line_id")
        corrected_text = entry.get("corrected_text")

        if not line_id:
            raise ValueError(f"Entry missing 'line_id': {entry}")
        if line_id in seen_ids:
            raise ValueError(f"Duplicate line_id in response: {line_id!r}")
        if line_id not in expected_set:
            raise ValueError(f"Unknown line_id in response: {line_id!r}")

        seen_ids.add(line_id)

        if not isinstance(corrected_text, str) or corrected_text == "":
            raise ValueError(f"corrected_text for {line_id!r} is empty or missing")
        if "\n" in corrected_text or "\r" in corrected_text:
            raise ValueError(f"corrected_text for {line_id!r} contains a newline character")

        outputs.append(LLMLineOutput(line_id=line_id, corrected_text=corrected_text))

    # --- Check all expected IDs are present ---
    missing = expected_set - seen_ids
    if missing:
        raise ValueError(f"Missing line_ids in response: {sorted(missing)}")

    # --- Hyphen integrity ---
    if hyphen_pairs:
        text_by_id = {o.line_id: o.corrected_text for o in outputs}
        _validate_hyphen_integrity(
            text_by_id,
            hyphen_pairs,
            expected_set,
            ocr_texts or {},
            hyphen_subs or {},
        )

    return LLMResponse(lines=outputs)


def _validate_hyphen_integrity(
    text_by_id: dict[str, str],
    hyphen_pairs: dict[str, str],
    chunk_ids: set[str],
    ocr_texts: dict[str, str],
    hyphen_subs: dict[str, str],
) -> None:
    """
    Check that no hyphen-pair line has been illegally merged or shifted.

    hyphen_pairs maps PART1 → PART2 and PART2 → PART1 (both directions).
    We deduplicate via frozenset and check each pair once.

    Checks performed:
    1. Neither side is empty.
    2. PART1 word count didn't grow drastically (text pulled from PART2).
    3. PART2 word count didn't shrink drastically (absorbed by PART1).
    4. PART1 doesn't contain the full logical word (fusion with subs_content).
    """
    checked_pairs: set[frozenset[str]] = set()

    for id_a, id_b in hyphen_pairs.items():
        pair = frozenset({id_a, id_b})
        if pair in checked_pairs:
            continue
        if id_a not in chunk_ids or id_b not in chunk_ids:
            continue
        checked_pairs.add(pair)

        text_a = text_by_id.get(id_a, "")
        text_b = text_by_id.get(id_b, "")

        # 1. Either side being empty means illegal fusion/deletion
        if not text_a:
            raise ValueError(
                f"hyphen_integrity_violation: corrected_text for line {id_a!r} is empty"
            )
        if not text_b:
            raise ValueError(
                f"hyphen_integrity_violation: corrected_text for line {id_b!r} is empty"
            )

        # 2–3. Semantic drift checks (only when OCR source is available)
        ocr_a = ocr_texts.get(id_a, "")
        ocr_b = ocr_texts.get(id_b, "")
        if ocr_a and ocr_b:
            _check_pair_drift(id_a, id_b, text_a, text_b, ocr_a, ocr_b)

    # 4. Fusion check: PART1 contains the full logical word
    for part1_id, subs_content in hyphen_subs.items():
        if not subs_content or part1_id not in text_by_id:
            continue
        part1_text = text_by_id[part1_id]
        # Strip trailing dash and whitespace to get the bare text
        part1_words = part1_text.rstrip().rstrip("-").split()
        if not part1_words:
            continue
        part1_last_word = part1_words[-1]
        if ncfold(part1_last_word) == ncfold(subs_content):
            raise ValueError(
                f"hyphen_integrity_violation: PART1 line {part1_id!r} "
                f"contains full logical word {subs_content!r} "
                f"(fusion detected)"
            )


def _check_pair_drift(
    id_a: str,
    id_b: str,
    text_a: str,
    text_b: str,
    ocr_a: str,
    ocr_b: str,
) -> None:
    """Raise if corrected texts diverge too much from their OCR sources."""
    ocr_a_wc = len(ocr_a.split())
    ocr_b_wc = len(ocr_b.split())
    cor_a_wc = len(text_a.split())
    cor_b_wc = len(text_b.split())

    # PART1 grew by more than 2 words → probably pulled from PART2
    if cor_a_wc > ocr_a_wc + 2:
        raise ValueError(
            f"hyphen_integrity_violation: PART1 line {id_a!r} grew from "
            f"{ocr_a_wc} to {cor_a_wc} words (text migration suspected)"
        )

    # PART2 shrank to less than 40% of original → absorbed by PART1
    if ocr_b_wc >= 2 and cor_b_wc < ocr_b_wc * 0.4:
        raise ValueError(
            f"hyphen_integrity_violation: PART2 line {id_b!r} shrank from "
            f"{ocr_b_wc} to {cor_b_wc} words (text migration suspected)"
        )
