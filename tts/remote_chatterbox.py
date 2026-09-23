#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
remote_chatterbox — narrate the letter with Chatterbox, on the GPU box, and mix it here.

petrograph's chatterbox_tts.py keeps the letter on your machine, which is the point of it, and
pays for that with time: a long letter is tens of minutes of synthesis on a laptop's GPU. This
script is the same engine on a card that is thirty times faster, and the letter still does not
go to anybody — it goes to an EC2 instance in your own account, over an SSH tunnel, and the
instance is stopped afterwards. "Nothing leaves this machine" becomes "nothing leaves machines
you are paying for", which is a different sentence and worth saying accurately.

WHY THIS IS NOT A FLAG IN PETROGRAPH. It presents chatterbox_tts.py's own command line and is
named as the engine, so `--tts-engine <this file>` is the whole of the integration and
petrograph learns nothing about ssh, AWS, or which host anything is on. _speech.resolve_engine
takes a path for exactly this. The seam is a command line; on this side of it, this file is
allowed to know about scp.

WHAT RUNS WHERE, and why the split is here rather than at the obvious place. The box does
synthesis and nothing else:

    here                       there
    ────                       ─────
    the markdown          ───▶  chatterbox_tts.py  (torch, the weights, the L40S)
    --voice-sample        ───▶  the reference clip
                          ◀───  the narration, one mp3
    mix_music.py                ·  the backing track never goes up
    (oss chain, or VST)         ·  and the VST chain could not run there anyway

Mixing stays here because it is seconds of work that wants files the box has no reason to
hold, and because `--mix-engine vst` drives licensed plug-ins that exist only on this Mac.
chatterbox_tts.py already treats the mix as a separable tail — it shells out to mix_music.py
after writing the voice mp3 — so this splits along a line the code already draws: we run it
there *without* --music, bring the voice back, and hand that to the same mixer with the same
arguments. The master is byte-for-byte the file a local run would have produced, modulo the
synthesis itself.

THE CHUNK CACHE LIVES ON THE BOX, under /opt/sublimation. That is the right place for it —
it is keyed by text and generation parameters, and re-rendering an edited letter should reuse
the sixty chunks that did not change. It survives `evanescent stop`, because that keeps the
EBS volume, and it dies with `evanescent destroy`, because that is what destroy means.

WHEN THE CARD IS FULL. Stage 4 arrives on a box that has just spent forty minutes writing,
and ollama does not put its weights away when it finishes: OLLAMA_KEEP_ALIVE holds the model
resident for half an hour after the letter, against the next request that is not coming. On a
48GB L40S that is nine gigabytes nobody is going to miss. On the fallback rung of evanescent's
ladder — g5.12xlarge, four A10Gs of 22GiB each, which is what you get in an L40S shortage —
ollama spreads a shard across every card, three workers ask for 4.5GiB apiece on card zero,
and the run dies with `torch.OutOfMemoryError` after the two expensive stages are already
paid for. So the render no longer assumes the card is empty: it asks nvidia-smi what is free,
renders on the emptiest card rather than always on cuda:0, asks ollama to put the model away
when what is free is not enough, and sizes the fan-out to what is actually there. An OOM that
gets through all of that is retried with fewer workers — the chunk cache keeps every chunk
that finished, so a retry resumes rather than restarts, and the second attempt is cheap.

Unloading ollama is safe and is not a cancellation: `ollama stop` is a keep-alive-zero request
that queues behind whatever is in flight and expires the runner once it is idle, and the next
request reloads from the weights on the volume. It costs a model load, which is seconds, and
it is only asked for when there is not enough memory to render without it.

FIRST RUN IS SLOW and it is not the network: torch is a ~2.5GB wheel and the Chatterbox
weights are another ~2GB, both fetched on the box the first time anything asks for them.
`--prewarm` does that on an empty sentence, and `just sublimate` starts it in the background
and leaves it running while opencode reads the week — the slowest stage of a run, and the one
that asks nothing of the GPU. So for the first few minutes of a cold box there are two of
these in flight, and they would fetch the same 4.5GB into the same caches; the lock in
one_at_a_time() is what makes the second one wait rather than race.

THE VOICE is a clip, not a name. Chatterbox clones zero-shot from ten to twenty seconds of
somebody speaking and has no catalogue to look an id up in, so `--capture-reference` cuts a
window out of a recording you already have and installs it as the one every letter from here
on is read in. That is `just voice <recording>`, and resolve_sample() is the whole precedence.

Usage — as an engine, which is how it is meant to be reached:

    weekly_review.py sessions/2026-08-20 --tts-engine tts/remote_chatterbox.py
    just sublimate 2026-08-20

...and directly, which is the same thing:

    tts/remote_chatterbox.py letter.md --normalize --out voice.mp3
    tts/remote_chatterbox.py --prewarm
    tts/remote_chatterbox.py --capture-reference "~/Voice Memos/reading.m4a"
