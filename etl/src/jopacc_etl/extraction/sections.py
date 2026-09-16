"""Work out which payment system each page is talking about.

Most pages never name their system. A slide headed simply ``Summary`` sits
inside the CliQ chapter and means CliQ, but the only thing on the page that
says so is a logo, which is an image. The system therefore has to be inherited
from the nearest preceding section divider.

Divider pages do carry their titles as text, which makes them a dependable
anchor. Their positions are not fixed: the 2024 monthly decks use seven
sections and the 2025 layout uses eight. Dividers are found per file rather
than assumed, and a divider that matches nothing known is reported as a
structural change rather than dropped.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class Section:
    key: str
    title: str
    start_page: int          # 1-based, inclusive
    end_page: int            # 1-based, inclusive
    recognised: bool


_STOPWORDS = {"a", "an", "the", "for", "of", "and", "on", "in", "to", "is", "are"}


def _significant_words(phrase: str) -> List[str]:
    words = re.findall(r"[a-z0-9]+", phrase.lower())
    return [w for w in words if w not in _STOPWORDS and len(w) > 1]


def _page_word_set(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _words_present(needle_words: List[str], page_words: set) -> bool:
    """Is every significant word of the match phrase present on the page?

    Native text preserves reading order, so a plain substring check works.
    OCR does not: a divider's title is read as a scattered bag of words in
    whatever order their positions happened to sort into, so 'Instant Payment
    System for' never appears as a contiguous phrase even though every word
    of it is genuinely on the page. Checking word presence rather than word
    order is what makes divider detection work on both.

    Matching allows only simple plural/singular variation ('Payment' against
    a page that has 'Payments'), not arbitrary substring containment. A wider
    containment check looks harmless but is not: 'interoperability' contains
    'in' as a run of letters, and 'in' is one of the commonest words on any
    page, so a naive substring check matches almost every page in the report
    against the interoperability rule alone.
    """
    for needle in needle_words:
        if not any(word == needle or word == needle + "s" or needle == word + "s"
                   for word in page_words):
            return False
    return True


def find_dividers(page_texts: Dict[int, str], cfg: dict) -> List[dict]:
    """Locate section divider pages and name them.

    A divider is a sparse page whose words match one of the configured
    section headings. Sparseness matters: the phrase 'Automated Clearing
    House' also appears in body slides, and only the divider is otherwise
    nearly empty.
    """
    rules = cfg["structure"]["sections"]
    rule_words = [(rule, _significant_words(rule["match"])) for rule in rules]
    found = []
    for page_no, text in sorted(page_texts.items()):
        flat = " ".join(text.split())
        if len(flat) > 240:                     # a content page, not a divider
            continue
        page_words = _page_word_set(flat)
        # A page can satisfy more than one rule at once -- 'Instant Payment
        # Systems in Jordan' contains every word 'Instant Payment System for'
        # (the CliQ rule) needs too. The more specific rule -- the one
        # requiring more words -- is preferred, rather than whichever rule
        # happens to be listed first in the config.
        matches = [(rule, words) for rule, words in rule_words
                  if _words_present(words, page_words)]
        if matches:
            rule, _ = max(matches, key=lambda m: len(m[1]))
            found.append({"page": page_no, "key": rule["key"], "title": flat[:120]})
    return found


def build_sections(page_texts: Dict[int, str], page_count: int,
                   cfg: dict) -> tuple[List[Section], List[str]]:
    """Return the section map for one report, plus any structural warnings."""
    dividers = find_dividers(page_texts, cfg)
    warnings: List[str] = []

    if not dividers:
        warnings.append("no section dividers found; whole report treated as one section")
        return [Section("unsectioned", "", 1, page_count, False)], warnings

    sections: List[Section] = []
    if dividers[0]["page"] > 1:
        sections.append(Section("front_matter", "Cover and contents", 1,
                                dividers[0]["page"] - 1, True))

    for index, divider in enumerate(dividers):
        end = dividers[index + 1]["page"] - 1 if index + 1 < len(dividers) else page_count
        sections.append(Section(divider["key"], divider["title"], divider["page"], end, True))

    known = {rule["key"] for rule in cfg["structure"]["sections"]}
    seen = {s.key for s in sections} - {"front_matter"}
    missing = known - seen
    if missing:
        warnings.append(
            "sections absent from this report: " + ", ".join(sorted(missing)))
    log.debug("mapped %d sections across %d pages", len(sections), page_count)
    return sections, warnings


def section_for_page(sections: List[Section], page_no: int) -> Optional[Section]:
    for section in sections:
        if section.start_page <= page_no <= section.end_page:
            return section
    return None
