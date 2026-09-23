#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
experiment — one week, replayed under many configurations, scored against the known-good.

`just ab` replays one fixed session and prints a scorecard. This is that, in a loop, with the
configuration written down rather than left in the shell history — because the thing being
measured here is the configuration, and an A/B whose A you have to remember is not one.

What a variant may set, and where each lands:

  top_k, min_p, repeat_penalty    the Modelfile. Changing one costs an `evanescent retune`,
                                  which is seconds and no download — so variants are ordered
                                  to retune as rarely as the list allows.
  temperature, top_p              the opencode agent, per request. Free to change.
  effort                          `opencode --variant`. Free.
  read_prompt                     `--prompt`, the ACT read's question.
  style_prompt                    the register block appended after it.
  compaction_prompt               the letter's question.
  references                      the example read and the metaphor menu, appended under
                                  opencode. `false` withholds both.

Everything but the first three rides on environment variables that petrograph reads, so a
variant costs a subprocess rather than a rebuild.

Each run is scored twice: `scorecard.py` on both documents, which is free and deterministic,
and — with `--judge` — `judge.py`, which puts the candidate and the known-good in front of
Claude blind, in both orders. The scorecard is the gate; the judge is the answer.

**More than one week, because one week is a thing to overfit to.** `--session` may be given
more than once, and every variant is then replayed against each of them and scored against each
one's own known-good. The rows are combined by `combine()` below, which does two things a plain
mean does not: it weights the read above the letter, because the letter is written in the
session the read opened and cannot be better than what it compacts; and it subtracts the spread
between the weeks, because a prompt that scores ninety on the week it was written from and fifty
on the next one is not a better prompt than a steady seventy — it is a prompt that has been
shown the answer key. Both weights are flags, and both are printed with the table.

**And only the rows still worth the box.** `--previous BOOK --top N` reads an earlier sweep's
book, keeps its N best-scoring variants, and drops the rest before anything runs. A variant that
is not in that book at all is new and unmeasured, so it is always kept. Twenty-five rows against
two weeks is ten hours of a box billed by the minute; ten rows is three, and the fifteen dropped
are ones whose numbers are already in.

**It is meant to be walked away from.** Twenty variants is closer to three hours of a box you
are paying for by the minute, and nobody sits through that in one sitting. So the book of
results is one file per session rather than one per invocation, every variant is written into
it the moment it finishes, and a variant already in the book is skipped rather than re-run:

    just experiment --judge --only baseline,topk-20     # an hour you have now
    just end-sublimation                                # stop paying
    ...
    just resume && just experiment --judge              # tomorrow: the other eighteen

A row is taken as done only if it succeeded, its documents are still on disk, and it is still
asking for the same thing — the variant's configuration is fingerprinted, so editing a knob in
variants.toml re-runs that row instead of reporting it under numbers it no longer earns.
`--redo NAME` (or `--redo all`) forces one anyway. Asking for `--judge` after a run that went
without it costs the judging only, not another turn on the box.

`--report` prints the book and runs nothing, which is also what `summary.md` holds.

Each session supplies its own known-good by convention — `claude-analysis.md` for the read and
`lifelog-<session>.md` for the letter, which is where petrograph already puts them — so a second
week costs one flag. `--letter-ref` and `--analysis-ref` still override, for a single session
whose files are elsewhere.

Usage:
    ./experiment.py variants.toml --session sessions/2026-09-09 --session sessions/2026-09-16 \
        --judge [--previous .sublimation/experiments/2026-09-09 --top 5] \
        [--only NAME,NAME] [--redo NAME] [--report]
