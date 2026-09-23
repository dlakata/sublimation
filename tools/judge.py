#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
judge — how close a candidate got to the known-good, graded blind by Claude.

`scorecard.py` counts the things that can be counted, and those are necessary conditions for a
good letter rather than the thing itself. This is the other half: the two documents put in front
of a strong reader as A and B, with no indication which came from where, and scored against the
standard the good one sets.

Two precautions, because an LLM asked to compare two texts has two well-known tells:

  position   it prefers whichever it read second. So every comparison is run twice with the
             order swapped, and a verdict that flips between the two orderings is reported as
             a tie rather than averaged into a false winner.
  identity   it prefers what it recognises as its own. So neither document is labelled, the
             prompt never says one is a reference, and the rubric asks for absolute per-
             dimension scores rather than "which is better" alone.

The rubric is per-stage, because the two documents are different genres with different
addressees — the read is written to the subject in the second person, the letter is written to
the interlocutor about him in the third — and a single rubric would score the letter down for
being the thing it was asked to be.

Dimensions get added when a sweep loses for a reason four dimensions could see and not name,
and each one was read off a comparison rather than designed in advance:

  challenge  after the 2026-09-09 sweep. Across all thirty analysis passes the difference the
             judge reached for was never style and rarely grounding: the known-good contested a
             premise the week had stopped examining and handed him a step outside his own frame,
             and no candidate did. The property was hiding inside `usefulness`, where it
             competed with ordinary specificity and lost.
  function   after the winner of that sweep still missed three of the known-good's findings,
             all of them the same move: a behaviour read by what it is in service of rather than
             by what it is about, evidenced from the order of events the week itself recorded.
             That is not a harder `insight` — a candidate can find a real pattern in the content
             and never ask what the content is doing for him, which is what happened.
  warmth     because the winning read was cold. It prosecutes for two thousand words and never
             says anything the subject did that week was any good, and it scored 5/5 on
             `register` for it — the rubric was rewarding nerve and could not see the difference
             between a clinician unafraid of him and one against him. Being challenged is the
             point; being prosecuted is not, and the two needed separating.
  carry      on the letter only. The letter is the half that gets narrated, so a finding the
             read made and the letter dropped was never delivered. The most valuable item is
             also the most compressible — a named piece of outside expertise shortens to a
             sentence reporting that a recommendation was made — and nothing in the rubric
             docked a letter for the shortening.
  proportion because every one of the twenty-five variants in the 2026-09-09 sweep handled the
             week's hottest passage the same way. The known-good gave it a paragraph and read
             it as the peak of a bad hour rather than as a settled conclusion, said what the
             passage was answering, and said how long the state actually lasted. No candidate
             did either, and the top of the table was an eighty-three. A rubric in which a
             document can flatten the loudest thing in the week and still score in the eighties
             is measuring craft and calling it quality. Silence scores low here; so does alarm,
             which is the other way to get it wrong.

Because a rubric that gains a dimension changes every mean it has ever produced, each result
carries the hash of the rubric that scored it. `--rubric-id STAGE` prints the current one, which
is how `experiment.py` knows a cached verdict was graded under an older rubric and re-judges it
rather than printing five-dimension means in an eight-dimension table.

Usage:
    ./judge.py CANDIDATE --against KNOWN-GOOD.md --stage letter [--model opus] [--json OUT]
    ./judge.py --rubric-id analysis
