# How sublimation works

The commands, and the shortest path to a narrated week, are in the [top-level
README](../README.md). This is everything underneath them: what runs where, what a scaffolded
session decides on your behalf, how the narrator is plugged into petrograph without petrograph
knowing, what the two sampler knobs do to the prose, what a run costs, and how to prove the
letter actually went to your server.

## What runs where

| stage | | where |
|---|---|---|
| 1 | the week's Obsidian diff, finances, Oura | this machine |
| 2 | the ACT read | your Ollama, over an SSH tunnel |
| 3 | the compaction letter | same session, same box |
| 4 | the narration | Chatterbox on the box's L40S |
| 5 | the mix and master | this machine |

Nothing goes to Anthropic and nothing goes to ElevenLabs. It is not "nothing leaves this
machine" — the letter does leave it, for a box you own and then destroy — and that is a
different sentence, so it is the one printed during a run.

Stages 1 and 5 stay here for reasons that are not privacy. The diff reads your live Obsidian
vault; the mix is seconds of work over backing tracks the box has no reason to hold, and
`--mix-engine vst` drives licensed plug-ins that exist only on this Mac.

## Layout

```
justfile                  sublimate, begin-sublimation, end-sublimation, voice, and the box
sublimation.conf          gitignored — the standing answers, e.g. which bed plays under it
petrograph/               submodule — the flow itself, unmodified by this repo
evanescent/               the AWS box: create, stop, start, destroy, and its bootstrap
tts/remote_chatterbox.py  the narrator, wearing chatterbox_tts.py's command line
tools/stage.py            which stages a session already has, and what carries on from there
tools/context_check.py    whether the week fits the window, before forty minutes says it did not
voices/narrator.wav       gitignored — the clip the narrator clones (`just voice`)
sessions/<date>/          gitignored — the week's diff, the letter, and the audio
docs/                     this
```

## What a scaffolded session decides

The session `just sublimate` scaffolds is `--blank-context`: a stand-in title, and a week
context that says none was written, so the read works from the diff and the attachments alone.
That is what makes the one-command flow a flow rather than a pause. Write the paragraph when
you have one; the read is better for it.

The other two decisions are written into the same front-matter when the session is scaffolded:
`llm: opencode`, and an `engine:` pointing at `tts/remote_chatterbox.py`. So a week re-run
weeks later is re-run the way it was written, rather than falling back to petrograph's own
defaults — which are claude and ElevenLabs, and are the two things this repo exists to replace.
Anything you pass wins over them: `just begin-sublimation --llm claude` is still a sentence.

## Where the sessions live

Here, in `sessions/`, not in the submodule. petrograph is code; the archive is yours, and a
submodule's `sessions/` is an empty directory inside a checkout you did not write — left
alone, the archive you have been building would fork in two.

Both petrograph tools are passed `--out-root` explicitly rather than reading `$PETROGRAPH_OUT`
from the environment, because a just `export` does not reach a `mod`'s recipes, and an archive
whose location depends on how you invoked the command is worse than either answer. Set
`PETROGRAPH_OUT` to keep an existing archive where it is; `just setup` will point out one it
finds. `just doctor` prints where this week will land.

`just petrograph <recipe>` reaches everything petrograph can do, run from its own directory.
`just petrograph` lists them. A `mod` rather than an `import`, because petrograph's recipes
call `./tools/*.py` and only resolve from that directory.

Which is also why `begin-sublimation` rewrites one line of what petrograph prints. Its tools
sign off by naming the recipe to type next — `just review-and-narrate <session> --dry-run` —
and under a `mod` that name is `just petrograph review-and-narrate`, while the thing you
actually want next is `just sublimate <date>`, which has the box and the tunnel in it. The
advice is right and only the name is wrong, so the line is rewritten rather than swallowed.

## Picking up where a run stopped

Three of the four stages cost something real — the read and the letter are tens of minutes of
model time, the narration is a GPU you are renting by the hour — and all four leave their
output in the session directory the moment they finish. So `just sublimate <session>` does not
start at stage 1. It asks `tools/stage.py` what is already there and enters the flow at the
first thing that is missing:

| what is on disk | where it enters | what that costs |
|---|---|---|
| the week only | the whole run | the read, the letter, the narration |
| `claude-analysis.md` | petrograph's own `--resume` | the letter and the narration |
| `lifelog-<date>.md` | stage 4 alone | the narration |
| the voice, but no master | the mixer | seconds, on this machine |
| all four | nothing | it says so, and names the two ways to redo one |

The letter's stage is petrograph's `--resume`, which is the one of these that is not simply a
later starting point: it reuses the diff and the read *and reopens the model session that
wrote them*, so the letter is still written with the week in context rather than from the read
as a document. The narration's is the stage-4 command line rebuilt — the letter as the input,
`_names.voice_name` for the output, the session's `voice-sample:` and its bed — which is why
`stage.py` builds every one of those names out of petrograph's own helpers rather than
spelling them again. The flag list itself is the one thing mirrored by hand; it is a copy of
`weekly_review.py`'s stage 4 and says so in both directions.

Two commands fall out of it. `just stages <session>` prints the reading and does nothing with
it. `just narrate <session>` takes stage 4 whether or not there is already an mp3 — for a
letter you want read again after `just voice` changed whose voice reads it, or after
`sublimation.conf` named a bed, or with `--exaggeration 0.7` on the end because the last one
was flat. `--mix-only` stops at the mixer, which does not wake the box at all.

`--dry-run` is deliberately outside all of this: it is a question about what *would* be sent,
so it assembles the whole bundle however finished the session is.

## How the narrator plugs in, and why it is not a flag

`tts/remote_chatterbox.py` presents `chatterbox_tts.py`'s command line and is named as the
engine:

```
weekly_review.py <session> --tts-engine ~/git/sublimation/tts/remote_chatterbox.py
```

That is the whole integration. petrograph's `_speech.resolve_engine` takes a path where it
takes a name, so nothing over there learns what ssh is, and nothing here is a special case of
petrograph's flow. The alternative — a `--remote` flag threaded through `weekly_review.py`
so that stage 4 goes somewhere stages 2, 3 and 5 do not — would have put half of this repo's
concerns inside a repo that should not have them.

The script runs Chatterbox on the box **without** `--music`, pulls back the voice mp3, and
hands it to `mix_music.py` here — the same hand-off `chatterbox_tts.py` makes internally, with
the same arguments. The master is the file a local run would have produced, modulo where the
synthesis happened.

What goes up is `chatterbox_tts.py` and every sibling module it imports, walked from the
imports themselves rather than from a list kept here — so a helper that grows a dependency
over there does not strand a render over here. This needs petrograph's path-engine support,
which is on `main` as of `6217b2e`. Against an older petrograph, `--tts-engine <path>` fails
with "unknown TTS engine".

## What plays under it

A backing bed is the fourth thing a session records, beside who writes it, who narrates it,
and whether it needs writing. It comes from `sublimation.conf`, because most weeks are the
same bed — that is what a bed is — and a thing you would type every week is a thing to write
down once:

```ini
# sublimation.conf
music = ~/beds/rain.flac
```

`just setup` copies it from `sublimation.example.conf`, which documents the rest. It is
gitignored, it is optional, and an absent file, an absent key and an empty value all mean the
same thing: the narration is voice alone. Each new session gets the value as its `music:`
line, and stage 5 mixes the letter over it into the master you play.

`~` is expanded, and a bare relative path is resolved against the conf file. Comma-separate
for several — they play in order and the sequence loops under a long letter — and give those
as absolute paths, since the relative rule is for naming one. Spaces and commas in a name are
fine, which matters more than it sounds: `{Teichiku Records Co., Ltd. TECD-28262}` is what
album folders actually look like. The name is escaped on the way across to petrograph, because
`just` splices a `mod`'s arguments into the recipe as text rather than re-quoting them.

`just doctor` checks the bed is there before a run does, and `just begin-sublimation --music
<path>` overrides the conf for one week rather than playing after it. A session that already
exists keeps its own `music:` line; this is a decision made when a week is scaffolded, and
the audio it has already produced was named for it.

## Setup

```bash
just setup                         # submodules, the AWS client's venv, your petrograph secrets
just setup ~/somewhere/petrograph  # …if your existing clone is elsewhere
just doctor                        # what is in place and what is not
```

`setup` symlinks `.env` and `providers.conf` into the submodule from a petrograph clone you
already use, so your API keys exist once rather than twice. petrograph gitignores both; this
repo names them again, against the day the submodule becomes a plain directory.