"""

from __future__ import annotations
import argparse
import atexit
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PETROGRAPH = ROOT / "petrograph"
EVANESCENT = [str(ROOT / "evanescent" / ".venv" / "bin" / "python"),
              str(ROOT / "evanescent" / "evanescent.py")]
STATE = ROOT / ".sublimation"
SAMPLER = ("top_k", "min_p", "repeat_penalty")
# How many times a variant is given to opencode before its failure is believed, and how long
# to leave between tries. Three and thirty seconds costs a genuinely broken variant a minute
# and a half; one flaky second costs the sweep nothing.
ATTEMPTS = 3
BACKOFF = 30
# What a variant may say. Anything else in the file is a typo, and a typo that silently does
# nothing would be scored as a result — so it is refused rather than ignored.
KNOWN = set(SAMPLER) | {"name", "note", "temperature", "top_p", "effort",
                        "read_prompt", "style_prompt", "compaction_prompt", "references"}
ENV_PROMPTS = {"style_prompt": "PETROGRAPH_STYLE_PROMPT",
               "compaction_prompt": "PETROGRAPH_COMPACTION_PROMPT"}


def say(msg: str = "") -> None:
    print(msg, flush=True)


def model_name() -> str:
    return subprocess.run(EVANESCENT + ["model"], capture_output=True, text=True,
                          check=True).stdout.strip()


# What this process last put on the box. `None` until it has set one, which is what makes the
# first variant always retune: the state file is a claim about the box, and a sweep that is
# about to spend two hours measuring samplers should establish the sampler rather than inherit
# a claim. Ten seconds once, against a whole run measured under the wrong one.
_applied: dict | None = None


def retune(want: dict) -> None:
    """Rebuild the served model, but only when this variant actually moved the Modelfile."""
    global _applied
    have = _applied if _applied is not None else None
    if have is not None and all(float(want[k]) == float(have[k]) for k in SAMPLER):
        return
    flags = [f"--{k.replace('_', '-')}={want[k]}" for k in SAMPLER]
    say(f"    retune {' '.join(f'{k}={want[k]}' for k in SAMPLER)}")
    subprocess.run(EVANESCENT + ["retune"] + flags, check=True,
                   stdout=subprocess.DEVNULL)
    _applied = {k: want[k] for k in SAMPLER}
    ensure_serving()


def ensure_serving() -> None:
    """Prove the box will actually answer before handing it a week.

    `ollama create` can leave the runner wedged: the model stays resident, `/api/tags` answers
    in microseconds, and every inference request hangs until its client gives up. A sweep that
    does not check walks the rest of the list against a box that cannot answer -- which is how
    fourteen variants came back failed in twenty seconds, saying nothing about themselves.

    One small completion is the whole test, because the symptom is not slowness: a healthy box
    answers this in seconds and a wedged one never answers at all.
    """
    probe = json.dumps({"model": model_name().split("/", 1)[-1], "prompt": "ok",
                        "stream": False, "options": {"num_predict": 1}}).encode()
    for attempt in (1, 2):
        try:
            urllib.request.urlopen(
                urllib.request.Request("http://localhost:11434/api/generate", data=probe,
                                       headers={"Content-Type": "application/json"}),
                timeout=90).read()
            return
        except Exception:
            if attempt == 2:
                sys.exit("ollama is not answering completions even after a restart — "
                         "`just box-ssh -- sudo systemctl restart ollama`, then re-run.")
            say("    ollama is not answering completions — restarting it on the box")
            subprocess.run(EVANESCENT + ["ssh", "--", "sudo systemctl restart ollama"],
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(10)


def stage(session: Path, into: Path, model: str, effort: str) -> Path:
    """A private copy of the week, pointing at its own diff.

    The copy matters: a run must not reach back into the session it is replaying, or a bad
    variant would overwrite the very thing every variant is scored against.
    """
    run = into / "sessions" / session.name
    run.mkdir(parents=True, exist_ok=True)
    for f in ("edit-me.md", "microlite.md"):
        src = session / f
        if not src.exists():
            sys.exit(f"{session} has no {f} — this replays a session that already ran")
        shutil.copy(src, run / f)

    edit = run / "edit-me.md"
    out = []
    for line in edit.read_text().splitlines():
        if line.startswith("diff:"):
            line = f"diff: {run / 'microlite.md'}"
        elif line.startswith("llm:"):
            line = "llm: opencode  # pinned by the experiment"
        elif line.startswith("model:"):
            line = f"model: {model}  # pinned by the experiment"
        elif line.startswith("effort:"):
            line = f"effort: {effort}  # set by the variant"
        out.append(line)
    edit.write_text("\n".join(out) + "\n")
    return run


def resolve(value, base: Path) -> str:
    """A prompt path from the variants file, resolved against that file's own directory."""
    p = Path(str(value)).expanduser()
    p = p if p.is_absolute() else (base / p)
    if not p.exists():
        sys.exit(f"variant names a prompt that does not exist: {p}")
    return str(p.resolve())


