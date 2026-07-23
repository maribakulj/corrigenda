#!/usr/bin/env python3
"""Quality-estimation training-data generator (ROADMAP V3 Phase 2 → 3).

Produces token-labeled data for the Phase 3 QE scorer — the model that
answers the one question the calibration harness proved is missing: *is
this line/token already correct, or does it still carry an OCR error?*
A token's label is ``1`` when it carries an error, ``0`` when it is
clean.

Two complementary sources, both emitted as the same JSONL record shape
(``{case, line_id, tokens, labels, source}``):

* **real** — align a page's RAW OCR against its HUMAN-corrected
  reference (the OCR17+ pairs) with the Phase 1 token aligner; a token
  that the alignment could not match to an identical reference token is
  a real error (label 1). Gold labels, but scarce.
* **synthetic** — take the clean reference and apply scripted,
  documented ``clean → OCR`` degradations (long-s, ligatures, the m/rn
  and u/n confusions). Deterministic (a per-token hash gate, no RNG, so
  the same seed reproduces the same data offline), and unbounded — the
  degraded token is labeled 1, the untouched ones 0.

The two mix into one training set; ``--stats`` prints the label balance
and per-rule counts a data card should cite. This is TRAINING-TIME
tooling (repo ``scripts/``), never imported by the pixel-light core.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "packages" / "corrigenda" / "src"))

import corrigenda  # noqa: E402
from corrigenda.core.alignment import align_tokens  # noqa: E402

DEFAULT_CORPUS = (
    _REPO_ROOT / "packages" / "corrigenda" / "tests" / "corpus_gt" / "manifest.json"
)


@dataclass(frozen=True)
class Degradation:
    """One ``clean → OCR`` substitution rule.

    ``clean`` is the substring as it appears in modern/corrected text;
    ``ocr`` is the plausible OCR misreading a scripted degradation
    injects. The DIRECTION is the opposite of
    ``corrigenda.core.confidence.DEFAULT_CONFUSIONS`` (which maps the
    OCR form back to the correct one) — kept as its own curated,
    UNI-directional table so the generated errors are realistic, not
    every bidirectional pair fired blindly.
    """

    clean: str
    ocr: str
    name: str


#: Curated clean→OCR degradations for 17th–18th c. French print.
DEGRADATIONS: tuple[Degradation, ...] = (
    Degradation("s", "ſ", "long_s"),
    Degradation("fi", "ﬁ", "fi_ligature"),
    Degradation("fl", "ﬂ", "fl_ligature"),
    Degradation("m", "rn", "m_to_rn"),
    Degradation("u", "n", "u_to_n"),
    Degradation("n", "u", "n_to_u"),
)


def _gate(seed: int, key: str, rate_percent: int) -> bool:
    """Deterministic per-token gate: True ~``rate_percent``% of the time.

    A hash of ``seed:key`` replaces an RNG so the same seed reproduces
    the exact dataset offline (the pipeline's own no-``random`` rule,
    applied to tooling for reproducible data cards)."""
    digest = hashlib.sha256(f"{seed}:{key}".encode()).hexdigest()
    return int(digest[:8], 16) % 100 < rate_percent


def degrade_token(token: str, *, seed: int, index: int, rate_percent: int) -> str:
    """Apply at most ONE gated degradation to ``token`` (first rule that
    both matches and passes the per-(token, rule) gate)."""
    for rule in DEGRADATIONS:
        if rule.clean in token and _gate(
            seed, f"{index}:{rule.name}:{token}", rate_percent
        ):
            return token.replace(rule.clean, rule.ocr, 1)
    return token


def synthetic_labels(
    reference_text: str, *, seed: int, index: int, rate_percent: int
) -> tuple[list[str], list[int]]:
    """Degrade a clean line into (tokens, labels): 1 where a token was
    changed, 0 where it was left clean."""
    tokens: list[str] = []
    labels: list[int] = []
    for tok in reference_text.split():
        degraded = degrade_token(tok, seed=seed, index=index, rate_percent=rate_percent)
        tokens.append(degraded)
        labels.append(1 if degraded != tok else 0)
    return tokens, labels


def real_labels(raw_text: str, ref_text: str) -> tuple[list[str], list[int]]:
    """Label a raw OCR line against its reference with the token aligner:
    a raw token matched to an IDENTICAL reference token is clean (0);
    anything else (mismatch or a raw token the reference lacks) is a
    real OCR error (1)."""
    raw_tokens = raw_text.split()
    ref_tokens = ref_text.split()
    alignment = align_tokens(raw_tokens, ref_tokens)
    clean_indices: set[int] = set()
    for pair in alignment.pairs:
        if pair.source_index is None or pair.target_index is None:
            continue
        if raw_tokens[pair.source_index] == ref_tokens[pair.target_index]:
            clean_indices.add(pair.source_index)
    labels = [0 if i in clean_indices else 1 for i in range(len(raw_tokens))]
    return raw_tokens, labels


def _line_texts(path: Path) -> dict[str, str]:
    document = corrigenda.load(path)
    return {
        lm.line_id: lm.ocr_text for page in document.manifest.pages for lm in page.lines
    }


def generate(
    manifest_path: Path, *, mode: str, seed: int, rate_percent: int
) -> list[dict]:
    """Produce the JSONL records for every case in the manifest."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    case_dir = manifest_path.parent
    records: list[dict] = []
    for case in manifest["cases"]:
        ref_texts = _line_texts(case_dir / case["reference"])
        raw_texts = _line_texts(case_dir / case["source"])
        for idx, (line_id, ref) in enumerate(ref_texts.items()):
            if mode in ("real", "both"):
                raw = raw_texts.get(line_id)
                if raw is not None:
                    tokens, labels = real_labels(raw, ref)
                    records.append(
                        {
                            "case": case["name"],
                            "line_id": line_id,
                            "tokens": tokens,
                            "labels": labels,
                            "source": "real",
                        }
                    )
            if mode in ("synthetic", "both"):
                tokens, labels = synthetic_labels(
                    ref, seed=seed, index=idx, rate_percent=rate_percent
                )
                records.append(
                    {
                        "case": case["name"],
                        "line_id": line_id,
                        "tokens": tokens,
                        "labels": labels,
                        "source": "synthetic",
                    }
                )
    return records


def stats(records: list[dict]) -> dict:
    """Label balance + per-source token counts a data card should cite."""
    total = sum(len(r["labels"]) for r in records)
    errors = sum(sum(r["labels"]) for r in records)
    by_source: dict[str, dict[str, int]] = {}
    for r in records:
        bucket = by_source.setdefault(r["source"], {"tokens": 0, "errors": 0})
        bucket["tokens"] += len(r["labels"])
        bucket["errors"] += sum(r["labels"])
    return {
        "records": len(records),
        "tokens": total,
        "errors": errors,
        "error_rate": round(errors / total, 4) if total else 0.0,
        "by_source": by_source,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--mode", choices=["real", "synthetic", "both"], default="both")
    parser.add_argument("--seed", type=int, default=1637)
    parser.add_argument(
        "--rate", type=int, default=35, help="synthetic degradation rate (percent)"
    )
    parser.add_argument("--out", type=Path, default=None, help="JSONL output path")
    parser.add_argument("--stats", action="store_true", help="print stats, not data")
    args = parser.parse_args(argv)

    records = generate(
        args.corpus, mode=args.mode, seed=args.seed, rate_percent=args.rate
    )

    if args.stats:
        print(json.dumps(stats(records), indent=2, ensure_ascii=False))
        return 0

    lines = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(lines + "\n", encoding="utf-8")
    else:
        print(lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
