#!/usr/bin/env python3
"""Check a claim-response draft: citations, quotes, sentence length and banned phrases.

Usage: python3 check_draft.py DRAFT.txt   (exit 0 when the draft passes, 1 with the errors)
Only the standard library is used, so the same file runs inside Thoth and in Claude Code.
"""

from __future__ import annotations

import re
import sys

MAX_WORDS = 20
CITATION = re.compile(r"\(E\d+(?:,\s*E\d+)*\)")
QUOTE = re.compile(r"\[\[quote:\d+\]\]")
SENTENCE_END = re.compile(r"(?<=[.?!])\s+(?=[A-Z\[(\-\"'])|(?<=[.?!])$")
BANNED = [
    "stupid",
    "ridiculous",
    "pathetic",
    "or else",
    "last warning",
    "you had better",
    "your fault",
    "you failed",
    "you ignored",
    "you hide",
    "you hid",
    "hidden",
    "lie",
    "lied",
    "liar",
    "dishonest",
    "fraud",
    "breach",
    "bad faith",
    "negligence",
    "liable",
    "liability",
    "damages",
    "lawyer",
    "sue",
    "legal action",
    "always",
    "never",
    "every time",
    "nobody",
    "everyone knows",
    "obviously",
    "clearly",
    "as i said",
    "as i told you",
    "that is not true",
    "not true",
    "you agreed to this",
    "you changed your mind",
    "unacceptable",
]
# sentences without a citation may only frame, ask or propose
ALLOWED_OPENERS = (
    "i want",
    "i think",
    "i would like",
    "i'd like",
    "i hope",
    "i attach",
    "i propose",
    "i suggest",
    "can we",
    "could we",
    "shall we",
    "for the next",
    "then",
    "please",
    "if ",
    "let's",
    "lets",
    "hi ",
    "hi,",
    "hello",
    "dear",
    "thank",
    "thanks",
    "kind regards",
    "best regards",
    "regards",
    "my note",
    "i cannot find",
    "i could not find",
    "i am ",
    "i'm ",
)
SECTION_RECORDS = "what the records show"
SECTION_GAPS = "what i cannot find"
SECTION_QUESTIONS = "questions"


def strip_placeholders(text: str) -> str:
    """Quotes become one neutral word; citations disappear, so neither counts as words."""
    return CITATION.sub("", QUOTE.sub("QUOTE", text))


def words(sentence: str) -> int:
    return len([w for w in strip_placeholders(sentence).split() if re.search(r"\w", w)])


def sentences(text: str) -> list[str]:
    parts = [p.strip() for p in SENTENCE_END.split(text) if p and p.strip()]
    return parts


def banned_in(sentence: str) -> list[str]:
    lowered = " " + re.sub(r"[^\w' ]+", " ", sentence.lower()) + " "
    found = []
    for phrase in BANNED:
        if " " + phrase + " " in lowered:
            found.append(phrase)
    return found


def check_draft(text: str) -> list[str]:
    errors: list[str] = []
    section = ""
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        lowered = line.lower().rstrip(":")
        if lowered in (SECTION_RECORDS, SECTION_GAPS, SECTION_QUESTIONS):
            section = lowered
            continue
        if line.startswith(("DRAFT", "BRIEF")):
            continue
        is_bullet = line.startswith("- ")
        body = line[2:] if is_bullet else line
        for sentence in sentences(body):
            count = words(sentence)
            if count > MAX_WORDS:
                errors.append(f"line {number}: sentence over 20 words ({count}): {sentence[:60]}…")
            for phrase in banned_in(sentence):
                errors.append(f"line {number}: banned phrase '{phrase}': {sentence[:60]}…")
        has_citation = bool(CITATION.search(body)) or bool(QUOTE.search(body))
        if section == SECTION_RECORDS and is_bullet:
            if not CITATION.search(body):
                errors.append(f"line {number}: a record bullet needs a citation such as (E41233)")
            continue
        if section == SECTION_GAPS and is_bullet:
            if not re.search(r"\([^()]+\)\s*$", body):
                errors.append(
                    f"line {number}: a gap needs its search scope in parentheses: dates and terms"
                )
            continue
        if section == SECTION_QUESTIONS and is_bullet:
            if not body.rstrip().endswith("?"):
                errors.append(f"line {number}: a question must end with a question mark")
            continue
        for sentence in sentences(body):
            if CITATION.search(sentence) or QUOTE.search(sentence):
                continue
            if sentence.rstrip().endswith("?"):
                continue
            if sentence.lower().startswith(ALLOWED_OPENERS):
                continue
            errors.append(
                f"line {number}: a factual sentence without a citation (only questions, "
                f"framing and proposals may go uncited): {sentence[:60]}…"
            )
        del has_citation
    return errors


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: check_draft.py DRAFT.txt")
        return 2
    with open(argv[0], encoding="utf-8") as handle:
        errors = check_draft(handle.read())
    for error in errors:
        print(error)
    print("OK: the draft passes" if not errors else f"FAILED: {len(errors)} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