def run_variant(v: dict, session: Path, into: Path, base: Path, model: str) -> dict:
    into.mkdir(parents=True, exist_ok=True)
    (into / "variant.json").write_text(json.dumps(v, indent=2))
    retune(v)
    run = stage(session, into, model, str(v.get("effort", "max")))

    env = dict(os.environ)
    env["PETROGRAPH_TEMPERATURE"] = str(v.get("temperature", 1.0))
    env["PETROGRAPH_TOP_P"] = str(v.get("top_p", 0.95))
    if v.get("references", True) is False:
        env["PETROGRAPH_REFERENCES"] = "0"
    for key, var in ENV_PROMPTS.items():
        if v.get(key):
            env[var] = resolve(v[key], base)

    cmd = [str(PETROGRAPH / "tools" / "weekly_review.py"), str(run),
           "--out-root", str(into), "--llm", "opencode", "--skip-narration"]
    if v.get("read_prompt"):
        cmd += ["--prompt", resolve(v["read_prompt"], base)]

    log = into / "run.log"
    t = time.perf_counter()
    # opencode fails intermittently, in about a second, with an "Unexpected server error" that
    # reaches neither its own log nor ollama's -- so it is not the model, the prompt, the agent
    # or the flags, all of which were held still and bisected against it. The same command run
    # again a minute later goes through. It cost a whole fifteen-variant sweep once: every run
    # in a twenty-second window failed, and the book recorded fourteen FAILEDs that were not
    # about their variants at all.
    #
    # So it is retried rather than diagnosed. A real failure -- a prompt file that is not
    # there, a model the box cannot serve -- fails the same way every time and still lands in
    # the book after the last attempt, which is the behaviour worth keeping.
    # Through a shell, which is not decoration. Spawned as a plain argv from Python, opencode
    # fails inside two seconds with "Unexpected server error" and writes nothing to its own
    # log; ollama never sees the request. Put `/bin/sh -c` between the two and the identical
    # command, environment, working directory and prompt goes through -- which is also how
    # `just` invokes it, and why the hand-run path always worked.
    #
    # Alternated against each other with ollama known-healthy, this was the only variable that
    # changed the outcome; the flags, the agent config, the prompt, the uv variables, stdin,
    # close_fds and restore_signals were each held still and swapped. The mechanism is still
    # unexplained, so this is a workaround and is labelled as one.
    shell_cmd = " ".join(shlex.quote(c) for c in cmd)
    for attempt in range(1, ATTEMPTS + 1):
        with log.open("w") as fh:
            proc = subprocess.run(["/bin/sh", "-c", shell_cmd],
                                  stdout=fh, stderr=subprocess.STDOUT,
                                  text=True, cwd=PETROGRAPH, env=env)
        if proc.returncode == 0:
            break
        quick = time.perf_counter() - t < 20
        if attempt < ATTEMPTS:
            say(f"    attempt {attempt} failed"
                f"{' in seconds — the flaky one' if quick else ''}; retrying in {BACKOFF}s")
            time.sleep(BACKOFF)
            t = time.perf_counter()
    return {"ok": proc.returncode == 0, "attempts": attempt,
            "seconds": round(time.perf_counter() - t), "dir": str(run), "log": str(log)}


def scorecard(doc: Path, diff: Path) -> dict:
    proc = subprocess.run([str(ROOT / "tools" / "scorecard.py"), str(doc),
                           "--diff", str(diff), "--json"],
                          capture_output=True, text=True, check=True)
    return next(iter(json.loads(proc.stdout).values()))


