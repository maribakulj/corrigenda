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
   `ValidationError`, `CorrectionAborted`, and — since 0.9.0 — the
   provider branch `ProviderError` → `ProviderTransientError` /
   `ProviderPermanentError`, which historically inherited bare
   `Exception` and escaped an `except CorrectionError` catch-all).
   Value-shaped errors also inherit `ValueError` for backwards
   compatibility; provider errors deliberately do NOT (the retry
   classifier routes `ValueError` to the malformed-output branch).
   Parser entry points wrap lxml/OS/ValueError leaks into `ParseError`
   (`classified_parse_errors`), so hostile input can never escape
   unclassified — pinned by the fuzz suite. Every class carries a stable
   machine `code` and a `retryable` flag; hosts route on those, never on
   message text. Severity is NOT encoded in the hierarchy: the fatality
   of `ProviderPermanentError` is enforced by the pipeline's explicit
   `except ProviderPermanentError: raise` handlers ordered before every
   absorbing branch — pinned by `tests/test_error_taxonomy.py`.
2. **Recoverable vs fatal on the chunk path**:
   - malformed producer output / transient transport → retry, then
     granularity descent, then OCR fallback (never silent: events +
     traces record every fallback);
   - permanent provider rejection (401/403/404) → fatal for the run —
     it would hit every chunk identically and "succeed" with
     uncorrected text;
   - anything else → fail the run. Recoverability is an ALLOWLIST
     (`ProviderTransientError` + the `ValueError` family) — revised in
     0.9.0 from the historical denylist of eight programmer-bug types,
     under which an unknown exception (a `RuntimeError`, an unwrapped
     SDK error) degraded every chunk to OCR fallback while the run
     reported success. The provider-agnostic pipeline still cannot name
     raw SDK transport errors — which is exactly why wrapping them as
     `ProviderTransientError` is the provider CONTRACT, enforced by
     failing loudly instead of guessing.
3. **Cancellation is cooperative** (`CorrectionAborted`), observed at
   chunk/page boundaries, and no output is written after it.

## Consequences
Consumers catch `CorrectionError` once. A crash in the field is by
definition a library bug, not an input problem. The OCR-fallback path
can absorb only *recoverable domain* errors — a broken invariant
surfaces instead of being masked as uncorrected text.
