#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
stage — what this session already has, and where a re-run picks up.

A week is four stages and three of them are expensive: the read and the letter are model calls
of tens of minutes, the narration is a GPU you are renting by the hour. They are also, all
three, *already on disk* the moment they finish — `claude-analysis.md`, `lifelog-<date>.md`,
`lifelog-<date>-…-voice.wav`. So a run that died at stage 4 has three quarters of a week
sitting in a directory, and typing `just sublimate <session>` again used to mean paying for
the first three again to get at the fourth.

This is the reader that stops that. It looks at a session, says which stages are there, and
names the one command that carries on from where it stopped. `just sublimate` asks it before
it does anything and enters the flow at that point; `just narrate` asks it for stage 4 alone.

    ./tools/stage.py sessions/2026-09-23            # what is there, and what to type
    ./tools/stage.py sessions/2026-09-23 --shell    # the same, as shell assignments
    ./tools/stage.py sessions/2026-09-23 --pick narrate --shell   # that stage regardless

The five answers:

    full      nothing but the week — the whole run, the way it has always gone
    letter    the read is there and the letter is not → petrograph's own --resume, which
              reuses the diff and the read and reopens the model session that wrote them
    narrate   the letter is there and the audio is not → stage 4 alone, no model call
    mix       the voice is there and the master is not → the mixer alone, seconds, local
    done      all four, and nothing to do but say so

WHY THE COMMANDS ARE BUILT HERE. Re-entering at stage 4 means reconstructing the command line
weekly_review.py's stage 4 would have run — the letter as the input, the *session input's*
name for the output (`_names.voice_name`, which is why the wav is called -edit-me-voice), the
session's `voice-sample:` and its bed. That is a copy of something petrograph already knows,
and the way to keep a copy honest is to make it out of the same parts: every name here comes
from `_names`/`_session`, the engine from `_speech.resolve_engine`, and the bed is split by
`_speech.music_tracks` rather than on commas. What is left is the flag list itself, which is
the one thing that has to be mirrored by hand — see weekly_review.py's stage 4, and change
this when that changes.

Nothing here writes anything or touches the box.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# The petrograph checkout whose stage names these are. A submodule by default; PETROGRAPH_ROOT
# moves it, exactly as it moves the narrator's.
PETROGRAPH = Path(os.environ.get("PETROGRAPH_ROOT") or (ROOT / "petrograph")).resolve()
NARRATOR = ROOT / "tts" / "remote_chatterbox.py"
MIXER = PETROGRAPH / "tools" / "mix_music.py"

sys.path.insert(0, str(PETROGRAPH / "tools"))
try:
    from _names import master_name, voice_name
    from _session import ANALYSIS, DIFF, INPUT, STUB_MARKER, compaction
    from _speech import engine_label, engine_tool, music_tracks, resolve_engine
except ImportError as exc:  # a checkout that was never initialised, or moved
    sys.exit(f"stage: petrograph's tools are not importable from {PETROGRAPH} ({exc}).\n"
             "  Set PETROGRAPH_ROOT, or run `git submodule update --init`.")

# weekly_review.py's, so `music: /path/x.mp3 # the bed` reads here the way it reads there.
COMMENT_RE = re.compile(r"\s+#(?:\s.*)?$")

ORDER = ("full", "letter", "narrate", "mix", "done")