def judge(doc: Path, ref: Path, stage_name: str, out: Path, model: str) -> dict | None:
    proc = subprocess.run([str(ROOT / "tools" / "judge.py"), str(doc), "--against", str(ref),
                           "--stage", stage_name, "--model", model, "--json", str(out)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        say(f"    judge failed: {(proc.stderr or proc.stdout).strip()[:300]}")
        return None
    return json.loads(out.read_text())


def fingerprint(v: dict, base: Path) -> str:
    """What a variant asks for, as a short hash — so an edited variant is not taken as done.

    `note` is excluded because it is commentary: rewording why a row exists should not throw
    away the run that row already has.

    The prompt files are hashed by their *contents* and not by their paths, which is the part
    that was wrong. A variant names a prompt; editing a block inside that prompt changes what
    the row asks for completely and moved nothing the old fingerprint could see, so the sweep
    reported the row as already done and printed the previous run's numbers under the new
    question. Nothing on the page said so. Reading the file in costs a few milliseconds and
    makes the obvious next move — improve a block, re-run the rows that use it — safe.
    """
    asked = {k: v[k] for k in sorted(v) if k not in ("note",)}
    for key in ("read_prompt", "style_prompt", "compaction_prompt"):
        if v.get(key):
            asked[key] = hashlib.sha256(
                Path(resolve(v[key], base)).read_bytes()).hexdigest()[:16]
    return hashlib.sha256(json.dumps(asked, sort_keys=True).encode()).hexdigest()[:12]


# How the two stages are weighted against each other when a variant's weeks are combined, and
# how hard a variant is docked for being good on one week and poor on the next.
#
# The read is worth more than the letter because the letter is not an independent draw: it is
# written in the session the read opened, so a better read improves the letter for free and a
# letter can only compact what it was given. Scoring them evenly rewards a row that writes well
# about a thin read, which is the row least worth shipping.
#
# The spread is subtracted rather than reported beside the mean because a sweep that only ranks
# by mean will hand back whichever prompt was tuned hardest against the week it was written
# from. Half the range is enough to matter and not enough to dominate: a variant forty points
# apart across two weeks loses twenty, which puts it behind a steady row seventeen points below
# its own average, and a variant five points apart loses almost nothing.
ANALYSIS_WEIGHT = 0.6
SPREAD_PENALTY = 0.5


def per_session(row: dict) -> dict[str, dict]:
    """A row's results keyed by session, for rows from either book layout.

    Sweeps before the second known-good week stored one session's `run`, `letter` and
    `analysis` at the top level, and `--previous` has to rank exactly those rows. Rather than
    rewrite the old book -- which is the record of what those configurations produced, and not
    this tool's to edit -- the flat shape is read as the one-session case it always was.
    """
    if "sessions" in row:
        return row["sessions"]
    return {row.get("session", "(unnamed)"): row} if "run" in row else {}


def combine(row: dict, analysis_weight: float = ANALYSIS_WEIGHT,
            spread_penalty: float = SPREAD_PENALTY) -> dict | None:
    """One number for a variant across every week it was run against.

    Per week: the two stages' gaps, weighted, over the weights of the stages that actually
    scored -- so a week with no known-good read is that week's letter rather than a zero, which
    would punish a variant for a file that is missing from the archive.

    Across weeks: the mean, less the spread. `spread` is the plain range and not a standard
    deviation because two weeks is the case this runs in, where the two are the same number and
    the range is the one a person can check by looking at the row.
    """
    letter_weight = 1.0 - analysis_weight
    weeks = {}
    for name, res in per_session(row).items():
        parts = [(analysis_weight, res.get("analysis", {}).get("judge")),
                 (letter_weight, res.get("letter", {}).get("judge"))]
        got = [(w, j["gap"]) for w, j in parts if j and isinstance(j.get("gap"), (int, float))]
        if got:
            weeks[name] = round(sum(w * g for w, g in got) / sum(w for w, _ in got), 1)
    if not weeks:
        return None
    mean = sum(weeks.values()) / len(weeks)
    spread = max(weeks.values()) - min(weeks.values())
    return {"weeks": weeks, "mean": round(mean, 1), "spread": round(spread, 1),
            "score": round(max(0.0, mean - spread_penalty * spread), 1)}


def load(results: Path) -> dict[str, dict]:
    """Every variant already run, by name. One line per variant, the last one winning.

    The file is read back rather than held only in memory because the whole point of it is the
    session you come back to tomorrow -- a sweep interrupted after four variants should cost
    four variants, not twenty.
    """
    if not results.exists():
        return {}
    done = {}
    for line in results.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            done[row["variant"]["name"]] = row
    return done


def save(results: Path, done: dict[str, dict]) -> None:
    """Rewrite the book, atomically, in the order the variants were defined.

    Written beside and renamed over rather than truncated in place: `--report` is meant to be
    run against a sweep that is still going, and a plain rewrite has a window in which the
    reader sees a file that has been emptied but not yet refilled.
    """
    tmp = results.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(done[k]) + "\n" for k in done))
    tmp.replace(results)


def rubrics(stages: list[str]) -> dict[str, str]:
    """The hash of each stage's current rubric, asked of the tool that owns it."""
    ids = {}
    for stage_name in stages:
        proc = subprocess.run([str(ROOT / "tools" / "judge.py"), "--rubric-id", stage_name],
                              capture_output=True, text=True, check=True)
        ids[stage_name] = proc.stdout.strip()
    return ids


def current(res: dict, stage_name: str, ids: dict[str, str]) -> dict | None:
    """One session's stored verdict for a stage, if it was graded under the rubric in force now.

    A rubric that gains or reworks a dimension changes every mean it produces, so a cached
    verdict from before the change is not a smaller version of today's answer -- it is an
    answer to a different question, and a table that mixes the two is wrong in a way nothing
    on the page shows. Rather than silently re-judging the whole book, a verdict whose rubric
    has moved is dropped here and re-earned on the next --judge run.
    """
    verdict = res.get(stage_name, {}).get("judge")
    if verdict and verdict.get("rubric") == ids.get(stage_name):
        return verdict
    return None


def judged(row: dict, ids: dict[str, str]) -> bool:
    """Whether every stage of every week this row ran carries a verdict under today's rubric."""
    seen = per_session(row)
    if not seen:
        return False
    return all(current(res, s, ids) for res in seen.values()
               for s in ("letter", "analysis") if s in res)


