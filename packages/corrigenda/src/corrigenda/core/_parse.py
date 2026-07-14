"""Tolerant integer parsing shared across format parsers.

Heritage XML producers emit coordinates and indices as floats
(``HPOS="123.0"``), as blanks (``WIDTH=""``), or occasionally as garbage.
Every format parser needs the same policy: a blank/missing value becomes a
default, a float truncates toward zero (spec F5), and a genuinely
non-numeric value either raises or defaults depending on the call site.

Centralising it here keeps the four former copies from drifting — one
already had: ``page/_text._index_of`` used ``int(raw)`` (not
``int(float(raw))``), so a float-valued ``@index`` aborted to 0 where the
ALTO geometry policy truncates it.

Pure core: no lxml, no format import — the import-contract test keeps it so.
"""

from __future__ import annotations


def parse_int_tolerant(
    raw: str | None, default: int = 0, *, strict: bool = False
) -> int:
    """Parse ``raw`` to ``int``, truncating floats toward zero (spec F5).

    ``None`` or ``""`` returns ``default``. A float-shaped string truncates
    toward zero (``"12.9" → 12``, ``"-1.9" → -1``). A genuinely non-numeric
    string (``"abc"``) returns ``default`` when ``strict`` is ``False`` (the
    PAGE tolerance — skip and move on) or re-raises ``ValueError`` when
    ``strict`` is ``True`` (the ALTO geometry policy — a non-numeric
    coordinate is a real error worth surfacing, not silently zeroed).

    Audit-F7 — an infinity/overflow-shaped value (``"inf"``, ``"1e999"``)
    passes ``float()`` but ``int(inf)`` raises ``OverflowError``, which
    used to escape the ``except ValueError`` and crash the whole parse.
    Such values now follow the SAME policy as non-numeric ones: default
    in tolerant mode, ``ValueError`` (the promised class) in strict mode.
    """
    if raw is None or raw == "":
        return default
    try:
        return int(float(raw))
    except (ValueError, OverflowError) as exc:
        if strict:
            if isinstance(exc, OverflowError):
                raise ValueError(f"non-finite integer value: {raw!r}") from exc
            raise
        return default


__all__ = ["parse_int_tolerant"]
