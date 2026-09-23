#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
scorecard — the four things that were wrong with the letter, counted.

Reading two letters side by side and deciding which is better is a judgement that drifts, and
it drifts in the direction of whichever you read second. These are the specific failures the
opencode path had against the claude one, each reduced to a number so a sampler change or a
prompt change can be attributed rather than felt:

  person       the letter came back in the third person ("David spun up a server"), as a
               briefing about a subject, where it should be the clinician addressing you
  grounding    names, dates and numbers per thousand words — the generic read has almost
               none, because it wrote the genre rather than the week
  quoting      spans that actually appear in the source diff. A read that quotes you back is
               a read that opened the file; one that never does could have been written from
               the prompt alone
  length       the good letter is ~1400 words; the flat one was ~800
  repetition   the longest phrase said more than twice, and how often. The flat letter finds
               one rhetorical figure and reaches for it every paragraph
  cadence      mean and longest sentence, and mean paragraph, in words. The letter is the half
               that gets narrated, and a clause a narrator cannot land is a clause a listener
               does not receive. Added after a sweep in which the best *analysis* produced a
               letter averaging forty-word sentences in two-hundred-and-sixty-word paragraphs
               against a known-good's sixteen and seventy-nine — a difference no dimension in
               the rubric was counting and a reader hears immediately

None of these is quality. All of them are necessary conditions for it, and unlike quality they
do not move depending on what you read first.

Usage:
    ./scorecard.py LETTER [--diff microlite.md] [--against KNOWN-GOOD.md]