def table(done: dict[str, dict], ids: dict[str, str] | None = None,
          sessions: list[str] | None = None,
          analysis_weight: float = ANALYSIS_WEIGHT,
          spread_penalty: float = SPREAD_PENALTY) -> str:
    """The whole sweep as one table, which is the thing you actually come back to read.

    Sorted by the combined score rather than by the order the variants were defined, because
    with two weeks in the row the question the table answers is "which one ships" and a reader
    should not have to compute it. Unscored and failed rows sink to the bottom.

    A verdict graded under a rubric that has since moved is marked `*` rather than dropped or
    quietly reprinted: it is the best answer that row has, and it is not comparable with the
    ones beside it. The mark is the invitation to re-run --judge for that row.
    """
    weeks = sessions or sorted({s for row in done.values() for s in per_session(row)})
    head = ["variant", "sec"] + [f"{w}  a/l" for w in weeks] + ["mean", "spread", "score"]
    stale = False

    def cells(name: str, row: dict) -> list[str]:
        nonlocal stale
        seen = per_session(row)
        secs = sum(r.get("run", {}).get("seconds", 0) or 0 for r in seen.values())
        out = [name, str(secs or "")]
        for w in weeks:
            res = seen.get(w)
            if not res:
                out.append("—")
            elif not res.get("run", {}).get("ok"):
                out.append("FAILED")
            else:
                parts = []
                for st in ("analysis", "letter"):
                    j = res.get(st, {}).get("judge")
                    if not j:
                        parts.append("—")
                        continue
                    old = ids is not None and j.get("rubric") != ids.get(st)
                    stale = stale or old
                    parts.append(f"{j['gap']:g}" + ("*" if old else ""))
                out.append("/".join(parts))
        c = combine(row, analysis_weight, spread_penalty)
        out += [f"{c['mean']:g}", f"{c['spread']:g}", f"{c['score']:g}"] if c else ["—"] * 3
        return out

    rows = [head]
    ranked = sorted(done.items(),
                    key=lambda kv: -(combine(kv[1], analysis_weight, spread_penalty)
                                     or {"score": -1})["score"])
    rows += [cells(name, row) for name, row in ranked]
    width = [max(len(r[i]) for r in rows) for i in range(len(head))]
    out = ["  ".join(c.ljust(width[i]) for i, c in enumerate(rows[0])).rstrip(),
           "  ".join("-" * width[i] for i in range(len(head)))]
    out += ["  ".join(c.ljust(width[i]) for i, c in enumerate(r)).rstrip() for r in rows[1:]]
    out += ["",
            f"a/l is the judge's gap out of 100 on the read and on the letter, per week.",
            f"score = mean - {spread_penalty:g} x spread, where each week is the read at "
            f"{analysis_weight:g} and the letter at {1 - analysis_weight:g}.",
            "spread is the range across the weeks: the cost of having been written against one."]
    if stale:
        out += ["", "* graded under an older rubric — not comparable with the unmarked rows;",
                "  `--judge` re-earns it."]
    return "\n".join(out)


def carry_over(variants: list[dict], book: Path, top: int,
               analysis_weight: float, spread_penalty: float) -> list[dict]:
    """Drop the variants an earlier sweep already showed to be weak, before anything runs.

    A row that is not in the previous book is new and unmeasured, and is always kept -- the
    whole reason to run again is usually that some of the rows are new. Of the rows that *are*
    in it, the best `top` by combined score survive; the rest are named as they are dropped,
    because a variant vanishing from a table with no explanation is how a sweep loses a row it
    meant to keep.

    A row with no verdict in the book scores nothing and cannot be ranked, so it is treated as
    new: an unjudged run is not evidence of weakness.
    """
    book = book / "results.jsonl" if book.is_dir() else book
    if not book.exists():
        sys.exit(f"--previous names no book: {book}")
    scored, unscored = {}, set()
    for name, row in load(book).items():
        c = combine(row, analysis_weight, spread_penalty)
        (scored.__setitem__(name, c["score"]) if c else unscored.add(name))
    keep = {n for n, _ in sorted(scored.items(), key=lambda kv: -kv[1])[:top]}
    out, dropped = [], []
    for v in variants:
        name = v["name"]
        if name not in scored or name in keep:
            out.append(v)
        else:
            dropped.append((name, scored[name]))
    say(f"==> {book}: keeping the top {top} of {len(scored)} scored "
        f"({', '.join(sorted(keep)) or 'none'})")
    if dropped:
        say(f"    dropping {len(dropped)}: "
            + ", ".join(f"{n} ({s:g})" for n, s in sorted(dropped, key=lambda d: -d[1])))
    fresh = [v["name"] for v in out if v["name"] not in scored]
    if fresh:
        say(f"    keeping {len(fresh)} not in that book: {', '.join(fresh)}")
    return out