def front_matter(path: Path) -> dict[str, str]:
    """The flat keys at the top of a session's input file.

    A deliberately small copy of weekly_review.parse_front_matter: importing that module means
    importing the pipeline — _llm, the CLIs it shells out to, its own dependency list — to read
    six lines of YAML-ish, and this tool has to stay cheap enough for `just sublimate` to ask
    it before every run. Same comment rule, same flat-keys-only reading, no body.
    """
    raw = path.read_text()
    if not raw.startswith("---\n"):
        sys.exit(f"{path}: expected YAML front-matter, starting with a --- line.")
    end = raw.find("\n---", 4)
    if end == -1:
        sys.exit(f"{path}: front-matter is never closed by a --- line.")
    fields: dict[str, str] = {}
    for line in raw[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#") or line.startswith((" ", "\t", "-")):
            continue
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = COMMENT_RE.sub("", value).strip()
    return fields


def resolve_in(session: Path, raw: str) -> Path:
    """A front-matter path, resolved the way weekly_review resolves one: against the session."""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (session / path).resolve()


def written(letter: Path) -> bool:
    """Whether the letter is a letter, rather than the stub a scaffolded session is given.

    `_session.ensure_stub` puts the file there from the start, so that a session shows the
    whole shape of the work at a glance. Existence is therefore not the question; the marker
    is, and it is the same marker `just render` refuses to narrate over.
    """
    return letter.exists() and STUB_MARKER not in letter.read_text()


def look(target: Path) -> dict:
    """Everything a caller needs to know about this session, including what to run next."""
    session = target if target.is_dir() else target.parent
    session = session.resolve()
    inp = session / INPUT
    if not inp.exists():
        sys.exit(f"{session} is not a session — it has no {INPUT} in it.")

    fields = front_matter(inp)
    diff = session / DIFF
    analysis = session / ANALYSIS
    letter = session / compaction(session)
    # The audio is named off the *input*, not off the letter — voice_name is what stage 4 hands
    # its engine, and it is where the -edit-me in lifelog-<date>-edit-me-voice.wav comes from.
    voice = voice_name(inp)
    master = master_name(voice)

    try:
        engine = resolve_engine(fields.get("engine"))
        tool = engine_tool(engine)
    except ValueError as exc:
        sys.exit(f"{inp}: {exc}")

    music = [resolve_in(session, str(t))
             for t in music_tracks([fields["music"]], base=session)] if fields.get("music") else []
    mix_engine = fields.get("music-engine") or "oss"

    # Which stage this session is at: the first thing that is not there yet.
    if not written(letter):
        stage = "letter" if analysis.exists() and diff.exists() else "full"
    elif not voice.exists():
        stage = "narrate"
    elif music and not master.exists():
        stage = "mix"
    else:
        stage = "done"

    return {
        "session": session, "input": inp, "diff": diff, "analysis": analysis,
        "letter": letter, "voice": voice, "master": master,
        "music": music, "mix_engine": mix_engine, "engine": engine, "tool": tool,
        "fields": fields, "stage": stage,
    }


def narrate_cmd(s: dict) -> list[str]:
    """Stage 4's command line, rebuilt. Mirror of weekly_review.py's stage 4 — keep in step."""
    cmd = [str(s["tool"]), str(s["letter"]), "--normalize", "--out", str(s["voice"])]
    if s["engine"] == "eleven":
        # An ElevenLabs voice id and its pause-hyphens, which mean nothing to a local cloner.
        cmd += ["--voice", s["fields"].get("voice") or "WeAAwKYcS06VmXw086yZ",
                "--chunk", "300", "--sentence-pause", "1"]
    elif sample := s["fields"].get("voice-sample"):
        cmd += ["--voice-sample", str(resolve_in(s["session"], sample))]
    for track in s["music"]:
        cmd += ["--music", str(track)]
    if s["music"] and s["fields"].get("music-engine"):
        cmd += ["--mix-engine", s["mix_engine"]]
    return cmd


def mix_cmd(s: dict) -> list[str]:
    """The mixer alone, on a voice that was rendered but never got its bed under it.

    The same hand-off remote_chatterbox.local_mix makes, and the same one chatterbox_tts.py
    makes at the end of a local render: same mixer, same --out convention, same --engine.
    """
    cmd = [str(MIXER), str(s["voice"])]
    for track in s["music"]:
        cmd += ["--music", str(track)]
    return cmd + ["--out", str(s["master"]), "--engine", s["mix_engine"]]


def command_for(s: dict, stage: str) -> list[str]:
    return {"narrate": narrate_cmd, "mix": mix_cmd}.get(stage, lambda _s: [])(s)


def human(s: dict, stage: str) -> None:
    def mark(done: bool) -> str:
        return "✓" if done else "·"

    rel = s["session"].name
    print(f"{s['session']}")
    print(f"  {mark(s['diff'].exists())} 1/4 diff       {s['diff'].name}")
    print(f"  {mark(s['analysis'].exists())} 2/4 act read   {s['analysis'].name}")
    print(f"  {mark(written(s['letter']))} 3/4 letter     {s['letter'].name}")
    print(f"  {mark(s['voice'].exists())} 4/4 narration  {s['voice'].name}"
          f"  ({engine_label(s['engine'])})")
    if s["music"]:
        print(f"  {mark(s['master'].exists())}     master     {s['master'].name}"
              f"  ({', '.join(t.name for t in s['music'])} under it)")
    print()
    if stage == "done":
        print("Every stage is on disk. Nothing to do — and to do one of them again:")
        print(f"    just narrate {rel}              # the letter read aloud again")
        print(f"    rm -r {s['session']}   # the week from the beginning")
        return
    what = {
        "full": ("the whole run — the read, the letter and the narration",
                 f"just sublimate {rel}"),
        "letter": ("the letter and the narration — the diff and the read are reused, "
                   "and the model session that wrote them is reopened",
                   f"just sublimate {rel}"),
        "narrate": ("the narration alone — no model call, nothing billed but the box",
                    f"just narrate {rel}"),
        "mix": ("the mix alone — the voice is rendered, the bed is not under it yet; "
                "seconds, on this machine", f"just narrate {rel} --mix-only"),
    }[stage]
    print(f"Picks up at: {what[0]}.")
    print(f"    {what[1]}")


def shell(s: dict, stage: str) -> None:
    """The same answer as `eval`-able assignments, which is how the justfile asks.

    `cmd` is a bash array rather than a string because a bed can be `{Label Co., Ltd.}/1.mp3`
    and word-splitting a quoted string would take that apart. Same reason evanescent prints
    its conninfo this way: the shell on the other end should not have to parse anything.
    """
    out = [
        f"stage={stage}",
        f"session={shlex.quote(str(s['session']))}",
        f"letter={shlex.quote(str(s['letter']))}",
        f"voice={shlex.quote(str(s['voice']))}",
        f"master={shlex.quote(str(s['master']))}",
        f"engine={shlex.quote(engine_label(s['engine']))}",
        # What the run has already got, in one line a recipe can print back.
        "have=" + shlex.quote(", ".join(
            name for name, there in (("diff", s["diff"].exists()),
                                     ("read", s["analysis"].exists()),
                                     ("letter", written(s["letter"])),
                                     ("narration", s["voice"].exists()),
                                     ("master", s["master"].exists() if s["music"] else None))
            if there)),
        "cmd=(" + " ".join(shlex.quote(a) for a in command_for(s, stage)) + ")",
    ]
    print("\n".join(out))


def main() -> None:
    ap = argparse.ArgumentParser(
        description="What a session already has, and the command that carries on from there.")
    ap.add_argument("session", type=Path, help="The session directory, or the edit-me.md in it.")
    ap.add_argument("--shell", action="store_true",
                    help="Print eval-able assignments instead of prose.")
    ap.add_argument("--pick", choices=ORDER, metavar="STAGE",
                    help=f"Answer for this stage rather than the one the session is at "
                         f"({', '.join(ORDER)}). `just narrate` uses it to mean stage 4 "
                         "whether or not there is already an mp3.")
    args = ap.parse_args()

    if not args.session.exists():
        sys.exit(f"No such session: {args.session}")
    s = look(args.session)
    stage = args.pick or s["stage"]
    (shell if args.shell else human)(s, stage)


if __name__ == "__main__":
    main()