"""

from __future__ import annotations
import argparse
import ast
import contextlib
import fcntl
import hashlib
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
EVANESCENT = ROOT / "evanescent" / "evanescent.py"
EVANESCENT_PY = ROOT / "evanescent" / ".venv" / "bin" / "python"

# The petrograph checkout this narrates for. A submodule by default; PETROGRAPH_ROOT moves it,
# which is what a run against your own clone wants.
PETROGRAPH = Path(os.environ.get("PETROGRAPH_ROOT") or (ROOT / "petrograph")).resolve()

# Where the box keeps its half of a render. One directory: the scripts we push, the letter,
# the reference clip, the output, and the chunk cache that makes a re-render cheap.
REMOTE_DIR = "/opt/sublimation"
REMOTE_CACHE = f"{REMOTE_DIR}/tts-cache"
# Dropped once torch and the weights are on the box's volume, so a second prewarm costs one
# ssh instead of a model load. It lives on the volume it is a claim about: `evanescent stop`
# keeps both, `destroy` deletes both, and the marker cannot outlive what it vouches for.
REMOTE_WARM = f"{REMOTE_DIR}/.warm"

# Only one chatterbox run on the box at a time. Held here rather than there because both ends
# are started from this machine — see one_at_a_time().
LOCK = ROOT / ".sublimation" / "narrator.lock"

# How many chunks the box synthesises at once. Set here rather than in chatterbox_tts.py's own
# default because it is a fact about a machine, and this is the only file that knows which
# machine: evanescent's g6e.xlarge, one L40S and four vCPUs. The GPU is nowhere near the limit
# — a worker's copy of the model is a couple of gigabytes against forty-eight — but the decode
# loop is Python and the watermarker is numpy on the CPU, so the cores run out first. Three
# workers and a core left for the parent and for sshd.
#
# It does not change the audio. Chunks are independent and each worker seeds its own sampler,
# so this is the same recording, made in less time. Pass --concurrency yourself to override.
REMOTE_CONCURRENCY = 3

# What one Chatterbox worker holds on the card: its own copy of the model, plus the activations
# for the chunk it is on. Measured off the OOM that made any of this necessary — three workers
# on a 22GiB A10G reported 4.09, 4.13 and 4.39 GiB — and rounded up, because what has to fit is
# the peak and a traceback only prints the instant it was taken at.
WORKER_VRAM_GIB = 4.6
# Not spoken for by that number: each worker's CUDA context, cuDNN's workspaces, and whatever
# fragmentation the allocator is carrying. A worker that fits with a hundred megabytes to spare
# does not fit.
VRAM_HEADROOM_GIB = 1.0
# How long to wait for ollama's weights to actually leave the card after it has been asked to
# put them away. The unload is asynchronous and queues behind any request still in flight.
UNLOAD_WAIT_SECONDS = 25

# What has to be up there for chatterbox_tts.py to run: itself, and every sibling module it
# imports, and every sibling those import. Walked rather than listed, because a list is a copy
# of something the imports already say and it went stale the first time a helper grew an import
# of its own: _names.py started asking _session.py which file in a session is the letter, the
# push kept sending the five files it had always sent, and the box died on the import four
# minutes into a render. audio_dsp.py still never goes up, for the same reason it was never in
# the list — nothing on this path imports it, only mix_music.py does, and mixing is local.
ENTRY = "tools/chatterbox_tts.py"

# Where the clip this narrator clones lives when the session does not name one. Under this
# repo rather than in the submodule: a voice print is a recording of a person, `voices/` is
# gitignored, and petrograph's own copy is named for somebody else.
#
# `just voice` writes narrator.wav, and that is the answer for anybody who used it. The rest
# of the directory is read too, because a clip you already have is a file you should be able
# to drop in — an mp3 off a phone, an m4a out of Voice Memos — without transcoding it first
# to satisfy a filename. Extensions rather than "any file", so a .DS_Store never becomes a
# voice; an explicit --voice-sample is not filtered, since naming a file is saying you mean it.
VOICES_DIR = ROOT / "voices"
VOICE_SAMPLE = VOICES_DIR / "narrator.wav"
AUDIO_SUFFIXES = (".wav", ".mp3", ".m4a", ".flac", ".aif", ".aiff", ".ogg", ".opus", ".caf",
                  ".wma", ".aac")
# …and the same answer for one run, without installing anything. `just sublimate` has no flag
# to reach the narrator with — petrograph invokes it, not the justfile — so an environment
# variable is the channel that exists.
VOICE_SAMPLE_ENV = "SUBLIMATION_VOICE_SAMPLE"

# chatterbox_tts.py falls back to this clip when no --voice-sample is given, and looks for it
# under its own repo root — which on the box is /opt/sublimation, where it will never be. Left
# alone, that is a silent divergence in the one direction that matters: the same session would
# be cloned in your captured voice here and read by the built-in speaker there, with nothing
# in either run's output to say so. So this side resolves the default too, and uploads it.
DEFAULT_VOICE_SAMPLE = PETROGRAPH / "share" / "voices" / "victoria.wav"

SAMPLE_RATE = 24_000  # S3GEN_SR, as chatterbox_tts.py has it — what a reference clip is cut to
REFERENCE_SECONDS = 20  # long enough for turbo's >5s floor with room to spare
MIN_REFERENCE_SECONDS = 5.0


def say(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str) -> "None":
    sys.exit(f"remote_chatterbox: {msg}")


@contextlib.contextmanager
def one_at_a_time(what: str):
    """Serialise the box's chatterbox runs — a prewarm and a render are the same install.

    `just sublimate` fires the prewarm off and gets on with the week: opencode's read takes
    minutes, wants no GPU, and is exactly the window in which a cold box should be fetching
    torch. The cost of that is an overlap — stage 4 arrives while the prewarm may still be
    downloading — and two uv processes resolving the same wheels into the same cache, on a
    card with one letter's worth of memory to spare, is not a thing to find out about forty
    minutes into a render.

    The lock is a file on this machine because both ends are launched from this machine, and
    a flock is cheaper and more honest than a lease held over ssh: if this process dies, the
    kernel drops it. Only the remote half is inside it — the mix that follows a render is
    local work the box has no part in, and holding the box's lock through it would make one
    render wait on another's ffmpeg.
    """
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            say(f"    waiting for another narrator run to finish before {what}")
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# ── the box ───────────────────────────────────────────────────────────────────────────────

def conninfo() -> dict[str, str]:
    """Where the box is, asked of the tool that owns that question.

    evanescent.py's `conninfo` prints the same shell assignments its own ssh uses, so the key,
    the known_hosts file and the user are its decisions rather than a second copy of them
    here. A box that is stopped or absent fails here, before a letter has been read.
    """
    if not EVANESCENT_PY.exists():
        die(f"{EVANESCENT_PY} is missing — run `just setup` first.")
    p = subprocess.run([str(EVANESCENT_PY), str(EVANESCENT), "conninfo"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        die("no reachable box (`just begin-sublimation` starts one):\n  "
            + (p.stderr.strip() or p.stdout.strip() or "evanescent conninfo failed"))
    info = {}
    for line in p.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k] = shlex.split(v)[0] if v else ""
    for key in ("host", "user", "key", "known_hosts"):
        if not info.get(key):
            die(f"evanescent conninfo said nothing about {key}")
    return info


def ctl_path(info: dict[str, str]) -> Path:
    """The ssh master socket `just tunnel-up` opens, so a render joins it instead of dialling.

    Not under ROOT/.sublimation, where the rest of the box's state lives, because a unix
    socket's path is capped at 104 bytes on macOS and ssh appends a 17-character suffix while
    it binds the master: that directory plus ssh's own %C (40 hex characters) came to 109, and
    every ssh here died with "path too long". A short fixed directory and eight characters of
    hash holds under any checkout depth. Per-uid and 0700 because /tmp is shared.

    Must match ctl_dir and the hash in the justfile — two ends of the same connection.
    """
    d = Path(f"/tmp/.sublimation-{os.getuid()}")
    d.mkdir(exist_ok=True)
    d.chmod(0o700)
    key = f"{info['user']}@{info['host']}:{info.get('port', '')}"
    return d / hashlib.sha256(key.encode()).hexdigest()[:8]


def ssh_opts(info: dict[str, str]) -> list[str]:
    # Mirrors evanescent.ssh_base's options. ControlMaster is the addition: a render is one
    # scp up, one long ssh, and one scp down, and without multiplexing that is three
    # handshakes to a host whose sshd we already have an open connection to.
    ctl = ctl_path(info)
    return [
        "-i", info["key"],
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={info['known_hosts']}",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=4",
        "-o", "LogLevel=ERROR",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={ctl}",
        "-o", "ControlPersist=10m",
    ]


def run_ssh(info: dict[str, str], command: str, *, quiet: bool = False) -> int:
    cmd = ["ssh", *ssh_opts(info), f"{info['user']}@{info['host']}", command]
    if quiet:
        return subprocess.run(cmd, capture_output=True, text=True).returncode
    return subprocess.run(cmd).returncode


def ask_ssh(info: dict[str, str], command: str) -> str:
    """One question for the box, answered in its stdout. Silent about failure.

    Every caller of this is asking the box about itself — what the cards hold, what ollama has
    loaded — and none of those questions is worth failing a render over: an answer that does
    not come back means the render falls back to what it did before any of it was asked.
    """
    p = subprocess.run(["ssh", *ssh_opts(info), f"{info['user']}@{info['host']}", command],
                       capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else ""


def tee_ssh(info: dict[str, str], command: str) -> tuple[int, str]:
    """Run it on the box, show its output as it arrives, and keep a copy.

    The copy is what looks_like_oom() reads. Streaming and capturing both, rather than
    capturing and printing at the end, because a render is forty minutes of progress lines and
    a run that prints nothing until it is over is a run you cannot tell from a hung one.
    """
    proc = subprocess.Popen(["ssh", *ssh_opts(info), f"{info['user']}@{info['host']}", command],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    lines: list[str] = []
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        lines.append(line)
    return proc.wait(), "".join(lines)


def scp(info: dict[str, str], src: list[str], dst: str) -> None:
    p = subprocess.run(["scp", "-q", *ssh_opts(info), *src, dst], capture_output=True, text=True)
    if p.returncode != 0:
        die(f"scp failed:\n  {p.stderr.strip()}")


def sibling_imports(src: Path) -> list[str]:
    """The `_foo` modules `src` imports — parsed, so a name inside a docstring is not one.

    Sibling helpers in petrograph are the single-underscore names; `__future__` is not one of
    them, and a dotted or relative import would not be a sibling either.
    """
    names = []
    for node in ast.walk(ast.parse(src.read_text())):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            names.append(node.module or "")
        elif isinstance(node, ast.Import):
            names += [a.name for a in node.names]
    return [n for n in names if n.startswith("_") and not n.startswith("__")]


def push_list() -> tuple[list[str], list[str]]:
    """ENTRY and everything reachable from it, and whatever of that is not in the checkout.

    A module that will not be found is reported rather than skipped: skipping it would push a
    set that imports something absent, which is the failure this walk exists to prevent, only
    discovered on the box instead of here.
    """
    order: list[str] = []
    missing: list[str] = []
    queue = [ENTRY]
    while queue:
        rel = queue.pop(0)
        if rel in order or rel in missing:
            continue
        src = PETROGRAPH / rel
        if not src.exists():
            missing.append(rel)
            continue
        order.append(rel)
        queue += [f"tools/{name}.py" for name in sibling_imports(src)]
    return order, missing


def push_tools(info: dict[str, str]) -> None:
    """The narrator itself, pushed per-run rather than installed.

    Per-run because the box is ephemeral and petrograph is not: a `destroy` must not be able
    to leave a months-old chatterbox_tts.py running against a letter written by a newer one.
    A handful of small files over an already-open connection is not worth caching.
    """
    push, missing = push_list()
    if missing:
        die(f"petrograph checkout at {PETROGRAPH} is missing {', '.join(missing)}.\n"
            "  Set PETROGRAPH_ROOT, or run `git submodule update --init`.")
    run_ssh(info, f"mkdir -p {REMOTE_DIR}/tools {REMOTE_CACHE}", quiet=True)
    scp(info, [str(PETROGRAPH / f) for f in push],
        f"{info['user']}@{info['host']}:{REMOTE_DIR}/tools/")
    run_ssh(info, f"chmod +x {REMOTE_DIR}/tools/*.py", quiet=True)


# ── the voice ─────────────────────────────────────────────────────────────────────────────

def resolve_sample(explicit: Path | None) -> tuple[Path | None, str]:
    """The clip the box clones, and where that answer came from.

    Four answers, most specific first, and each one is somebody saying something different:

      --voice-sample      this session's own `voice-sample:`, handed over by petrograph
      $SUBLIMATION_VOICE_SAMPLE   this run's, for a letter you want in another voice
      voices/             the clip `just voice` installed, or one you dropped in — the
                          standing answer, and see installed_clip() for which of several
      petrograph's        share/voices/victoria.wav, what a local chatterbox_tts.py would
                          have picked up on its own (see DEFAULT_VOICE_SAMPLE)

    None means Chatterbox's built-in speaker, which is a working voice and not yours. The
    reason it is said out loud either way is that the failure mode here is silent: a missing
    clip does not fail, it just reads the letter to you in a stranger's voice.
    """
    if explicit is not None:
        return explicit.expanduser(), "asked for"
    if env := os.environ.get(VOICE_SAMPLE_ENV):
        return Path(env).expanduser(), f"${VOICE_SAMPLE_ENV}"
    if found := installed_clip():
        return found
    if DEFAULT_VOICE_SAMPLE.exists():
        return DEFAULT_VOICE_SAMPLE, "petrograph's, the clip a local run would have used"
    return None, "Chatterbox's built-in speaker — `just voice <recording>` changes that"


def installed_clip() -> tuple[Path, str] | None:
    """Whatever is in voices/, and how it got to be the one.

    narrator.wav first, always: `just voice` writes it, so somebody who ran that gets what
    they asked for even if there is an older clip beside it. Otherwise the audio files there,
    sorted, so the same directory does not mean a different voice on a different day — and if
    there is more than one, the count goes into the reason, which every run prints. Two clips
    in a directory is not an error worth stopping a run over; it is a thing to say.
    """
    if VOICE_SAMPLE.exists():
        return VOICE_SAMPLE, "`just voice`"
    clips = sorted(p for p in VOICES_DIR.glob("*")
                   if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES)
    if not clips:
        return None
    why = "dropped into voices/"
    if len(clips) > 1:
        why += f", first of {len(clips)} by name"
    return clips[0], why


def probe_duration(path: Path) -> float:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0


def capture_reference(src: Path, dst: Path, seconds: int, start: float) -> None:
    """Cut a reference window out of a recording you already have.

    The same four ffmpeg arguments as chatterbox_tts.py's capture_reference, and deliberately
    a copy rather than a call: that tool's script header pulls in chatterbox and torch, so
    reaching it to trim twenty seconds of an mp3 would install 2.5GB of wheels on this Mac —
    the exact thing the box exists to avoid. What it does is worth keeping identical, so a
    clip cut here and one cut there are the same clip: seek, take the window, collapse to
    mono at the model's own rate, and trim the leading silence so it opens on speech rather
    than on room tone. No compression, no EQ, no loudness matching — every one of those is a
    colour the clone would inherit.
    """
    if not src.exists():
        die(f"no such recording: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-ss", f"{start:g}", "-t", str(seconds), "-i", str(src),
         "-ac", "1", "-ar", str(SAMPLE_RATE),
         "-af", "silenceremove=start_periods=1:start_duration=0.05:start_threshold=-50dB",
         str(dst)], capture_output=True, text=True)
    if p.returncode != 0:
        die(f"ffmpeg could not read {src.name}:\n  {p.stderr.strip()}")
    secs = probe_duration(dst)
    say(f"    the narrator now reads in {dst.name} — {secs:.1f}s from {src.name}")
    say(f"    {dst}")
    if secs < MIN_REFERENCE_SECONDS:
        say(f"    warning: {secs:.1f}s is short. Chatterbox wants 10-20s — try "
            "--reference-start past the quiet opening, or a longer --reference-seconds.")


def as_upload(sample: Path) -> Path:
    """The clip in a format the box is certain to read.

    A wav goes up as it is. Anything else — an mp3 dropped into voices/, an m4a named in a
    session's `voice-sample:` — is decoded here first. Not because Chatterbox refuses one:
    it loads the reference through librosa, which reads an mp3 through whichever libsndfile
    came with the wheel on that box. That is a dependency this side cannot see and should not
    be betting a run on, and the bet would be called at stage 4 — after the read and the
    letter have been written and paid for. ffmpeg is here, it is already required, and the
    conversion is the same mono-at-the-model's-rate the cloner does internally anyway.

    One fixed name, rewritten per run. The encode is deterministic, so the same clip becomes
    the same bytes every time, and the box's chunk cache — keyed by the content of the
    reference, not its path — goes on hitting across runs.
    """
    if sample.suffix.lower() == ".wav":
        return sample
    dst = ROOT / ".sublimation" / "voice-converted.wav"
    dst.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-i", str(sample), "-ac", "1", "-ar", str(SAMPLE_RATE), str(dst)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        die(f"ffmpeg could not read the reference clip {sample.name}:\n  {p.stderr.strip()}")
    say(f"    {sample.name} → {SAMPLE_RATE // 1000}kHz mono wav, so the box need not decode it")
    return dst


# ── the card ──────────────────────────────────────────────────────────────────────────────

def gpus(info: dict[str, str]) -> list[tuple[int, float, float]]:
    """(index, total GiB, free GiB) for every card on the box, emptiest last. [] if unasked."""
    out = ask_ssh(info, "nvidia-smi --query-gpu=index,memory.total,memory.free "
                        "--format=csv,noheader,nounits")
    table = []
    for line in out.splitlines():
        parts = [f.strip() for f in line.split(",")]
        if len(parts) == 3 and all(f.isdigit() for f in parts):
            index, total, free = (int(f) for f in parts)
            table.append((index, total / 1024, free / 1024))
    return sorted(table, key=lambda g: g[2])


def fits(free_gib: float) -> int:
    """How many workers that much free memory will hold."""
    return max(0, int((free_gib - VRAM_HEADROOM_GIB) // WORKER_VRAM_GIB))


def release_ollama(info: dict[str, str]) -> list[str]:
    """Ask ollama to put its weights away, and say which models it had.

    `ollama stop` is a keep-alive-zero request rather than a kill: it queues behind anything
    still in flight and expires the runner when it goes idle, so this cannot cut a generation
    short — the worst it can do is cost the next request a model load off the box's own volume.
    That is the trade this makes, and it makes it only when the card is too full to render on.

    Asked of every loaded model rather than of the one we expect, because a box that has served
    both the read's model and a retuned variant has two resident and half the memory is the
    other one's.
    """
    # The same PATH the render exports, and for the same reason: ollama installs itself into
    # /usr/local/bin, which a non-login `ssh host cmd` is not promised to have.
    path = "export PATH=/usr/local/bin:$PATH; "
    loaded = ask_ssh(info, path + "ollama ps 2>/dev/null | awk 'NR>1 && NF {print $1}'").split()
    if not loaded:
        return []
    run_ssh(info, path + "; ".join(f"ollama stop {shlex.quote(m)}" for m in loaded), quiet=True)
    return loaded


def plan(info: dict[str, str], asked: int | None) -> tuple[int | None, int, str]:
    """Which card to render on, and how many workers to put on it.

    Three facts decide it and all three are properties of this box at this moment, which is why
    none of them is a constant: how many cards there are, what is free on the emptiest, and
    whether ollama is still holding the letter's model. The first is the one that made the
    difference on the box this was written for — evanescent's fallback rung is four A10Gs, and
    pinning to the emptiest instead of always taking cuda:0 is most of the fix on its own.

    `asked` is an explicit --concurrency, and it is obeyed: naming a number is saying you mean
    it, and the OOM retry below is still there if it turns out you did not. Unasked, the number
    is whatever fits, capped at REMOTE_CONCURRENCY — above that the box's four vCPUs are the
    limit rather than its memory, which is what that constant has always been about.

    A box that does not answer nvidia-smi gets the old behaviour: no pinning, REMOTE_CONCURRENCY
    workers, and whatever happens next handled by the retry.
    """
    want = asked or REMOTE_CONCURRENCY
    cards = gpus(info)
    if not cards:
        return None, want, "the box did not answer nvidia-smi — taking it at its word"

    index, total, free = cards[-1]
    where = f"{len(cards)} card(s), emptiest is cuda:{index} with {free:,.1f} of {total:,.1f} GiB free"
    if fits(free) < want:
        # Not enough for the fan-out we want, and the likeliest reason is upstairs in this same
        # run: ollama, holding the model that wrote the letter against a request that is not
        # coming. Asking for it back costs nothing when there is nothing to give back.
        if freed := release_ollama(info):
            say(f"    the card is full — asking ollama to put {', '.join(freed)} away")
            deadline = time.time() + UNLOAD_WAIT_SECONDS
            while time.time() < deadline:
                cards = gpus(info) or cards
                if fits(cards[-1][2]) >= want:
                    break
                time.sleep(2)
            index, total, free = cards[-1]
            where = (f"{len(cards)} card(s), emptiest is cuda:{index} with "
                     f"{free:,.1f} of {total:,.1f} GiB free")

    workers = want if asked else max(1, min(want, fits(free)))
    if workers < want:
        where += f" — {workers} worker(s) rather than {want}"
    return index, workers, where


def looks_like_oom(output: str) -> bool:
    """Whether what came back is the card being full rather than the letter being wrong."""
    low = output.lower()
    return ("out of memory" in low or "outofmemoryerror" in low
            or "cuda error: out of memory" in low)


# ── the render ────────────────────────────────────────────────────────────────────────────

def split_concurrency(passthrough: list[str]) -> tuple[list[str], int | None]:
    """Lift --concurrency out of the passthrough, so this file owns the number.

    It is the one flag both ends of this have an opinion about: yours is the ceiling, and the
    retry has to be able to lower it. Left in the passthrough it would arrive as a second
    --concurrency on the same command line and the box would keep whichever argparse saw last,
    which is a coin toss to be settling the fan-out on.
    """
    rest: list[str] = []
    asked: int | None = None
    skip = False
    for i, arg in enumerate(passthrough):
        if skip:
            skip = False
            continue
        if arg == "--concurrency":
            if i + 1 < len(passthrough) and passthrough[i + 1].isdigit():
                asked = int(passthrough[i + 1])
                skip = True
            continue
        if arg.startswith("--concurrency="):
            value = arg.split("=", 1)[1]
            if value.isdigit():
                asked = int(value)
            continue
        rest.append(arg)
    return rest, asked


def remote_render(info: dict[str, str], markdown: Path, out: Path,
                  sample: Path | None, passthrough: list[str]) -> None:
    """Synthesise there, land the mp3 here. No --music: the mixer runs on this machine."""
    push_tools(info)
    stem = f"{int(time.time())}-{os.getpid()}"
    remote_md = f"{REMOTE_DIR}/{stem}.md"
    remote_out = f"{REMOTE_DIR}/{stem}.mp3"

    uploads = [str(markdown)]
    remote_sample = None
    if sample:
        sample = as_upload(sample)
        remote_sample = f"{REMOTE_DIR}/{stem}{sample.suffix}"
        uploads.append(str(sample))
    # One scp for both files, then move them into the names the run will use. scp of several
    # sources wants a directory as its target, so the rename is a separate step.
    scp(info, uploads, f"{info['user']}@{info['host']}:{REMOTE_DIR}/")
    moves = [f"mv {shlex.quote(REMOTE_DIR + '/' + markdown.name)} {shlex.quote(remote_md)}"]
    if remote_sample:
        moves.append(f"mv {shlex.quote(REMOTE_DIR + '/' + sample.name)} "
                     f"{shlex.quote(remote_sample)}")
    run_ssh(info, " && ".join(moves), quiet=True)

    # The card decides the fan-out, and it is asked now rather than assumed — see plan().
    passthrough, asked = split_concurrency(passthrough)
    device, workers, where = plan(info, asked)
    say(f"    {where}")

    def line(device: int | None, workers: int) -> str:
        cmd = [f"{REMOTE_DIR}/tools/chatterbox_tts.py", remote_md,
               "--out", remote_out, "--cache-dir", REMOTE_CACHE, "--device", "cuda",
               "--concurrency", str(workers), *passthrough]
        if remote_sample:
            cmd += ["--voice-sample", remote_sample]
        # PATH: uv went to /usr/local/bin, which a non-login `ssh host cmd` does not always
        # have. CUDA_VISIBLE_DEVICES: the whole run — parent and every worker it spawns — on
        # the card plan() picked, which is how a fan-out stops piling onto cuda:0 on a box
        # whose other three cards are emptier. expandable_segments: the allocator's own answer
        # to the fragmentation its OOM message suggests it, and free where it does not help.
        env = ["export PATH=/usr/local/bin:$PATH"]
        if device is not None:
            env.append(f"export CUDA_VISIBLE_DEVICES={device}")
        env.append("export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True")
        return "; ".join(env) + "; " + " ".join(shlex.quote(a) for a in cmd)

    say(f"    → {info['host']}:{REMOTE_DIR}  ({markdown.stat().st_size:,} bytes of letter)")
    t = time.perf_counter()
    while True:
        code, output = tee_ssh(info, line(device, workers))
        if code == 0:
            break
        # Anything but the card being full is this letter's problem rather than this box's
        # weather, and retrying it would only spend the box's time saying the same thing.
        if not looks_like_oom(output) or workers <= 1:
            die("the remote render failed — its output is above. The chunk cache on the box "
                "keeps whatever finished, so re-running resumes rather than restarts.")
        workers -= 1
        say(f"\n    out of memory with {workers + 1} worker(s) on the card. Every chunk that "
            f"finished is in the box's cache, so this carries on from there — retrying with "
            f"{workers}.")
        if freed := release_ollama(info):
            say(f"    (asking ollama to put {', '.join(freed)} away first)")
            time.sleep(5)
        # The emptiest card may not be the one it was a minute ago — something else on this box
        # may have finished, or ollama may only now have let go.
        device, _, where = plan(info, workers)
        say(f"    {where}")
    say(f"    synthesised in {time.perf_counter() - t:,.0f}s")

    out.parent.mkdir(parents=True, exist_ok=True)
    scp(info, [f"{info['user']}@{info['host']}:{remote_out}"], str(out))
    # The letter is the private thing here; the box keeps its cache but not the document.
    run_ssh(info, f"rm -f {shlex.quote(remote_md)} {shlex.quote(remote_out)}"
                  + (f" {shlex.quote(remote_sample)}" if remote_sample else ""), quiet=True)
    say(f"    ← {out.name}  ({out.stat().st_size / 1024:,.1f} KB)")


def local_mix(out: Path, music: list[Path], mix_engine: str) -> None:
    """The same hand-off chatterbox_tts.py makes, made here instead.

    Kept identical on purpose: same mixer, same --out convention (master_name drops the
    -voice), same --engine. A remotely-narrated letter is mixed by the code that mixes a
    locally-narrated one, so the two differ in where the voice was made and nowhere else.
    """
    sys.path.insert(0, str(PETROGRAPH / "tools"))
    from _names import master_name  # noqa: E402  — after the path is set

    master = master_name(out)
    mixer = PETROGRAPH / "tools" / "mix_music.py"
    say(f"    mixing over {' → '.join(p.name for p in music)} → {master.name} "
        f"({mix_engine} chain, on this machine)")
    subprocess.run([str(mixer), str(out), *[a for p in music for a in ("--music", str(p))],
                    "--out", str(master), "--engine", mix_engine], check=True)


def prewarm(info: dict[str, str]) -> None:
    """Make the box pay for torch and the weights now, on one sentence, rather than later.

    Not cached across `destroy`, and not needed twice on a box you only stop. It writes into
    the same chunk cache a real render uses, so the sentence it synthesises is also the only
    thing it leaves behind — and a marker beside it, so the second call is one ssh rather than
    a model load. The marker lives on the volume it is a claim about, which is the only place
    it can be right: `stop` keeps them together and `destroy` deletes them together. If you
    ever want the check redone, `just box-ssh -- rm /opt/sublimation/.warm` is the whole of it.
    """
    if run_ssh(info, f"test -f {REMOTE_WARM}", quiet=True) == 0:
        say("    torch and the weights are already on the box's volume")
        return
    push_tools(info)
    say("    fetching torch and the Chatterbox weights on the box (first run only, ~5 min)")
    t = time.perf_counter()
    # The emptiest card, and nothing asked to move: a prewarm runs *while* ollama is writing
    # the read, which is the one moment in a week when the model on the card is being used.
    # One sentence needs one worker's worth of memory, so choosing well is the whole of it.
    cards = gpus(info)
    pin = f"export CUDA_VISIBLE_DEVICES={cards[-1][0]}; " if cards else ""
    line = (
        "export PATH=/usr/local/bin:$PATH; " + pin +
        f"printf 'Warming up.\\n' > {REMOTE_DIR}/prewarm.md && "
        f"{REMOTE_DIR}/tools/chatterbox_tts.py {REMOTE_DIR}/prewarm.md "
        f"--out {REMOTE_DIR}/prewarm.mp3 --cache-dir {REMOTE_CACHE} --device cuda && "
        f"rm -f {REMOTE_DIR}/prewarm.md {REMOTE_DIR}/prewarm.mp3 && touch {REMOTE_WARM}"
    )
    if run_ssh(info, line) != 0:
        die("prewarm failed — its output is above.")
    say(f"    ready in {time.perf_counter() - t:,.0f}s")


# ── the command line, which is chatterbox_tts.py's ────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Narrate a markdown letter with Chatterbox on the evanescent box.",
        epilog="Unrecognised flags are passed through to chatterbox_tts.py on the box, so "
               "--exaggeration, --cfg-weight, --seed, --concurrency and the gap settings all "
               f"work. Unasked, the box renders --concurrency {REMOTE_CONCURRENCY}.")
    ap.add_argument("markdown", nargs="?", type=Path)
    ap.add_argument("--out", type=Path, help="Where the narration lands on this machine.")
    ap.add_argument("--voice-sample", type=Path, metavar="WAV",
                    help="Reference clip to clone; uploaded with the letter. Unset, see "
                         "resolve_sample for what is used instead.")
    ap.add_argument("--music", action="append", metavar="PATH[,PATH…]", default=[],
                    help="Backing track(s) for the master. Mixed here, never uploaded.")
    ap.add_argument("--mix-engine", choices=("oss", "vst"), default="oss")
    ap.add_argument("--prewarm", action="store_true",
                    help="Download torch and the weights on the box and exit.")
    ap.add_argument("--capture-reference", type=Path, metavar="AUDIO",
                    help=f"Cut a reference clip out of a recording, install it as "
                         f"{VOICE_SAMPLE.name}, and exit. Touches no box. `just voice`.")
    ap.add_argument("--reference-seconds", type=int, default=REFERENCE_SECONDS, metavar="N",
                    help="Seconds to take for --capture-reference (default: %(default)s).")
    ap.add_argument("--reference-start", type=float, default=0.0, metavar="SECONDS",
                    help="Where to start, to skip a quiet or noisy opening (default: 0).")
    ap.add_argument("--which-voice", action="store_true",
                    help="Print the clip a render would clone, and exit. Touches no box.")
    args, passthrough = ap.parse_known_args()

    # Both of these are answered on this machine, so they come before conninfo: `just doctor`
    # asks which voice on a laptop with no box, and cutting a clip is ffmpeg and nothing else.
    if args.capture_reference is not None:
        capture_reference(args.capture_reference.expanduser(),
                          args.voice_sample.expanduser() if args.voice_sample else VOICE_SAMPLE,
                          args.reference_seconds, args.reference_start)
        return

    sample, why = resolve_sample(args.voice_sample)
    # Whether the answer is actually on disk, worked out once and reported twice: `just doctor`
    # says so on a laptop with no box, and a render says so below, before the letter moves.
    missing = sample is not None and not sample.exists()
    if args.which_voice:
        print(f"{sample} ({why}){' — MISSING' if missing else ''}" if sample else why)
        return

    info = conninfo()

    if args.prewarm:
        with one_at_a_time("warming the box"):
            prewarm(info)
        return

    if not args.markdown:
        ap.error("a markdown file is required (or --prewarm)")
    if not args.markdown.exists():
        die(f"no such file: {args.markdown}")
    if missing:
        # Said here rather than at the scp, which is four minutes of ACT read further on.
        die(f"the reference clip is {sample} ({why}), and there is no such file.")
    out = args.out or args.markdown.with_suffix(".mp3")

    if sample:
        say(f"    cloning {sample.name} ({why})")
    else:
        say(f"    no reference clip: {why}")

    with one_at_a_time("the render"):
        remote_render(info, args.markdown, out, sample, passthrough)

    music = [Path(p.strip()).expanduser()
             for chunk in args.music for p in chunk.split(",") if p.strip()]
    if missing := [p for p in music if not p.exists()]:
        die(f"backing track(s) not found: {', '.join(str(p) for p in missing)}")
    if music:
        local_mix(out, music, args.mix_engine)


if __name__ == "__main__":
    main()