def hold(out: Path) -> None:
    """Refuse to be the second sweep writing this book.

    Two of these against one book is the expensive mistake: they retune the same box out from
    under each other, so each measures a sampler the other set, and both pay for it. The pid is
    checked for liveness rather than trusted -- a lock left behind by a run that was killed is
    the other way this goes wrong, and it would block every later sitting.
    """
    lock = out / "sweep.pid"
    if lock.exists():
        try:
            pid = int(lock.read_text().strip())
            os.kill(pid, 0)
        except (ValueError, ProcessLookupError, PermissionError):
            pass                                    # stale, or not ours to signal
        else:
            sys.exit(f"a sweep is already running against this book (pid {pid}).\n"
                     f"  ./tools/experiment.py … --report   # watch it instead\n"
                     f"  kill {pid}                         # or stop it, then re-run")
    lock.write_text(str(os.getpid()))
    atexit.register(lambda: lock.unlink(missing_ok=True))


def write_summary(out: Path, sessions: list[str], done: dict[str, dict],
                  ids: dict[str, str] | None = None,
                  analysis_weight: float = ANALYSIS_WEIGHT,
                  spread_penalty: float = SPREAD_PENALTY) -> None:
    """The table beside the data, so the book is readable without running the tool."""
    (out / "summary.md").write_text(
        f"# {' + '.join(sessions)} — {len(done)} variant(s)\n\n"
        "Each cell is how much of that week's known-good the candidate reached, out of 100,\n"
        "on the read and on the letter. The row to ship is the top of the `score` column,\n"
        "which prefers a variant that held up on both weeks over one that peaked on either.\n\n"
        f"```\n{table(done, ids, sessions, analysis_weight, spread_penalty)}\n```\n")


