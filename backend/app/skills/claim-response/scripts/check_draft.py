#!/usr/bin/env python3
"""Check a claim-response draft: citations, quotes, sentence length and banned phrases.

Usage: python3 -I -S check_draft.py DRAFT.txt   (exit 0 when the draft passes, 1 with the errors)
Only the standard library is used, so the same file runs inside Thoth and in Claude Code.
"""

from __future__ import annotations

import re
import sys

MAX_WORDS = 20
CITATION = re.compile(r"\(E\d+(?:,\s*E\d+)*\)")  # (E41233) or (E41233, E41502)
ENTRY = re.compile(r"E(\d+)")
QUOTE = re.compile(r"\[\[quote:\d+\]\]")
SENTENCE_END = re.compile(r"(?<=[.?!])\s+(?=[A-Z\[(\-\"'])|(?<=[.?!])$")
SALUTATION = re.compile(r"^(hi|hello|dear|good (morning|afternoon|evening))\b[^\n]{0,60}$", re.I)
SCOPE_SENTENCE = re.compile(
    r"^(I|We) (checked|searched|looked)\b|\bfrom .+? to .+?\b|\bbetween .+? and .+?\b", re.I
)
ABBREVIATIONS = ("Mr.", "Mrs.", "Ms.", "Dr.", "Prof.", "e.g.", "i.e.", "etc.", "vs.", "No.", "St.")
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
# sentences without a citation may only frame, ask, propose or give the writer's own account
ALLOWED_OPENERS = (
    "i want",
    "i think",
    "i would like",
    "i'd like",
    "i hope",
    "i attach",
    "i propose",
    "i suggest",
    "i checked",
    "i searched",
    "i looked",
    "i prepared",
    "i planned",
    "i understood",
    "i expected",
    "i assumed",
    "i am ",
    "i'm ",
    "i cannot find",
    "i could not find",
    "my note",
    "can we",
    "could we",
    "shall we",
    "for the next",
    "then",
    "please",
    "if ",
    "let's",
    "lets",
    "thank",
    "thanks",
    "kind regards",
    "best regards",
    "regards",
)
SECTION_RECORDS = "what the records show"
SECTION_GAPS = "what i cannot find"
SECTION_QUESTIONS = "questions"
SECTIONS = (SECTION_RECORDS, SECTION_GAPS, SECTION_QUESTIONS)


def cited_entries(text: str) -> list[int]:
    """Every entry number cited as (E41233) or (E41233, E41502), in order of appearance."""
    return [int(n) for group in CITATION.findall(text) for n in ENTRY.findall(group)]


def protect(text: str) -> str:
    """Abbreviations lose their sentence-ending dot, so `Mr. Smith` stays one sentence."""
    for abbreviation in ABBREVIATIONS:
        text = text.replace(abbreviation, abbreviation[:-1] + "․")
    return text


def strip_placeholders(text: str) -> str:
    """Quotes become one neutral word; citations disappear, so neither counts as words."""
    return CITATION.sub("", QUOTE.sub("QUOTE", text))


def words(sentence: str) -> int:
    return len([w for w in strip_placeholders(sentence).split() if re.search(r"\w", w)])


def sentences(text: str) -> list[str]:
    return [p.strip() for p in SENTENCE_END.split(text) if p and p.strip()]


def banned_in(sentence: str) -> list[str]:
    found = []
    for phrase in BANNED:
        for match in re.finditer(r"(?<![\w'])" + re.escape(phrase) + r"(?![\w'])", sentence, re.I):
            word = match.group(0)
            if (
                " " not in phrase
                and match.start() > 0
                and word[:1].isupper()
                and word[1:].islower()
            ):
                continue  # a capitalised word inside a sentence is a name (Sue, Bill)
            found.append(phrase)
            break
    return found


def check_draft(text: str) -> list[str]:
    errors: list[str] = []
    section = ""
    gaps_without_scope: list[int] = []
    gaps_scope = False

    def close_gaps() -> None:
        if gaps_without_scope and not gaps_scope:
            for number in gaps_without_scope:
                errors.append(
                    f"line {number}: a gap needs its search scope: dates and terms in parentheses, "
                    "or a sentence in this section such as 'I checked my records from … to …'"
                )
        gaps_without_scope.clear()

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        lowered = line.lower().rstrip(":")
        if lowered in SECTIONS:
            if section == SECTION_GAPS:
                close_gaps()
            section = lowered
            continue
        if line.startswith(("DRAFT", "BRIEF")) or SALUTATION.match(line):
            continue
        is_bullet = line.startswith("- ")
        body = protect(line[2:] if is_bullet else line)
        for sentence in sentences(body):
            count = words(sentence)
            if count > MAX_WORDS:
                errors.append(f"line {number}: sentence over 20 words ({count}): {sentence[:60]}…")
            for phrase in banned_in(sentence):
                errors.append(f"line {number}: banned phrase '{phrase}': {sentence[:60]}…")
        if section == SECTION_RECORDS and is_bullet:
            if not CITATION.search(body):
                errors.append(f"line {number}: a record bullet needs a citation such as (E41233)")
            continue
        if section == SECTION_GAPS:
            if is_bullet:
                if not re.search(r"\([^()]+\)\s*$", body):
                    gaps_without_scope.append(number)
                continue
            if SCOPE_SENTENCE.search(body):
                gaps_scope = True
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
                f"framing, proposals and your own account may go uncited): {sentence[:60]}…"
            )
    if section == SECTION_GAPS:
        close_gaps()
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