You also need `uv` (runs petrograph's tools), the `opencode` CLI, `ffmpeg`, and AWS
credentials with EC2 + `ssm:GetParameter` + `pricing:GetProducts`.

petrograph carries submodules of its own, so a clone wants `--recursive` and an update wants
`git submodule update --init --recursive`. `just setup` does both; `just doctor` names the
nested checkout it needs if one is missing.

## How the letter reads, and the two knobs that decide it

A 27B on your own box will not write what Opus writes. But most of the distance between the
two, on the first attempt here, was not the model — it was that nothing had been set.

**The sampler.** Nothing in the chain set one, so the run inherited whatever the model shipped.
`ollama show qwen3.8:27b --modelfile` says that is `top_k 20`, `min_p 0` and `repeat_penalty 1`
— its temperature and top_p are already 1.0 and 0.95. Twenty candidates a token is most of why a
letter comes back flat, and all of why it finds one rhetorical figure and reaches for it in every
paragraph. The defaults now are `top_k 0`, `min_p 0.05` and `repeat_penalty 1.05` in the
Modelfile. `temperature` and `top_p` also ride with each request, set on the agent — at these
values they match what the model already had, and they are there because those two are the pair
you can sweep without an `ollama create`. Dropping `top_k` lets the model reach the tail where the
distribution is genuinely flat — word choice, image, figure — while `min_p` still clamps it hard
where it is peaked, which is names, dates and syntax. That pairing buys variety without invented
specifics. Keep `repeat_penalty` light: above about 1.1 it eats the repetition prose is built
from.

```sh
just retune --top-k 40      # try one value; seconds, and no download
just ab sessions/2026-08-19 # rewrite a week you already have, and count what changed
```

`retune` re-derives the model in place, because the Modelfile is written by `bootstrap.sh` and
`bootstrap.sh` runs once, on a box's first boot — without it, trying a value costs an instance,
which is how the sampler came to be unset in the first place.

**The context length.** 131072, and the number is arithmetic rather than taste. It used to be
65536, and before that this paragraph claimed a week's bundle was "under twenty thousand tokens"
— which was true of a light week and not of a heavy one. The sweep week, 2026-09-09, assembles
to 124,130 characters: **29,495 tokens**, of which the Obsidian diff alone is 21,335.

That is the number the window has to be sized against, and it has to be sized against it twice,
because the letter is written by *resuming* the read's session. Stage 3's prompt is the bundle
plus the read. Two ceilings sit above that, and both are silent:

- **ollama truncates.** Prompt plus generation must fit `num_ctx`. Past it, ollama drops the
  front of the prompt and answers anyway — a normal 200, a normal-looking letter, and a
  `truncating input prompt` line in a journal on a box in Virginia. The bundle leads with the
  ACT prompt and the week context, so the front is the worst thing to lose. Nothing can be
  configured to make this an error instead: `/api/generate` takes `truncate: false`, and the
  OpenAI-compatible endpoint opencode speaks has no field for it.
- **opencode compacts.** Past `limit.context - max(limit.output, 20000)` it replaces the head of
  the session with a summary of itself. The head of this session is the week's diff. A letter
  written after that is a letter written from a precis, and the archive records nothing about it.

At 65536 that second line is 45,536 tokens, and a worst-case letter prompt — a 29.5k bundle plus
a read allowed to run to `limit.output` — is 46,776. It crosses. At 131072 the line is 98,304 and
the worst case is 62,883, which is why the window is what it is. The cost is ~4GB of KV at `q8_0`
instead of ~2GB, on a card with 48GB.

`just sublimate` now does that arithmetic before it spends anything. It runs the pipeline once
with `--dry-run`, which assembles the real bundle and makes no API call, and hands the result to
`tools/context_check.py` against the same `petrograph/opencode.json` opencode will read:

```
context check  qwen3.8:27b-128k
  window         131,072 tokens
  bundle         29,495
  read ceiling   32,768  (limit.output; reasoning counts against it)
  fits           35,421 tokens of headroom at the tightest of the two ceilings
```

After the fact, `just status` greps the box's journal for truncations and says so in red if it
finds any; `evanescent.py logs --truncation` shows the lines.

Changing the context length renames the derived model — `qwen3.8:27b-256k` becomes
`qwen3.8:27b-128k` — so a box that predates this needs `just retune` (which creates the new tag,
reusing the blobs) and then `evanescent up`, which rewrites `petrograph/opencode.json` to
declare it. On a box you are about to create anyway, neither is needed.

**Which boxes it will accept.** Only ones that can serve that window, which now means no single
24GB card at all. `g5.xlarge` (A10G) and `g6.xlarge` (L4) used to sit at the bottom of the
fallback ladder capped at 32768, and the code claimed `min_context` excluded them; it did not —
the floor was 32768, the cap was 32768, and the comparison is `>=`. A region short on L40S
capacity would quietly hand you a box serving a quarter of the window, and every stage of the run
would reshape itself around it without saying so. All twenty-six rows of the sweep in
`.sublimation/experiments/` were read on a 64K box; the box that replaced one of them was 32K.

The ladder is now five single-L40S `g6e` sizes — same GPU, so same speed and same context, they
differ only in vCPU and price — followed by `g5.12xlarge` and `g6.12xlarge`, which are four 24GB
cards each. Those two are there because they are a *different pool of silicon*: an L40S shortage
spans every `g6e` size at once, and 4 × 24GB aggregates to ~89GB, which holds the weights and a
256K cache with room to spare. `OLLAMA_SCHED_SPREAD=1` in the bootstrap makes ollama spread the
layers over all four rather than squeeze onto two. A10G before L4 because layers are walked in
sequence, so throughput tracks one card's bandwidth: ~600GB/s against ~300GB/s.

The p-family is deliberately absent. `p5.4xlarge` (1 × H100 80GB) would serve this happily, and
this account's "Running On-Demand P instances" quota is 0 against 192 for G and VT. Every rung
above fits inside that 192; the largest, `g6e.16xlarge`, takes 64. An instance type that is not
on the ladder is refused rather than tried, because nothing here knows its VRAM and so nothing
here can promise it serves the window.

If no rung has capacity, `up` gives up and nothing bills.

**And the prompts**, which are petrograph's and are covered in its README under "What the
prompts say differently under opencode". The short version: both backends are asked the same
question — `prompts/act-analysis.md`, with `prompts/synthetic-style-guide.md` appended after
it — and what `--llm opencode` adds is two reference sections after the diff, a real read of a
real week and a menu of ACT metaphors. The Claude path's bundle is unchanged to the byte,
which `--dry-run` will show you.

With one exception, which is this repo's and not petrograph's: `just sublimate` asks with
`experiments/prompts/read-proportion.md` and `experiments/prompts/letter-detailed.md` instead —
a read told to contest a premise the week reports as weather, to read what a behaviour is for
rather than what it is about, to challenge him the way somebody fond of him would, and to give
the week's hottest passage a paragraph of its own rather than a mention; and a letter told what
a letter worth narrating does. The read is `experiments/variants.toml`'s `read-proportion` row;
the letter is still `adversarial-and-letter`'s half, so the shipped pair is no longer any one
row in the book. The justfile passes the first as `--prompt` and the second as
`PETROGRAPH_COMPACTION_PROMPT`, both ahead of your own arguments, so either is still overridable
per run. `just ab`, `just experiment` and petrograph called directly are untouched by this and
still ask `act-analysis.md` — which is the point: the thing a sweep measures against has to sit
still.

There were once three more files over there: an addendum per turn, telling a smaller model what
a good read looks like, and a copy of the read prompt with its four hand-fillable openers
removed, on the theory that a 27B would try to fill them in itself. All three are gone, and the
register and prose principles they carried now live in `synthetic-style-guide.md`, which any
bundle appends whatever it was asked with. So `just ab` no longer pins `--prompt`: the prompt
is held still upstream, and what still moves with `--llm` is the pair of reference sections,
which no prompt flag reaches. Pass your own `--prompt` through `just ab` and it wins, and the
same flag asks either backend a different question from petrograph directly:

```sh
just petrograph review-and-narrate sessions/2026-08-14 --prompt prompts/some-other-read.md
```

`just ab` is the thing to reach for before believing any of this. It replays one fixed week
through opencode and prints a scorecard — which person the letter is in, names and dates per
thousand words, how many of its quotations are really in the diff, its length, the longest
phrase it said more than twice, and how long its sentences and paragraphs run. None of those is
quality. All of them are necessary conditions for it, and unlike quality they do not move
depending on which draft you read first.

The cadence numbers are the newest and the cheapest. The best *analysis* of the 2026-09-09
sweep came attached to a letter averaging just under forty words a sentence in paragraphs of
two hundred and sixty-seven, against a known-good's sixteen and eighty — and it scored 3.0 out
of 5 on `speakable` and won its stage anyway. That half is narrated to somebody with no page in
front of them. Counting it costs nothing and happens before any judge is asked.

`just experiment` is the same idea with the configurations written down instead of held in your
head: every row of `experiments/variants.toml` replayed against every week you already rate,
scored by the scorecard and then — with `--judge` — by Claude reading the candidate and the
known-good blind, in both orders, against a per-stage rubric.

**Against more than one week, and not every row.** `--session` takes more than one, and each
week supplies its own known-good by convention — `claude-analysis.md` and `lifelog-<date>.md`,
which is where petrograph already puts them — so a second week costs one flag. The two are then
combined by a rule that does two things a plain mean does not. It weights the read at 0.6 and
the letter at 0.4, because a letter is written in the session its read opened and cannot be
better than what it compacts, so scoring them evenly rewards the row that writes well about a
thin read. And it subtracts half the range between the weeks from the mean, because a prompt
that scores ninety on the week it was written from and fifty on the next one has been shown the
answer key, and a steady seventy is the better prompt. Both numbers are flags —
`--analysis-weight`, `--spread-penalty` — and both are printed under the table.

`--previous BOOK --top N` reads an earlier sweep's book, keeps its N best rows and drops the
rest before the box is touched; a row that is not in that book is new, unmeasured, and always
kept. Twenty-five rows against two weeks is ten hours of a box billed by the minute. Ten rows
is three, and the fifteen dropped had already lost once. `just experiment` does this by default
against the 2026-09-09 book, at five; `--top 0` runs everything.

A row is re-run when what it asks for moves, and a variant's prompt files are fingerprinted by
their **contents** rather than by their paths. That was wrong for a while and wrong invisibly:
editing a block inside a prompt changed the question completely, moved nothing the fingerprint
could see, and the sweep reported the row as already done under the previous run's numbers.
Improving a block and re-running the rows that use it is now the obvious thing and also the
correct one.

Dimensions get added to that rubric when a sweep loses for a reason the existing ones can see
and cannot name, and every one of them was read off a comparison rather than designed in
advance. It started at four per stage and is at eight for the read and nine for the letter.

`challenge` came after the first sweep, in which fifteen variants all lost the read comparison
and in all thirty judge passes the difference the judge reached for was the same one: the
known-good took something the week reported as settled, said plainly that it might not be, and
named a step from outside the frame he was writing in. Nothing in the sweep did that, and four
dimensions could see the result without naming the cause, because the property was hiding inside
`usefulness`
where it competed with ordinary specificity. The five rows under "conviction, and contesting
what the week takes for granted" are the attempt to produce it, and `adversarial-and-letter` is
the row that won — its letter still ships, and its read is what `read-proportion.md` is three
blocks on top of.

`function`, `warmth` and `carry` came after that win, from reading the winner beside the
known-good rather than from its scores, which said 83.5 and 85 and did not say what was left:

- **`function`** — the winner found a real pattern in the material and never asked what the
  material was doing for the subject: it described how a habit works and never said what would
  happen to him if it stopped. The known-good made the opposite move three times, each time
  reading a behaviour by what it is in service of and evidencing it from the order of events the
  week itself recorded. That is not a harder `insight`. It is a different question asked of the
  same paragraph, and a candidate can answer the first perfectly without ever being asked the
  second.
- **`warmth`** — the winning read opens by prosecuting the subject, keeps that up for two
  thousand words and nowhere says anything he did that week was any good, and it scored 5/5 on
  `register` for it. The rubric was rewarding nerve and could not tell a clinician unafraid of
  him from one against him. Being challenged is the point; being prosecuted is not.
- **`carry`**, on the letter only — the letter is the half that gets narrated, so a finding the
  read made and the letter dropped was never delivered. The most valuable item is also the most
  compressible: a named piece of outside expertise shortens to a sentence reporting that a
  recommendation was made, and nothing in the rubric docked a letter for the shortening.

The five rows under "the function of the symptom, and the temperature of the person saying it"
are the attempt to produce those three, each adding one block to the shipped pair so a win
decomposes. `read-complete` and `complete-and-carry` took that sweep on the read.

`proportion` came after it, and unlike the others it did not come from reading the winner — it
came from reading all twenty-five. Every one of them handled the same passage the same way. The
known-good gave a section of its own to the week's hottest stretch, read it as the peak of a bad
hour rather than as a conclusion, and said what it was answering and how long the state lasted.
No candidate did either at either stage, and the top of that table was an 83 — which is a rubric
measuring craft and calling it quality. Silence scores 1 there now, and so does alarm, which is
the other way to get it wrong; a week that genuinely holds nothing of the kind is a sentence
saying so.

The five rows under "the second generation" are the attempt to produce it and two other things
the known-good did that nothing in the sweep did: amending a repair the subject designed himself
rather than praising or replacing it, and counting where the week's feeling actually terminates.
As with the rows before them, none of
the blocks names what either known-good found — a prompt written against the answer key scores
well on the week it was written from and teaches nothing about next week, which is the only week
any of this is for. With two weeks scored and the spread subtracted, that discipline is now
something the table can catch rather than something the file asserts.

Adding a dimension changes every mean the rubric has ever produced, so each verdict carries the
hash of the rubric that scored it. A row graded under an older one is marked `*` in the table
and re-judged the next time `--judge` runs; `just experiment --report` will show you which. That
re-judging costs Claude calls and not box time — a row whose documents are still on disk is
graded again rather than replayed, and says so while it does it.

## A box that only writes

Stage 4 is the only thing on that box that is not ollama, and it is the only thing that wants
ffmpeg, uv, torch and two Chatterbox checkpoints. A box made to be swept rather than to produce
a week never reaches it:

```sh
just box-up --no-narration   # a box for `just experiment` and `just ab`
just box-up                  # the usual one, which narrates
```

`--no-narration` leaves ffmpeg and uv off at bootstrap, so the ~4.5GB of torch and weights that
the first render pulls is never fetched and never stored. The read and the letter are untouched,
because those are ollama and ollama is installed either way. Sixty GB of volume is comfortable
where a narrating box wants a hundred and fifty.

It is decided at first boot and recorded in state, so it is not a `retune`: changing your mind
means `just destroy && just box-up`. `just doctor` and `just status` both say which kind of box
is on the end of the tunnel, and `just sublimate` refuses one it cannot narrate on *before* the
read rather than at stage 4 — which arrives two model calls and forty minutes later, with the
letter already written and nothing to read it.

`just box-up` is also the answer to a smaller question: until now the only things that made a
box were `begin-sublimation` and `sublimate`, and both build a week on the way past. The two
commands that want nothing but a model on the end of a tunnel now have a way to say so.

## Cost, which is the real failure mode

A g6e.xlarge is ~$1.86/hr. `end-sublimation` **stops** the box, which keeps the EBS volume
(~$12/mo) and with it the ollama weights, torch, the Chatterbox checkpoints and the TTS chunk
cache — so coming back is seconds. `just destroy` throws all of that away and bills nothing.

The instance stops itself after 60 idle minutes. That watchdog has been extended here: a
narration in flight now counts as activity. Upstream it did not — a long render generates no
Ollama traffic, holds no connection to 11434, and arrives over a non-interactive `ssh host
cmd` that writes no utmp entry, so all three of its original checks would have called a
forty-minute letter idle and stopped the box in the middle of it.

`just status` shows accrued cost, for this session and lifetime.

## First run is slow, and pays for it in the one place there is time

Torch is a ~2.5GB wheel on the box and the Chatterbox weights are another ~2GB, both fetched
the first time anything asks. `just sublimate` asks first, in the background, and then gets on
with the week: opencode's read is the slowest stage of a run and wants nothing from the GPU,
which makes it exactly the window in which a cold box should be downloading. Stage 4 takes the
same lock as the download, so a render that arrives early waits for it rather than starting a
second one. On a warm box it is one `ssh test -f` and a sentence.

They live on the EBS volume, so `stop` keeps them and only `destroy` loses them.

## When the card is full

Stage 4 arrives on a box that has just spent forty minutes writing, and ollama does not put
its weights away when it stops being asked: `OLLAMA_KEEP_ALIVE` holds the model resident for
half an hour against a request that is not coming. On the g6e.xlarge this was built for that
is nine gigabytes of forty-eight and nobody notices. On the fallback rung of evanescent's
ladder it is the whole run:

```
g5.12xlarge — 4 x A10G, 22.5 GiB each      what the render used to do
  cuda:0   8.3 GiB of ollama               all three workers, here
  cuda:1   8.3 GiB of ollama               idle
  cuda:2   8.3 GiB of ollama               idle
  cuda:3   8.3 GiB of ollama               idle
```

`OLLAMA_SCHED_SPREAD=1` puts a shard of the model on every card, three Chatterbox workers want
about 4.5 GiB each, and `--device cuda` means cuda:0 to every one of them. 12.6 free against
13.8 wanted is `torch.OutOfMemoryError`, forty minutes and two model calls after the point
where anything could have been done about it cheaply.

So the render asks before it assumes. `nvidia-smi` says what is free on each card; the run
goes to the emptiest one rather than to cuda:0, which on this box is most of the fix by
itself. If the emptiest still will not hold the fan-out, ollama is asked to put the model away
— `ollama stop`, which is a keep-alive-zero request that queues behind anything in flight and
expires the runner when it goes idle, so it cannot cut a generation short and costs only a
reload off the box's own volume — and the cards are re-read. The fan-out is then sized to what
is actually free rather than to `REMOTE_CONCURRENCY`, which was only ever a statement about
four vCPUs on a box with memory to spare. An explicit `--concurrency` is still obeyed.

An OOM that gets through all of that is retried a worker lighter, and again, down to one. That
is cheap because of where the chunk cache lives: on the box, keyed by text and generation
parameters, so a retry picks up at the chunk that failed rather than at the top of the letter.
Anything that is *not* an out-of-memory failure is not retried — that is the letter's problem
rather than the box's weather, and a second attempt would only spend your money agreeing.

The prewarm plays by the same rules with one difference: it runs *while* ollama is writing the
read, which is the one moment in a week when the model on the card is in use, so it takes the
emptiest card and asks nothing to move out of its way.

## Whose voice

Chatterbox has no voice catalogue. Any voice but its built-in one is ten to twenty seconds of
somebody speaking, cloned zero-shot — so choosing one means handing over a recording rather
than naming an id:

```bash
just voice ~/recording.m4a          # 20s from the top → voices/narrator.wav
just voice ~/recording.m4a 20 45    # …20s, starting 45s in, past the quiet opening
just doctor                         # which voice a run would actually use
```

The clip goes up with the letter and is used on the box; nothing else about the run changes.
It lands in `voices/`, which is gitignored: it is a recording of a person, and this repo
publishes it no more than it publishes the sessions.

`just voice` is for cutting a clip out of something longer. If you already have one, drop it
into `voices/` and it is found — mp3, m4a, flac, wav, and the rest of `AUDIO_SUFFIXES`. What
`just voice` writes wins over what you dropped in, and with several of yours in there the run
says which it took. Anything that is not already a wav is converted on the way up rather than
on the box: Chatterbox reads the reference through librosa, whose ability to decode an mp3
depends on which libsndfile came with the wheel over there, and that is not a thing to find out
at stage 4 with the letter already written and paid for. ffmpeg is here, and the conversion is
the same mono-at-the-model's-rate the cloner does internally anyway.

Four answers, most specific first — a session's own `voice-sample:` line, then
`SUBLIMATION_VOICE_SAMPLE=<clip>` for a single run, then the clip `just voice` installed, then
petrograph's `share/voices/victoria.wav` if you have one there. None of them, and the box uses
Chatterbox's built-in speaker, which is a working voice and is not yours — so every run says
which of the five it got, because the failure here is otherwise silent.

## Verifying it actually went to your server

Ask the server, not the client:

```bash
just box-logs --ollama -f     # terminal 1 — every request ollama served
just sublimate 2026-08-20     # terminal 2
just box-ssh -- nvidia-smi    # the card, mid-render
```

`curl localhost:11434` proves only that something is listening on that port, and a local
Ollama would answer it identically. See `evanescent/` for the longer version of this argument.