"""

from __future__ import annotations
import argparse
import hashlib
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

FRONT = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)

# No tools, and prose rather than the coding register — the same reasoning weekly_review.py
# gives for replacing the system prompt on its own calls.
SYSTEM = (
    "You are a careful editor grading two documents against a standard. Answer with the JSON "
    "object asked for and nothing else — no preamble, no code fence, no commentary after it. "
    "Do not use tools."
)

# What actually separates these documents, read off the archive rather than invented: the good
# read is specific, quotes the week back, and finds a pattern the week did not announce; the
# good letter is a briefing to a colleague that stays speakable and does not hedge.
DIMENSIONS = {
    "analysis": [
        ("grounding", "Names this week's actual particulars — people, numbers, artifacts, "
                      "dates, things the subject really wrote — rather than the genre of a "
                      "therapeutic read. Quotes the source back where it earns it."),
        ("insight", "Finds a pattern the week did not announce about itself, and supports it "
                    "from evidence in the week rather than asserting it."),
        ("register", "Addresses the subject directly, as a clinician who has read everything "
                     "and is not frightened of him. No hedging, no assistant tics, no opening "
                     "summary of what it is about to do."),
        ("usefulness", "Leaves the subject with something actionable and specific that he "
                       "could not have written himself."),
        ("challenge", "Contests a premise the week treats as settled — a constraint the "
                      "subject has organised his life around working around rather than "
                      "around solving — says plainly that it may be wrong, and converts it "
                      "into a specific step outside the frame he was working in: expertise "
                      "he is not consulting, a check he has not had, a question he has not "
                      "asked anyone. Conviction, not a menu: a read that lists possibilities "
                      "and lets him choose scores low here."),
        ("function", "Reads the week's behaviour by what it is in service of rather than by "
                     "what it is about. Names the question he has been safely asking all week "
                     "and the unbearable one it stands in for, and argues it from the order "
                     "of events he himself recorded — the symptom that intensified after "
                     "something went right, the most insightful day that was also the least "
                     "active. Naming the mechanism of a habit is not the same as naming what "
                     "the habit protects him from: a read that explains how the loop works, "
                     "however well, and never says what would happen to him if it stopped, "
                     "sits mid-scale at best."),
        ("warmth", "Challenges from affection rather than from prosecution. Says once and "
                   "specifically what he genuinely did well, as a finding and not as padding; "
                   "aims its irreverence at the absurd particular rather than at him; and "
                   "delivers the hardest sentence in the read as something a person who likes "
                   "him would say. Cruelty scores low, and so does flattery — the question is "
                   "whether he is still held while he hears the worst of it."),
        ("proportion", "Handles the week's hottest passage — the stretch where the "
                       "frustration is total — by naming it plainly once, at its own "
                       "weight, rather than letting it pass inside a paragraph about "
                       "something else. Reads it as the peak of a bad hour rather than as a "
                       "conclusion he has reached: says what the passage was answering, how "
                       "long the state lasted, and what it is a demand to change. A read "
                       "that flattens material of this kind scores 1 however good the rest "
                       "of it is. So does one that dramatises it, refuses the rest of the "
                       "week to dwell on it, or treats a bad hour as an emergency. If the "
                       "week contains nothing of the kind, a read that correctly says so in "
                       "a sentence scores 5, and one that manufactures a concern to satisfy "
                       "this dimension scores 1."),
    ],
    "letter": [
        ("grounding", "Names this week's actual particulars rather than the genre. The "
                      "specifics are load-bearing, not decoration."),
        ("insight", "Carries the diagnostic weight of the week in compact form — a pattern, "
                    "supported, not a summary of topics covered."),
        ("register", "A Ben Lerner / David Foster Wallace clinician writing TO a colleague "
                     "ABOUT the subject. Dry, exact, unhurried. Not a letter to the subject, "
                     "and not a report template."),
        ("structure", "Covers the logistics, the planning, the register, the vibe and the "
                      "result, in prose, without announcing the scaffolding mechanically."),
        ("speakable", "Reads aloud, at a cadence a narrator can perform and a listener can "
                      "follow without the text in front of them. No tables, no bullet lists, "
                      "no dense parentheticals, no markdown furniture — and, as much as any "
                      "of those, no sprawl: sentences that average over about twenty-five "
                      "words, run past forty at their longest, or stack into paragraphs of "
                      "two hundred words are a wall the ear cannot climb, however exact the "
                      "prose is on the page. Vary the length; land the clauses."),
        ("function", "Carries the functional reading and not only the pattern: what the "
                     "subject's presenting complaint is protecting him from, the safe question "
                     "standing in for the unbearable one, and the sequence in his own week "
                     "that evidences it. A letter that reports his themes to the colleague "
                     "without saying what they are in service of scores low."),
        ("carry", "The step from outside his frame survives compaction as something that "
                  "could be arranged from the letter alone: what the thing is called, which "
                  "profession provides it, what it is for, by when. A sentence reporting that "
                  "a recommendation was made, or a problem area named without a discipline, is "
                  "the finding dropped. Same for a revision — his version, yours, the reason."),
        ("warmth", "The clinician is fond of the patient and the colleague can tell. The hard "
                   "findings are stated without cruelty, the comedy of the week is allowed to "
                   "be funny without being explained, and what he did well is reported as "
                   "fact. Not softness: a letter that flatters, or that hedges the diagnosis "
                   "to be kind, scores low here too."),
        ("proportion", "Carries the week's hottest passage to the colleague at its own "
                       "weight — stated flatly, in its own sentences, not folded into the "
                       "vibe. Says what surfaced, where it surfaced and what is unusual "
                       "about that, what the passage was answering, and how long the state "
                       "lasted. This is the item the colleague is owed most and the one a "
                       "compaction drops first, so a letter that omits it scores 1 however "
                       "well the rest reads; a letter that dramatises it scores low too. If "
                       "the week holds nothing of the kind, one clean sentence saying so "
                       "scores 5."),
    ],
}

def rubric_id(stage: str) -> str:
    """The rubric that scored a result, as a short hash — so a stale verdict is visible.

    Names and descriptions both, because a reworded dimension is a different question and the
    scores it produced are not comparable with the ones before it.
    """
    payload = json.dumps(DIMENSIONS[stage], sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()[:8]


TASK = """You are grading two documents, A and B, which are two attempts at the same task for \
the same week of one person's life. One of them is the standard; you are not told which, and \
you should not try to guess.

The task was: {task}

Score EACH document on EACH dimension, 1-5, where 5 is "could not be done better" and 1 is \
"failed at this". Judge each document on its own terms, then compare.

Dimensions:
{dims}

Reply with exactly this JSON object and nothing else:

{{
  "A": {{{keys}}},
  "B": {{{keys}}},
  "closer_to_standard": "A" | "B",
  "gap": <0-100, how much of the better document's quality the weaker one achieves; \
100 means indistinguishable in quality, 0 means no comparison>,
  "why": "<two sentences: the single biggest difference between them, named concretely>"
}}

--- DOCUMENT A ---
{a}

--- DOCUMENT B ---
{b}
"""

TASKS = {
    "analysis": "an ACT (Acceptance and Commitment Therapy) re-entry read of one week of a "
                "man's Obsidian notes, written to him.",
    "letter": "a short literary letter compacting that week, written to a recurring expert "
              "interlocutor — a clinician who sees him — about him, to be narrated aloud.",
}


def body(path: Path) -> str:
    return FRONT.sub("", path.read_text()).strip()


def ask(prompt: str, model: str) -> dict:
    cmd = ["claude", "-p", "--session-id", str(uuid.uuid4()), "--model", model,
           "--output-format", "json", "--system-prompt", SYSTEM,
           "--disallowed-tools", "Bash Edit Write Read Glob Grep WebFetch WebSearch Task"]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"claude exited {proc.returncode}:\n{(proc.stderr or proc.stdout)[-2000:]}")
    outer = json.loads(proc.stdout)
    text = (outer.get("result") or "").strip()
    # A fenced object is still an object; anything else is the model ignoring the instruction.
    text = re.sub(r"\A```(?:json)?\s*|\s*```\Z", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        sys.exit(f"judge did not return JSON:\n{text[:1500]}")


def one_pass(cand: str, ref: str, stage: str, model: str, swapped: bool) -> dict:
    dims = DIMENSIONS[stage]
    prompt = TASK.format(
        task=TASKS[stage],
        dims="\n".join(f"  {name} — {desc}" for name, desc in dims),
        keys=", ".join(f'"{name}": <1-5>' for name, _ in dims),
        a=ref if swapped else cand,
        b=cand if swapped else ref,
    )
    got = ask(prompt, model)
    cand_key, ref_key = ("B", "A") if swapped else ("A", "B")
    return {
        "candidate": got.get(cand_key, {}),
        "reference": got.get(ref_key, {}),
        "candidate_won": got.get("closer_to_standard") == cand_key,
        "gap": got.get("gap"),
        "why": got.get("why", ""),
        "order": "reference first" if swapped else "candidate first",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("candidate", type=Path, nargs="?")
    ap.add_argument("--against", type=Path, help="the known-good document")
    ap.add_argument("--stage", choices=sorted(DIMENSIONS))
    ap.add_argument("--model", default="opus")
    ap.add_argument("--json", type=Path, help="write the full result here as well")
    ap.add_argument("--rubric-id", choices=sorted(DIMENSIONS),
                    help="print the hash of that stage's rubric and exit, and grade nothing")
    args = ap.parse_args()

    if args.rubric_id:
        print(rubric_id(args.rubric_id))
        return
    if not (args.candidate and args.against and args.stage):
        ap.error("candidate, --against and --stage are required unless --rubric-id is given")

    for p in (args.candidate, args.against):
        if not p.exists():
            sys.exit(f"no such file: {p}")
    cand, ref = body(args.candidate), body(args.against)

    passes = [one_pass(cand, ref, args.stage, args.model, swapped) for swapped in (False, True)]
    dims = [name for name, _ in DIMENSIONS[args.stage]]
    mean = lambda side, d: round(
        sum(float(p[side].get(d, 0) or 0) for p in passes) / len(passes), 2)

    wins = sum(p["candidate_won"] for p in passes)
    verdict = ("candidate" if wins == 2 else
               "reference" if wins == 0 else
               "tie — the two orderings disagreed, which is position bias, not a result")
    gaps = [p["gap"] for p in passes if isinstance(p["gap"], (int, float))]
    result = {
        "candidate": str(args.candidate),
        "reference": str(args.against),
        "stage": args.stage,
        "rubric": rubric_id(args.stage),
        "dimensions": {d: {"candidate": mean("candidate", d),
                           "reference": mean("reference", d)} for d in dims},
        "candidate_mean": round(sum(mean("candidate", d) for d in dims) / len(dims), 2),
        "reference_mean": round(sum(mean("reference", d) for d in dims) / len(dims), 2),
        "closer_to_standard": verdict,
        "gap": round(sum(gaps) / len(gaps), 1) if gaps else None,
        "passes": passes,
    }
    if args.json:
        args.json.write_text(json.dumps(result, indent=2))

    width = max(len(d) for d in dims) + 2
    print(f"{'':<{width}}  candidate  reference")
    for d in dims:
        c, r = mean("candidate", d), mean("reference", d)
        print(f"{d:<{width}}  {c:^9}  {r:^9}")
    print(f"{'mean':<{width}}  {result['candidate_mean']:^9}  {result['reference_mean']:^9}")
    print(f"\ncloser to the standard: {verdict}")
    print(f"gap: {result['gap']}/100 of the better document's quality")
    for p in passes:
        print(f"  ({p['order']}) {p['why']}")


if __name__ == "__main__":
    main()