"""

from __future__ import annotations
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

# Front-matter is the archive's, not the letter's, and the narrator strips it too.
FRONT = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
WORD = re.compile(r"[A-Za-z']+")
MONTHS = (r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
          r"Aug(?:ust)?|Sep(?:t|tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?")
DATE = re.compile(rf"\b(?:{MONTHS})\.?\s+\d{{1,2}}\b|\b\d{{1,2}}\s+(?:{MONTHS})\b"
                  rf"|\b\d{{4}}-\d{{2}}-\d{{2}}\b|\bthe \d{{1,2}}(?:st|nd|rd|th)\b"
                  rf"|\b(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day\b")
NUMBER = re.compile(r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
                    r"twenty|thirty|forty|fifty|sixty|seventy|hundred|thousand|\d+)\b", re.I)
# What a quotation looks like in these documents: italics, or quote marks.
QUOTED = re.compile(r"\*([^*\n]{25,400})\*|[\"“]([^\"”\n]{25,400})[\"”]")


def body(path: Path) -> str:
    return FRONT.sub("", path.read_text()).strip()


def normalise(text: str) -> str:
    """Lowercased words only, so a quotation still matches across punctuation and markup."""
    return " ".join(WORD.findall(text.lower()))


def proper_nouns(text: str) -> int:
    """Capitalised words that are not merely opening a sentence, a line or a bullet.

    Done by position rather than by lookbehind: this corpus is full of markdown bullets and
    short declaratives, and a regex that only skips "capital after full stop" counts every one
    of them -- which scores the list-shaped read as the more specific of the two, exactly
    backwards. Sentence-initial words are dropped wholesale instead, which undercounts a real
    name that happens to start a sentence and is the error worth having.
    """
    total = 0
    for line in text.split("\n"):
        line = re.sub(r"\A[\s>*\-+#\d.)]+", "", line)          # bullet and heading furniture
        for clause in re.split(r"(?<=[.!?:;])\s+", line):
            words = WORD.findall(clause)
            total += sum(1 for w in words[1:] if w[0].isupper() and len(w) > 2)
    return total


def person(text: str) -> tuple[str, dict[str, int]]:
    counts = {
        "first": len(re.findall(r"\b(?:I|I'm|I've|I'd|my|me|myself)\b", text)),
        "second": len(re.findall(r"\b(?:you|you're|you've|your|yours|yourself)\b", text, re.I)),
        # The failure worth catching: a letter written *about* a subject rather than to one.
        # Third parties legitimately appear in any week, so read this against the other two
        # rather than on its own -- it is the ratio that says which shape the letter took.
        "third": len(re.findall(r"\b(?:he|him|his|she|her|hers)\b", text, re.I)),
    }
    top = max(counts, key=counts.get)
    return (top if counts[top] else "none"), counts


def quoting(text: str, diff: str | None) -> tuple[int, int]:
    """Quoted spans, and how many of them are really in the source."""
    spans = [m.group(1) or m.group(2) for m in QUOTED.finditer(text)]
    if diff is None:
        return len(spans), -1
    hay = normalise(diff)
    return len(spans), sum(1 for s in spans if normalise(s) and normalise(s) in hay)


def repetition(text: str, n: int = 5) -> tuple[str, int]:
    """The longest n-gram said more than twice — the shape of a model stuck on one figure."""
    words = normalise(text).split()
    for size in range(12, n - 1, -1):
        grams = Counter(" ".join(words[i:i + size]) for i in range(len(words) - size + 1))
        gram, count = (grams.most_common(1) or [("", 0)])[0]
        if count > 2:
            return gram, count
    return "", 0


def cadence(text: str) -> dict[str, float]:
    """How long the sentences and paragraphs run, in words.

    Bold headings on their own line are the letter's only furniture and they are not prose, so
    they are dropped before the paragraphs are measured; leaving them in halves the mean by
    counting a three-word heading as a paragraph. Fragments under four words are dropped from
    the sentence side for the same reason -- an abbreviation or an initial splits a sentence
    without shortening it, and the statistic worth having is the one a narrator has to breathe
    through.
    """
    prose = "\n".join(l for l in text.split("\n")
                      if not re.fullmatch(r"\s*(?:#{1,6}\s+.*|\*\*[^*]+\*\*|-{3,})\s*", l))
    lens = [len(WORD.findall(s)) for s in re.split(r"(?<=[.!?])\s+", prose)]
    lens = [n for n in lens if n > 3]
    paras = [len(WORD.findall(p)) for p in re.split(r"\n\s*\n", prose)]
    paras = [n for n in paras if n > 25]
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else 0.0
    return {"sentence_mean": avg(lens), "sentence_max": max(lens, default=0),
            "paragraph_mean": avg(paras)}


def score(path: Path, diff: str | None, raw: bool = False) -> dict:
    text = body(path)
    words = WORD.findall(text)
    n = len(words) or 1
    top, counts = person(text)
    spans, found = quoting(text, diff)
    gram, repeats = repetition(text)
    beat = cadence(text)
    per_k = lambda c: round(c * 1000 / n, 1)
    if raw:
        # The same measurements, as numbers rather than as sentences. The printed table says
        # "second (I/my 22 · you/your 112 · he/she 23)" because that is what a person reads;
        # a sweep wants to subtract one run's numbers from another's, and cannot subtract that.
        return {
            "words": n,
            "person": top,
            "first": counts["first"], "second": counts["second"], "third": counts["third"],
            "names_per_k": per_k(proper_nouns(text)),
            "dates_per_k": per_k(len(DATE.findall(text))),
            "numbers_per_k": per_k(len(NUMBER.findall(text))),
            "quotations": spans,
            "quotations_in_diff": found,
            "repeated_gram": gram,
            "repeats": repeats,
            **beat,
        }
    return {
        "words": n,
        "person": f"{top}  (I/my {counts['first']} · you/your {counts['second']} "
                  f"· he/she {counts['third']})",
        "names /1k": per_k(proper_nouns(text)),
        "dates /1k": per_k(len(DATE.findall(text))),
        "numbers /1k": per_k(len(NUMBER.findall(text))),
        "quotations": f"{spans}" + (f" ({found} found in the diff)" if found >= 0 else ""),
        "most repeated": f'"{gram}" ×{repeats}' if repeats else "nothing said more than twice",
        "sentence words": f"{beat['sentence_mean']} mean, {beat['sentence_max']} longest",
        "paragraph words": f"{beat['paragraph_mean']} mean",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("letter", type=Path)
    ap.add_argument("--diff", type=Path,
                    help="the week's microlite.md, to check quotations against")
    ap.add_argument("--against", type=Path,
                    help="a known-good letter to print beside it")
    ap.add_argument("--json", action="store_true",
                    help="the raw counts, for a sweep to diff rather than a person to read")
    args = ap.parse_args()

    for p in (args.letter, args.diff, args.against):
        if p and not p.exists():
            sys.exit(f"no such file: {p}")
    diff = args.diff.read_text() if args.diff else None

    paths = [args.letter] + ([args.against] if args.against else [])
    if args.json:
        out = {str(p): score(p, diff, raw=True) for p in paths}
        print(json.dumps(out, indent=2))
        return
    rows = [score(p, diff) for p in paths]
    # Both columns are usually named claude-output.md, so the session they came from is the
    # part worth printing -- the file name alone would label them identically.
    heads = [f"{p.parent.name}/{p.name}" for p in paths]
    keys = list(rows[0])
    label = max(len(k) for k in keys)
    col = max([len(h) for h in heads]
              + [len(str(r[k])) for r in rows for k in keys]) + 2
    print(f"{'':<{label}}  " + "".join(f"{h:<{col}}" for h in heads).rstrip())
    for k in keys:
        print(f"{k:<{label}}  " + "".join(f"{str(r[k]):<{col}}" for r in rows).rstrip())


if __name__ == "__main__":
    main()
