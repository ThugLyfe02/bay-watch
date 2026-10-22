"""Strict identity checks for collectible comps: fuzzy titles alone can misprice variants."""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class CollectibleIdentity:
    card_number: str | None
    grading_company: str | None
    grade: str | None
    language: str | None
    edition: str | None
    variant: str | None


def extract_identity(title: str) -> CollectibleIdentity:
    s = title.casefold()
    m = re.search(r"#\s*(\d{1,4})\b|\b(\d{1,4})\s*/\s*\d{1,4}\b", s)
    g = re.search(r"\b(psa|bgs|beckett|cgc|sgc)\s*[-:#]?\s*(\d{1,2}(?:\.\d)?)\b", s)
    if g and g.group(1) == "beckett": company = "bgs"
    else: company = g.group(1) if g else None
    grade = g.group(2) if g else None
    language = "japanese" if re.search(r"\b(japanese|japan|jpn)\b", s) else "english" if re.search(r"\b(english|eng)\b", s) else None
    edition = "first" if re.search(r"\b(1st|first edition)\b", s) else "unlimited" if "unlimited" in s else None
    variant = "reverse_holo" if re.search(r"reverse[ -]?holo", s) else "non_holo" if re.search(r"\bnon[ -]?holo\b", s) else "holo" if re.search(r"\bholo(?:graphic)?\b", s) else None
    return CollectibleIdentity((m.group(1) or m.group(2)) if m else None, company, grade, language, edition, variant)


def compatible_titles(target: str, comp: str) -> bool:
    """Reject known cross-variant comparisons; unknown comp fields stay usable but lower confidence."""
    a, b = extract_identity(target), extract_identity(comp)
    for field in ("card_number", "grading_company", "grade", "language", "edition", "variant"):
        av, bv = getattr(a, field), getattr(b, field)
        if av is not None and bv is not None and av != bv:
            return False
    return True