def refs_for(session: Path, letter_ref: Path | None,
             analysis_ref: Path | None) -> dict[str, Path]:
    """Where a week's known-good documents live.

    By convention, which is petrograph's own: it writes the read to `claude-analysis.md` and
    names the letter after the session, `lifelog-<date>.md`, the same name its audio takes. So
    a second week costs one flag rather than three. The explicit flags still win when they are
    given, which is only useful for a single session whose files sit somewhere else -- with two
    sessions there is no one file either flag could mean, and passing them is refused rather
    than silently applied to the first.
    """
    out = {}
    letter = letter_ref or session / f"lifelog-{session.name}.md"
    if not letter.exists():
        sys.exit(f"{session} has no known-good letter at {letter}")
    out["letter"] = letter
    analysis = analysis_ref or session / "claude-analysis.md"
    if analysis.exists():
        out["analysis"] = analysis
    elif analysis_ref:
        sys.exit(f"no such file: {analysis_ref}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("variants", type=Path)
    ap.add_argument("--session", type=Path, action="append", required=True, dest="sessions",
                    help="a week to replay; repeat it to score against more than one")
    ap.add_argument("--letter-ref", type=Path, help="override the known-good letter (one session)")
    ap.add_argument("--analysis-ref", type=Path, help="override the known-good read (one session)")
    ap.add_argument("--judge", action="store_true", help="also grade blind against the known-good")
    ap.add_argument("--judge-model", default="opus")
    ap.add_argument("--only", help="comma-separated variant names to run")
    ap.add_argument("--redo", help="comma-separated names to run again, or `all`")
    ap.add_argument("--previous", type=Path,
                    help="an earlier book (or its directory) to carry the best rows over from")
    ap.add_argument("--top", type=int, default=5,
                    help="how many of --previous's rows to keep; the rest are dropped unrun. "
                         "0 keeps them all, which is --previous with no filter")
    ap.add_argument("--analysis-weight", type=float, default=ANALYSIS_WEIGHT,
                    help=f"the read's share of a week's score (default {ANALYSIS_WEIGHT})")
    ap.add_argument("--spread-penalty", type=float, default=SPREAD_PENALTY,
                    help=f"score = mean - this x the range across weeks "
                         f"(default {SPREAD_PENALTY}); 0 ranks by mean alone")
    ap.add_argument("--report", action="store_true",
                    help="print what is already in the book and run nothing")
    ap.add_argument("--out", type=Path,
                    help="the book to add to; default .sublimation/experiments/<sessions>")
    args = ap.parse_args()

    sessions = [s if not str(s).endswith("/") else Path(str(s).rstrip("/"))
                for s in args.sessions]
    if len(sessions) > 1 and (args.letter_ref or args.analysis_ref):
        ap.error("--letter-ref/--analysis-ref name one week's files and cannot be applied to "
                 "several; drop them and let each session supply its own")
    for s in sessions:
        if not s.is_dir():
            sys.exit(f"no session at {s}")
    known = {s.name: refs_for(s, args.letter_ref, args.analysis_ref) for s in sessions}
    names = [s.name for s in sessions]

    spec = tomllib.loads(args.variants.read_text())
    variants = spec.get("variant", [])
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        variants = [v for v in variants if v.get("name") in wanted]
    if not variants:
        sys.exit("no variants to run")
    # What a variant inherits when it does not say. Declared in the file rather than read off
    # the box, and that is the whole point: a fingerprint taken against live box state moves
    # whenever some other variant retunes, so a row already in the book would stop matching
    # itself and be re-run for nothing. Written down, the baseline is also a fact about the
    # experiment rather than a fact about whatever the box was last left tuned to.
    defaults = spec.get("defaults", {})
    unknown = set(defaults) - KNOWN
    if unknown:
        sys.exit(f"[defaults] sets unknown keys: {', '.join(sorted(unknown))}")
    missing = [k for k in SAMPLER if k not in defaults]
    if missing:
        sys.exit(f"[defaults] must pin the sampler — missing: {', '.join(missing)}")
    for v in variants:
        unknown = set(v) - KNOWN
        if unknown:
            sys.exit(f"variant {v.get('name')!r} sets unknown keys: {', '.join(sorted(unknown))}")
        v.setdefault("name", "unnamed")
        for k, value in defaults.items():
            if k != "name":
                v.setdefault(k, value)

    base = args.variants.resolve().parent
    # `--top 0` means keep everything: a book named with no number to cut it at is a book
    # nobody asked to filter by.
    if args.previous and args.top > 0 and not args.report:
        variants = carry_over(variants, args.previous, args.top,
                              args.analysis_weight, args.spread_penalty)
        if not variants:
            sys.exit("--previous and --top dropped every variant")

    # A retune is the only expensive transition in the list, so the list is walked in an order
    # that makes as few of them as the set of variants allows -- same sampler, run together.
    variants.sort(key=lambda v: tuple(float(v[k]) for k in SAMPLER))

    # Keyed by the weeks rather than by the clock. A timestamp would give every invocation its
    # own directory, which is exactly wrong for a sweep you mean to walk away from: the second
    # sitting would start an empty book beside the first instead of continuing it. Two weeks
    # join their names, so a two-week sweep is its own book and does not land on top of either
    # week's single-week one -- the scores are not comparable and the books should not mix.
    out = args.out or STATE / "experiments" / "+".join(names)
    out.mkdir(parents=True, exist_ok=True)
    results = out / "results.jsonl"
    done = load(results)

    if args.report:
        if not done:
            say(f"nothing in {results} yet")
            return
        ids = rubrics(["letter", "analysis"])
        say(table(done, ids, names, args.analysis_weight, args.spread_penalty))
        write_summary(out, names, done, ids, args.analysis_weight, args.spread_penalty)
        return

    hold(out)
    redo = {s.strip() for s in (args.redo or "").split(",") if s.strip()}
    prints = {v["name"]: fingerprint(v, base) for v in variants}
    # The tunnel, before anything else. Without it every run fails in about a second, and a
    # sweep left alone would work through the whole list doing that -- which is the one way
    # this returns worthless rows quickly rather than good rows slowly.
    todo = [v for v in variants if v["name"] not in done or v["name"] in redo or "all" in redo]
    if todo:
        try:
            urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5).read()
        except Exception as exc:
            sys.exit(f"nothing is answering on localhost:11434 ({exc}).\n"
                     f"  just resume      # if the box stopped itself after 60 idle minutes\n"
                     f"  just tunnel-up   # if it is running and only the forward is gone")
    model = model_name()
    say(f"==> {len(variants)} variant(s) x {len(sessions)} week(s) "
        f"({', '.join(names)}) → {out}")
    say(f"    model {model} · judge {'on' if args.judge else 'off'} · "
        f"{len(done)} already in the book")

    # Asked once, before the loop, so a sweep is graded under one rubric from end to end even
    # if the file is edited while it runs.
    rubric_ids = rubrics(["letter", "analysis"])
    for i, v in enumerate(variants, 1):
        name = v["name"]
        say(f"\n── {i}/{len(variants)}  {name} " + "─" * 40)
        was = done.get(name)
        forced = "all" in redo or name in redo
        # Three things have to hold before a variant is taken as done: every week it owes
        # succeeded, it is still asking for the same thing, and its documents are where it left
        # them. The fingerprint is what makes editing a variant -- or a block inside the prompt
        # it names -- safe: a row whose question moved is re-run rather than silently reported
        # under numbers it no longer earns.
        seen = per_session(was) if was else {}
        fresh = (was and was.get("fingerprint") == prints[name] and not forced
                 and all(w in seen and seen[w].get("run", {}).get("ok")
                         and Path(seen[w]["run"]["dir"]).exists() for w in names))
        if fresh and (not args.judge or judged(was, rubric_ids)):
            say(f"    already done ({was.get('when', 'earlier')}) — skipping")
            continue

        row = was if fresh else {"variant": v, "fingerprint": prints[name],
                                 "sessions": {},
                                 "when": datetime.now().isoformat(timespec="seconds")}
        row["variant"], row["fingerprint"] = v, prints[name]
        row.setdefault("sessions", {})
        if fresh:
            # The expensive half is already on disk. Asking for --judge after a run that did
            # not use it should cost the judging and not the box.
            say("    documents already written — judging those, not re-running")

        failed = False
        for session in sessions:
            week = session.name
            had = seen.get(week, {})
            usable = (fresh and had.get("run", {}).get("ok")
                      and Path(had["run"]["dir"]).exists())
            if usable:
                run, res = had["run"], had
            else:
                say(f"    [{week}]")
                run = run_variant(v, session, out / name / week, base, model)
                res = {"run": run}
            if not run["ok"]:
                say(f"    [{week}] FAILED after {run['seconds']}s — see {run['log']}")
                failed = True
                break
            if not usable:
                say(f"    [{week}] wrote both documents in {run['seconds']}s")
            made = Path(run["dir"])
            # petrograph names the letter after the session it belongs to -- `lifelog-<date>.md`,
            # the name its audio takes too. `claude-output.md` is what it used to be called, and
            # is still what `just ab` looks for, so both are tried before the stage is skipped.
            letter = next((made / n for n in (f"lifelog-{made.name}.md", "claude-output.md")
                           if (made / n).exists()), made / f"lifelog-{made.name}.md")
            docs = {"letter": letter, "analysis": made / "claude-analysis.md"}
            for stage, ref in known[week].items():
                doc = docs[stage]
                if not doc.exists():
                    continue
                cand, gold = scorecard(doc, session / "microlite.md"), scorecard(ref, session / "microlite.md")
                kept = current(res, stage, rubric_ids)
                res[stage] = {"candidate": cand, "reference": gold,
                              "delta_words": cand["words"] - gold["words"],
                              **({"judge": kept} if kept else {})}
                say(f"    [{week}] {stage:<9} {cand['words']} words "
                    f"(known-good {gold['words']}) · sentences {cand['sentence_mean']} "
                    f"(known-good {gold['sentence_mean']}) · paragraphs "
                    f"{cand['paragraph_mean']} (known-good {gold['paragraph_mean']})")
                if args.judge and "judge" not in res[stage]:
                    outfile = out / name / week / f"judge-{stage}.json"
                    outfile.parent.mkdir(parents=True, exist_ok=True)
                    verdict = judge(doc, ref, stage, outfile, args.judge_model)
                    if verdict:
                        res[stage]["judge"] = verdict
                if (verdict := res[stage].get("judge")):
                    say(f"    [{week}] {stage:<9} judge: gap {verdict['gap']}/100 · "
                        f"{verdict['closer_to_standard']}")
            row["sessions"][week] = res

        if failed:
            # A failed re-run must not overwrite a row that succeeded: the book is the record
            # of what this configuration produced, and a box that was mid-rebuild or briefly
            # unreachable has produced nothing to record. The old numbers stand; the failure
            # is in the log, where a failure belongs.
            if was and per_session(was) and all(
                    r.get("run", {}).get("ok") for r in per_session(was).values()):
                say("    keeping the earlier successful row — --redo to replace it")
                continue
            done[name] = row
            save(results, done)
            continue

        if (c := combine(row, args.analysis_weight, args.spread_penalty)):
            row["score"] = c
            say(f"    score {c['score']} (mean {c['mean']}, spread {c['spread']} across "
                f"{', '.join(f'{w} {g:g}' for w, g in c['weeks'].items())})")
        done[name] = row
        # Written after every variant rather than at the end, because the run this has to
        # survive is the one you stop halfway through.
        save(results, done)
        write_summary(out, names, done, rubric_ids, args.analysis_weight, args.spread_penalty)

    say(f"\n{table(done, rubric_ids, names, args.analysis_weight, args.spread_penalty)}")
    say(f"\n==> {results}")


if __name__ == "__main__":
    main()
