"""The evidence gate. `02-contracts-and-trust.md` section 2.

Every filled slot carries a quoted substring that must appear **verbatim** in
the source text. Not found -> drop the value and treat the slot as unfilled.

This is the anti-hallucination gate, and it is deliberately dumb: a substring
check is cheap, has no model in the loop, and cannot be argued with. A value the
LLM invented has no span to point at.
"""

import re

_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Collapse runs of whitespace. Nothing else."""
    return _WHITESPACE.sub(" ", (text or "")).strip()


#: Quote characters a model may wrap its span in. Straight and curly.
_QUOTES = "\"'“”‘’«»"


def unwrap_quotes(span: str) -> str:
    """Strip one balanced pair of surrounding quote characters.

    Asked to quote a substring, models often return the substring *inside
    quotation marks*: `'"Account tier should be Standard"'` for text that reads
    `Account tier should be Standard`. The value is properly grounded; only the
    packaging differs, and it varies between runs of the same prompt.

    Stripping the wrapper does not weaken the gate -- every character of the
    inner text still has to appear verbatim.
    """
    text = (span or "").strip()
    while len(text) >= 2 and text[0] in _QUOTES and text[-1] in _QUOTES:
        text = text[1:-1].strip()
    return text


def evidence_supports(source_text: str, span: str) -> bool:
    """Does `span` actually appear in `source_text`?

    Three attempts, each weaker only in packaging, never in substance:

    1. exact substring;
    2. with surrounding quote characters stripped;
    3. with whitespace collapsed on both sides -- ticket text arrives with line
       wraps and ragged spacing, and a model quoting across a line break is
       quoting honestly.

    None of these lets a paraphrase through: every character apart from
    whitespace and the outer quotes still has to match, in order.
    """
    if not span:
        return False
    if span in source_text:
        return True

    inner = unwrap_quotes(span)
    if not inner:
        return False
    if inner in source_text:
        return True
    return normalise(inner) in normalise(source_text)


def locate(source_text: str, span: str):
    """Character offsets of `span` in `source_text`, or None.

    Offsets are into the original text, so a caller can show the user exactly
    what DA read. Only the exact match can be located; a whitespace-normalised
    match is accepted by `evidence_supports` but has no single true offset.
    """
    if not span:
        return None
    index = source_text.find(span)
    if index < 0:
        return None
    return index, index + len(span)
