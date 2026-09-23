#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["tiktoken"]
# ///
"""
context_check — will this week fit in the window, before forty minutes says it did not.

The run is two model calls in one session: the read is asked with the bundle, and the letter
is asked by resuming that same session, so the letter's prompt is the bundle plus the read.
That total is the number that matters, and two separate ceilings sit above it:

  ollama truncates.   Prompt plus generation must fit num_ctx. Over that line it drops the
                      front of the prompt and answers anyway, with a normal 200 and a
                      `truncating input prompt` line in a journal on a box in Virginia. The
                      bundle leads with the ACT prompt and the week context, so the front is
                      the worst possible thing to lose. There is no setting that makes this
                      an error instead: /api/generate takes `truncate: false`, and the
                      OpenAI-compatible endpoint opencode speaks has no field for it.

  opencode compacts.  Past `limit.context - max(limit.output, 20000)` it replaces the head of
                      the session with a summary of itself. The head of this session is the
                      week's diff. A letter written after that is a letter written from a
                      precis, and nothing in the archive afterwards says so.

Neither announces itself, which is why this runs first. It reads the same two files the run
does -- opencode.json for what opencode believes about the model, and the bundle
`--dry-run` just assembled -- and does the arithmetic against the worst case rather than the
likely one: a read allowed to run all the way to limit.output, and a letter allowed the same.

    ./tools/context_check.py SESSION/bundle.md --opencode-config petrograph/opencode.json \
        --letter-prompt experiments/prompts/letter-detailed.md

Exit 0 if the week fits, 1 if it does not, with the arithmetic either way.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import tiktoken

# opencode's default `compaction.buffer`. It reserves the larger of this and the model's
# output limit, so at any context worth having it is this number that decides the line.
COMPACTION_BUFFER = 20_000

# qwen3.8's tokenizer is not on this machine and pulling it would mean torch. cl100k is the
# closest thing to hand and it errs the right way: a 151k-vocabulary BPE trained with more
# recent English generally needs slightly fewer tokens for the same prose, so counting here
# runs a few percent high. Measured against this repo's own files it lands at 3.9 characters
# a token on a diff and 4.4 on a prompt, against the flat 4.0 petrograph estimates with.
ENCODING = "cl100k_base"

GREEN, YELLOW, RED, DIM, BOLD, RESET = (
    ("\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[1m", "\033[0m")
    if sys.stdout.isatty()
    else ("", "", "", "", "", "")
)


def tokens(text: str) -> int:
    return len(tiktoken.get_encoding(ENCODING).encode(text))


def model_limits(config: Path, provider: str) -> tuple[str, int, int]:
    """(model name, context, output) out of the opencode config evanescent wrote.

    Read from there rather than from evanescent's state because this is the file opencode
    actually consults: if the two ever disagree, the run obeys this one.
    """
    try:
        doc = json.loads(config.read_text())
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"{config}: {e}\n  `evanescent.py up` writes it; run the box first.")
    models = doc.get("provider", {}).get(provider, {}).get("models", {})
    if not models:
        sys.exit(f"{config}: no models under provider `{provider}`.")
    if len(models) > 1:
        sys.exit(f"{config}: {len(models)} models under `{provider}`; this expects one.")
    name, spec = next(iter(models.items()))
    limit = spec.get("limit", {})
    ctx, out = limit.get("context"), limit.get("output")
    if not ctx or not out:
        sys.exit(f"{config}: `{name}` has no limit.context/limit.output to check against.")
    return name, int(ctx), int(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bundle", type=Path, help="the bundle.md a --dry-run just wrote")
    ap.add_argument("--opencode-config", type=Path, required=True,
                    help="the opencode.json `evanescent.py up` wrote")
    ap.add_argument("--provider", default="evanescent", help="provider key in that config")
    ap.add_argument("--letter-prompt", type=Path,
                    help="the compaction prompt stage 3 sends; counted if given")
    args = ap.parse_args()

    if not args.bundle.exists():
        sys.exit(f"no bundle at {args.bundle} — run the pipeline with --dry-run first.")

    model, context, output = model_limits(args.opencode_config, args.provider)
    bundle = tokens(args.bundle.read_text())
    letter_prompt = tokens(args.letter_prompt.read_text()) if args.letter_prompt else 0

    # The worst case, not the likely one. The read is allowed to run to its own ceiling —
    # reasoning counts against it — and the letter is allowed the same after it.
    stage2_peak = bundle + output
    stage3_prompt = bundle + output + letter_prompt
    stage3_peak = stage3_prompt + output
    compact_at = context - max(output, COMPACTION_BUFFER)

    print(f"{BOLD}context check{RESET}  {model}")
    print(f"  {DIM}limits from    {args.opencode_config}{RESET}")
    print(f"  window         {context:,} tokens")
    print(f"  bundle         {bundle:,}")
    print(f"  read ceiling   {output:,}  (limit.output; reasoning counts against it)")
    if letter_prompt:
        print(f"  letter prompt  {letter_prompt:,}")
    print(f"  {DIM}stage 2 peak   {stage2_peak:,}{RESET}")
    print(f"  {DIM}stage 3 peak   {stage3_peak:,}  (prompt {stage3_prompt:,}){RESET}")

    problems = []
    if stage3_prompt > compact_at:
        problems.append(
            f"opencode would compact: a {stage3_prompt:,}-token letter prompt is past "
            f"{compact_at:,} (context {context:,} - max(output {output:,}, buffer "
            f"{COMPACTION_BUFFER:,})). The week's diff is the head of that session, so it is "
            f"what gets replaced by a summary."
        )
    if stage3_peak > context:
        problems.append(
            f"ollama would truncate: {stage3_peak:,} tokens against a {context:,} window. "
            f"It drops the front of the prompt, which here is the ACT prompt and the week "
            f"context, and answers as though nothing happened."
        )
    elif stage2_peak > context:
        problems.append(
            f"ollama would truncate the read: {stage2_peak:,} tokens against {context:,}."
        )

    if not problems:
        headroom = min(compact_at - stage3_prompt, context - stage3_peak)
        print(f"  {GREEN}fits{RESET}           {headroom:,} tokens of headroom at the "
              f"tightest of the two ceilings")
        return

    print()
    for p in problems:
        print(f"{RED}would not fit:{RESET} {p}")
    print()
    print(f"{YELLOW}Nothing has been sent yet.{RESET} Either give the box a bigger window —")
    print("    just destroy && just box-up   # after raising context_length in")
    print("                                  # evanescent/evanescent.toml")
    print("  — or make the week smaller, with a shorter `--days` on the hunks.")
    sys.exit(1)


if __name__ == "__main__":
    main()
