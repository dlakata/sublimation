# sublimation

The weekly compaction letter — written, read, and narrated on hardware you are paying for.

[petrograph](https://github.com/altosaar/petrograph) assembles a week of Obsidian notes,
finances and sleep, has a model write an ACT read and a letter about it, and narrates the
letter. By default that means Anthropic writes it and ElevenLabs reads it. This repo is the
same flow with both of those replaced by one EC2 instance in your own account, brought up for
the occasion and stopped afterwards.

Nothing goes to Anthropic and nothing goes to ElevenLabs. It is not "nothing leaves this
machine" — the letter does leave it, for a box you own and then destroy — and that is a
different sentence, so it is the one printed during a run.

**[docs/](docs/README.md) is the rest of it**: what runs where, what a scaffolded session
decides on your behalf, how the narrator is plugged into petrograph without petrograph knowing,
what the two sampler knobs do to the prose, what a run costs, and how to prove the letter
actually went to your server.

## Setup

```bash
just setup                         # submodules, the AWS client's venv, your petrograph secrets
just setup ~/somewhere/petrograph  # …if your existing clone is elsewhere
just doctor                        # what is in place and what is not
```

You also need `uv`, the `opencode` CLI, `ffmpeg`, and AWS credentials with EC2 +
`ssm:GetParameter` + `pricing:GetProducts`. The longer version is in
[docs/](docs/README.md#setup).

## A week

```
just sublimate              # a box if there is not one, this week's context if it is not
                            # there, then the read, the letter and the narration
just end-sublimation        # stop paying for the box
```

Which is this, with the waiting taken out of your hands — and still what you want on a week
you mean to write the week-context paragraph for yourself:

```
just begin-sublimation      # the week's context here, a GPU box there, in parallel
                            # …write your paragraph into the edit-me.md it names, if you want one
just sublimate 2026-08-20   # the read, the letter, the narration
```

`just sublimate` with no session runs `begin-sublimation` first, and both halves of that are
conditional: a box that already exists is used rather than replaced, and a session that
already exists is left exactly as it is. So it is safe to type twice, and safe to type on a
week you have already assembled by hand. Delete the session directory to build a week again —
re-running the providers would swap the attachments out from under a letter written from them.

It is safe to type twice after a run that *failed*, too, and that is the interesting half. Each
of the four stages is on disk the moment it finishes, so `just sublimate <session>` reads the
session first and enters the flow where the session actually is: at the letter if the read is
written, at the narration if the letter is, and nowhere at all if it is finished. A run that
died at the narration costs the narration to finish, not the forty minutes of model calls
above it. `just stages <session>` prints that reading without acting on it, and `just narrate
<session>` is stage 4 on its own — the command for a letter you want read again in a voice or
over a bed you have changed since.

## Every command

`just` with no recipe prints this list; `just doctor` is the one to type when something is off.

**The week**

| | |
|---|---|
| `just sublimate [session] [flags]` | RUN: the read, the letter, the narration. No session means this week, scaffolded first, and a session half-done is carried on rather than redone. |
| `just begin-sublimation [flags]` | START: this week's context here and a GPU box there, in parallel. |
| `just end-sublimation` | STOP: close the tunnel and stop the box. The volume, and its caches, survive. |
| `just narrate [session] [flags]` | Stage 4 alone: a letter that is already written, read aloud again. `--mix-only` re-lays the bed and wakes nothing. |
| `just stages [session]` | Which of the four stages this session has, and the one command that carries on from there. |
| `just petrograph <recipe>` | Everything petrograph can do, run from its own directory. `just petrograph` lists it. |

**The box**

| | |
|---|---|
| `just box-up [flags]` | A box and nothing else. `--no-narration` makes one that only writes. |
| `just status` | State, IP, uptime, and what it has cost so far. |
| `just resume` | Boot a stopped box back up, and reopen the tunnel. |
| `just destroy` | Terminate it and delete the volume — including the TTS chunk cache on it. |
| `just box-ssh -- <cmd>` | A shell on the box, or one command on it. |
| `just box-logs [--ollama] [-f]` | The bootstrap log, or every request ollama served. |
| `just tunnel-up` / `just tunnel-down` | The `localhost:11434` forward, opened or closed on its own. |

**The prose, and the voice**

| | |
|---|---|
| `just voice <recording> [secs] [start]` | Cut a clip and teach the narrator whose voice to read in. |
| `just ab [session] [flags]` | Replay a week you already have through opencode and score what changed. |
| `just retune [--top-k N …]` | Rebuild the model with the sampler `evanescent.toml` now says. Seconds, no download. |

Flags are passed through to the petrograph tool underneath, so `--dry-run`, `--llm claude`,
`--prompt <file>` and the rest of `just petrograph review-and-narrate --help` work from
`sublimate` and `ab`, and `weekly`'s own flags work from `begin-sublimation`. Whatever you pass
wins over what this repo would have chosen.
