# ADR-008 — Error taxonomy: one classified root; bugs fail, bad input degrades

Status: accepted (2026-07)

## Context
A correction run faces four very different failure families: hostile or
malformed *input*, misbehaving *producers* (LLM output), *transport*
flakiness, and genuine *programming errors*. Treating them uniformly
either crashes runs on recoverable noise or — far worse — lets a bug
degrade every line to OCR fallback while the run still reports success.

## Decision
1. **Single classified root** (spec §8.4): everything the library raises
   derives from `CorrectionError` (`ParseError`, `DuplicateIdError`,
   `ValidationError`, `CorrectionAborted`). Value-shaped errors also
   inherit `ValueError` for backwards compatibility. Parser entry points
   wrap lxml/OS/ValueError leaks into `ParseError`
   (`classified_parse_errors`), so hostile input can never escape
   unclassified — pinned by the fuzz suite.
2. **Recoverable vs fatal on the chunk path**:
   - malformed producer output / transient transport → retry, then
     granularity descent, then OCR fallback (never silent: events +
     traces record every fallback);
   - permanent provider rejection (401/403/404) → fatal for the run —
     it would hit every chunk identically and "succeed" with
     uncorrected text;
   - programming-error types (TypeError, AttributeError, KeyError, …)
     → fail the run. A denylist rather than an allowlist: the
     provider-agnostic pipeline cannot name every SDK's transport
     errors, which must stay recoverable.
3. **Cancellation is cooperative** (`CorrectionAborted`), observed at
   chunk/page boundaries, and no output is written after it.

## Consequences
Consumers catch `CorrectionError` once. A crash in the field is by
definition a library bug, not an input problem. The OCR-fallback path
can absorb only *recoverable domain* errors — a broken invariant
surfaces instead of being masked as uncorrected text.
