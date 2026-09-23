# sublimation — the weekly letter, written and read aloud on hardware you are paying for.
#
# One command, and then one more when the letter is in your ears:
#
#   just sublimate              a box if there is not one, this week's context if it is not
#                               there, then the read, the letter and the narration
#   just end-sublimation        stop paying for the box
#
# `just voice <recording>` teaches the narrator whose voice to read the letter in, and
# sublimation.conf names the bed it reads over. Both are written once, not weekly.
#
# Which is the three-command flow with the waiting taken out of your hands. The parts are
# still there, and are what you want on a week you mean to write the context for yourself:
#
#   just begin-sublimation      the week's context here, a GPU box there, in parallel
#   (write your week-context paragraph into the edit-me.md it names, if you want one)
#   just sublimate <session>    the read, the letter, the narration
#
# Everything petrograph does is still available, under `just petrograph <recipe>` — see the
# `mod` line below. Run `just` to list, `just petrograph` to list those.

# petrograph's own recipes, run in petrograph's own directory. `mod` rather than `import`
# deliberately: its recipes call `./tools/*.py`, which only resolves when the working
# directory is the petrograph checkout, and an `import` would run them from here.
#
# Everything petrograph can do: `just petrograph` lists it
mod petrograph 'petrograph/justfile'

root       := justfile_directory()
state      := root / ".sublimation"
# The standing answers, if you have written any: `music` today, and whatever else it grows.
# Gitignored; sublimation.example.conf is the tracked copy `just setup` copies from.
conf       := root / "sublimation.conf"
# Sessions are yours; petrograph is code. As a submodule its sessions/ is a fresh empty
# directory inside a checkout you did not write, so left alone the archive you already have
# would fork in two. This is the root of the one that counts — the scaffold, the attachments,
# the letter and the audio all land under it — and it is passed to both petrograph tools as
# `--out-root` rather than left in the environment, because a just `export` does not reach a
# `mod`'s recipes and an archive that moves depending on how you invoked it is worse than
# either answer. $PETROGRAPH_OUT still works, and still means the same thing.
sessions := env('PETROGRAPH_OUT', root)
evanescent := root / "evanescent" / ".venv" / "bin" / "python" + " " + root / "evanescent" / "evanescent.py"
narrator   := root / "tts" / "remote_chatterbox.py"
# What the weekly run asks with, in place of petrograph's own two prompts. The read contests a
# premise the week reports as weather, ranks itself out loud, names what a field he is not
# consulting would say, reads what a behaviour is *for* rather than what it is about, aims the
# nerve at the material and never at him, and gives the week's hottest passage a paragraph of
# its own rather than a mention; the letter is told what a letter worth narrating does, rather
# than left to infer it. The read is `experiments/variants.toml`'s `read-proportion` row —
# `read-complete.md` and one block, the one every variant in the 2026-09-09 sweep flattened
# while the table's top row read 83, a rubric measuring craft and calling it quality. Left in
# experiments/ rather than copied here, so the row and the shipped run stay the same file.
#
# The letter stays `letter-detailed.md`, which is not what the `read-proportion` row pairs it
# with, so the shipped pair is no longer any single row in the book. The letter half has not
# been re-run since it won; moving it on the strength of a read sweep would be changing it
# because something else was measured.
#
# Both are defaults and not pins: `--prompt` in the arguments to `just sublimate` comes after
# this one and wins, and PETROGRAPH_COMPACTION_PROMPT already set in your environment is left
# alone. `just ab` and `just experiment` are deliberately not routed through here — those two
# measure against petrograph's defaults, and a comparison whose baseline moves is not one.
read_prompt   := root / "experiments" / "prompts" / "read-proportion.md"
letter_prompt := root / "experiments" / "prompts" / "letter-detailed.md"
# One ssh master shared by the tunnel and by every scp the narrator makes. The socket is
# named for a hash of user@host:port, so a rebuilt box gets its own rather than a stale one.
#
# It lives under /tmp and not in .sublimation/ because a unix socket's path is capped at 104
# bytes on macOS and ssh appends a 17-character suffix while it is binding the master. This
# checkout plus ssh's own %C — 40 hex characters — came to 109, and the tunnel died with
# "path too long" before it ever dialled out. A short fixed directory and eight characters
# of hash keeps that arithmetic true no matter how deep the checkout is. Per-uid and 0700
# because /tmp is shared; the box's own state stays in .sublimation/, this is only the socket.
#
# Must match ctl_path() in tts/remote_chatterbox.py — two ends of the same connection.
ctl_dir    := "/tmp/.sublimation-" + shell("id -u")

default:
    @just --list

# Whether the box on the end of the tunnel was built to narrate. `yes` for every box made
# before `--no-narration` existed and every one made without it, because the key is absent from
# their state and absent has to mean the default — a box that narrates is what a box was.
[private]
_narrates:
    #!/usr/bin/env python3
    import json
    from pathlib import Path
    state = Path("{{root}}") / "evanescent" / ".evanescent" / "state.json"
    try:
        recorded = json.loads(state.read_text()).get("narration", True)
    except (OSError, ValueError):
        recorded = True
    print("yes" if recorded else "no")

# `just sublimate 2026-08-20`, `sessions/2026-08-20` and a full path are the same ask, and
# more than one recipe takes a session now — so the resolving lives in one place rather than
# in each of them. Absolute on the way out, because the tools that receive it run in the
# petrograph checkout, where a path relative to this directory means something else.
[private]
_session name:
    #!/usr/bin/env bash
    set -euo pipefail
    s="{{name}}"
    case "$s" in
        /*) ;;
        *)  if   [ -e "{{sessions}}/$s" ];          then s="{{sessions}}/$s"
            elif [ -e "{{sessions}}/sessions/$s" ]; then s="{{sessions}}/sessions/$s"
            fi ;;
    esac
    [ -e "$s" ] || { echo "no session at $s" >&2; \
        echo "  sessions live under {{sessions}}/sessions/" >&2; exit 1; }
    printf '%s\n' "$s"

# One reader, so `begin-sublimation` and `doctor` cannot disagree about what the file says.
# Prints nothing when the file, the key, or the value is absent — every caller treats those
# the same way, as no answer given. `just _conf music` to see what a run would pick up.
[private]
_conf key:
    #!/usr/bin/env python3
    import os, sys
    from pathlib import Path
    conf = Path("{{conf}}")
    if not conf.exists():
        sys.exit(0)
    value = ""
    for line in conf.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        if key.strip() == "{{key}}":
            value = raw.strip()  # the last assignment wins, the way an ini file reads
    if not value:
        sys.exit(0)
    # A `~` is expanded here because petrograph's music_field resolves without expanding one,
    # and a path with a literal ~ in it exists nowhere. Per comma-separated piece, and never a
    # blind split into paths: the separator in a list is resolved against the disk over there,
    # since `{Teichiku Records Co., Ltd. TECD-28262}` is a real album folder, and putting the
    # string back together with the commas it came apart on keeps that true. A piece that
    # follows a comma cannot begin with ~, so only a genuine first path is ever expanded.
    value = ",".join(os.path.expanduser(p) if p.startswith("~") else p
                     for p in value.split(","))
    # A single relative path is relative to the conf file, which is the directory somebody
    # editing it is thinking about. Not applied to a list, where a piece after a comma may be
    # the tail of a name that contains one, and prefixing that would break a path that was fine.
    if "," not in value and not value.startswith("/"):
        value = str(Path("{{root}}") / value)
    print(value)

# ── setup, once ───────────────────────────────────────────────────────────────────────────

# Submodules, the AWS client's venv, and the two petrograph files that are yours rather than
# the repo's. .env and providers.conf are gitignored over there, so a fresh submodule checkout
# has neither — they are symlinked from a petrograph clone you already use, which keeps one
# copy of your keys rather than two.
# Bring a fresh clone to the point where `begin-sublimation` will work
setup from="~/git/petrograph":
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{root}}"
    git submodule update --init --recursive
    [ -d evanescent/.venv ] || python3 -m venv evanescent/.venv
    evanescent/.venv/bin/pip install -q -r evanescent/requirements.txt
    src=$(eval echo "{{from}}")
    # An archive in the clone we are borrowing secrets from is the one you have been building
    # up to now, and it is not the one this repo will write to. Said rather than migrated:
    # moving somebody's audio is not a setup step's decision.
    if [ -d "$src/sessions" ] && [ "$src" != "{{sessions}}" ]; then
        n=$(find "$src/sessions" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')
        if [ "$n" -gt 0 ]; then
            echo "  note: $n existing session(s) in $src/sessions"
            echo "        this repo writes to {{sessions}}/sessions — move them, or set"
            echo "        PETROGRAPH_OUT=$src to keep one archive where it is"
        fi
    fi
    # Yours to edit, and nothing in it is required — but a copy that exists is a copy you
    # will find, and an example you have to go looking for is one you will not.
    if [ -e sublimation.conf ]; then
        echo "  sublimation.conf — already there"
    else
        cp sublimation.example.conf sublimation.conf
        echo "  sublimation.conf — copied from the example (name a backing bed in it, or not)"
    fi
    for f in .env providers.conf; do
        if [ -e "petrograph/$f" ]; then
            echo "  petrograph/$f — already there"
        elif [ -f "$src/$f" ]; then
            ln -s "$src/$f" "petrograph/$f"
            echo "  petrograph/$f → $src/$f"
        else
            echo "  petrograph/$f — MISSING, and not at $src/$f. Copy one in." >&2
        fi
    done
    just doctor

# What is in place and what is not, before a run rather than during one
doctor:
    #!/usr/bin/env bash
    set -uo pipefail
    cd "{{root}}"
    ok=0
    check() { if eval "$2" >/dev/null 2>&1; then echo "  ✓ $1"; else echo "  ✗ $1"; ok=1; fi; }
    check "petrograph submodule checked out"  '[ -f petrograph/justfile ]'
    check "petrograph tools present"          '[ -f petrograph/tools/chatterbox_tts.py ]'
    check "microlite reader (nested submodule)" '[ -d petrograph/vendor/obsidian-microlite/src ]'
    check "petrograph/.env"                   '[ -e petrograph/.env ]'
    check "petrograph/providers.conf"         '[ -e petrograph/providers.conf ]'
    check "evanescent venv"                   '[ -x evanescent/.venv/bin/python ]'
    check "uv (runs petrograph's tools)"      'command -v uv'
    check "opencode CLI"                      'command -v opencode'
    check "ffmpeg"                            'command -v ffmpeg'
    check "narrator is executable"            '[ -x tts/remote_chatterbox.py ]'
    check "stage reader is executable"        '[ -x tools/stage.py ]'
    # petrograph resolves a path engine against the *working directory*, and `sublimate` runs
    # it from petrograph/ — so the engine it is handed has to be absolute. It is, above; this
    # asserts it, because a relative one would fail four stages in.
    check "engine path is absolute"           'case "{{narrator}}" in /*) true;; *) false;; esac'
    echo "  · sessions → {{sessions}}/sessions/"
    # A property of the box, not of this machine, so it is a line rather than a check: the
    # checks above are all things you can go and fix, and this is a decision already made at
    # the box's first boot. Changing it means a new box.
    if [ "$(just _narrates)" = no ]; then
        echo "  · box      → writes only (--no-narration): stages 2 and 3, and no stage 4"
    else
        echo "  · box      → writes and narrates"
    fi
    # Which voice, asked of the narrator rather than answered again here: the precedence —
    # the session's line, the environment, `just voice`, petrograph's clip, the built-in
    # speaker — is resolve_sample()'s, and a second copy of it would be a second answer.
    echo "  · voice    → $("{{narrator}}" --which-voice 2>/dev/null || echo "the narrator did not answer — see the checks above")"
    # The bed, checked here rather than found missing when the week is built — which is the
    # next thing that would have caught it, and only after the box was already running.
    # A list is taken on trust: which commas in it are separators is a question the disk
    # answers, and petrograph asks it properly a moment later.
    bed=$(just _conf music)
    if [ -z "$bed" ]; then
        echo "  · bed      → none — the narration is voice alone (sublimation.conf names one)"
    elif [ -e "$bed" ]; then
        echo "  · bed      → $bed"
    elif case "$bed" in *,*) true ;; *) false ;; esac; then
        echo "  · bed      → $bed  (several — checked when the week is built)"
    else
        echo "  ✗ bed      → $bed  — sublimation.conf names it and it is not there"; ok=1
    fi
    if grep -q "^engine:" petrograph/sessions/*/edit-me.md 2>/dev/null; then
        echo "  ! a session carries its own engine: line, which beats --tts-engine's default"
    fi
    exit $ok

# ── the flow ──────────────────────────────────────────────────────────────────────────────

# The week's context is assembled here while the box boots there, because the box takes six to
# ten minutes and the assembling takes one — running them in series wastes the difference, and
# the box bills for it either way. `up` also writes the `evanescent` provider into
# petrograph/opencode.json, which is where opencode will look: petrograph runs it with the
# petrograph checkout as the working directory.
#
# Both halves are conditional, and that is what lets `just sublimate` open with this rather
# than asking you whether you have run it. A box that already exists is used — a second one is
# not made and a stopped one is woken — and a session that already exists is left exactly as it
# is: the providers would rewrite the attachments under a letter that was written from them,
# and this week's diff is not a thing to regenerate on the way past. Delete the session
# directory to rebuild it; that is a decision, and it should look like one.
#
# Three flags decide the session and are written into its front-matter, so no later command
# has to repeat them: opencode writes the read and the letter (through the tunnel, on the
# box's ollama), the narrator over in tts/ reads it aloud (on the box's GPU), and the week
# context is filled in rather than left for you. Anything you pass comes after them and wins,
# so `just begin-sublimation --llm claude` is still a sentence you can say.
#
# The fourth is the backing bed, and it comes from sublimation.conf rather than from here.
# Most weeks are the same bed — that is what a bed is — so it is a line you write once and
# stop thinking about, and every week after stays one command with nothing to remember. It is
# written into each new session as `music:`, and stage 5 mixes the letter over it into the
# master. No conf, no `music` line, or an empty one, and the narration is voice alone.
# START: this week's context + a GPU box, in parallel — a no-op for whichever is already there
begin-sublimation *args:
    #!/usr/bin/env bash
    set -uo pipefail
    cd "{{root}}"
    mkdir -p "{{state}}"
    log="{{state}}/up.log"
    today=$(date +%F)
    m=$(just _conf music)
    # A --music you typed beats the one you wrote down, rather than playing after it: the flag
    # takes a list and appends, so passing both would lay this week's bed on the end of the
    # standing one, which is nobody's intention.
    case " {{args}} " in
        *" --music "*|*" --music="*)
            [ -n "$m" ] && echo "  (--music given, so sublimation.conf's bed is left out)"
            m="" ;;
    esac
    # Escaped, and it has to be. `just petrograph weekly …` hands its arguments to a `mod`
    # recipe, and just splices a variadic into the recipe body as text — joined by spaces and
    # not re-quoted — so `{Teichiku Records Co., Ltd. TECD-28262}/rain.flac` reaches
    # weekly_context.py as five arguments and it exits on the four it does not recognise.
    # A bed is the one path here that routinely has spaces in it, and petrograph went to some
    # trouble to make a name like that writable in front-matter; escaping it once on this side
    # survives that splice and arrives as the one argument it started as.
    music_arg=()
    [ -n "$m" ] && music_arg=(--music "$(printf '%q' "$m")")
    # petrograph's tools sign off by naming the recipe to type next, and they name their own:
    # `just review-and-narrate <session> --dry-run`. That recipe is real, but in this checkout
    # it is `just petrograph review-and-narrate` — and it is not the sentence you want either,
    # because over here the read, the letter and the narration are one command with the box
    # and the tunnel already in it. The advice is right; only the name is wrong. So the line is
    # rewritten rather than swallowed, and rewritten line by line rather than by one `sed` over
    # the whole stream, so the provider progress above it still arrives while it happens.
    ours() {
        while IFS= read -r line; do
            case "$line" in
                *"just review-and-narrate"*)
                    line=$(printf '%s' "$line" \
                           | sed -E "s#just review-and-narrate [^ ]+#just sublimate $today#") ;;
            esac
            printf '%s\n' "$line"
        done
    }
    # The pid of a job bringing the box up, if this run had to start one. Empty means the box
    # was already running and step 3 has nothing to wait for.
    booting=""
    # Asked once, and captured before it is read: conninfo succeeds only for a box that is
    # running and reachable, and answers a box that is not with a sentence saying which.
    if info=$({{evanescent}} conninfo 2>&1); then
        echo "==> 1/3  the box is up already — left alone"
    else
        # Which box you do not have decides what to do about it, and evanescent has already
        # said. Anything else — pending, shutting-down, running with its key lost — is a state
        # this should not guess at: making a second box is the one mistake here that bills.
        case "$info" in
            *"no evanescent instance"*)
                echo "==> 1/3  starting the box (log: ${log#{{root}}/})"
                {{evanescent}} up --opencode-config "{{root}}/petrograph/opencode.json" \
                    > "$log" 2>&1 &
                booting=$! ;;
            *"instance is stopped"*)
                echo "==> 1/3  the box is stopped — booting it back up (log: ${log#{{root}}/})"
                {{evanescent}} start > "$log" 2>&1 &
                booting=$! ;;
            *)
                echo "  The box is in a state this does not know what to do with:" >&2
                echo "    ${info}" >&2
                echo "  \`just status\` says which; \`just destroy\` starts over." >&2
                exit 1 ;;
        esac
    fi
    echo
    weekly=0
    if [ -d "{{sessions}}/sessions/$today" ]; then
        echo "==> 2/3  sessions/$today is already assembled — left as it is"
        echo "         (delete that directory to build this week's context again)"
        if [ -n "$m" ]; then
            echo "         and the bed in sublimation.conf is not written in — \`music:\` in"
            echo "         that session is its own, and this run is not the one that made it"
        fi
    else
        echo "==> 2/3  this week's context, while that boots"
        # --model rather than leaving it blank: unset, opencode picks from its own config
        # rather than ours, and the session it opens can be against a model nobody chose --
        # which the archive then records as an empty `model:`, because there is nothing to
        # read back. `evanescent model` derives the pair from the same rule `up` used to name
        # it, and answers without an instance, so this works before the box is up.
        just petrograph weekly --out-root "{{sessions}}" \
            --llm opencode --tts-engine "{{narrator}}" --blank-context \
            --model "$({{evanescent}} model)" --effort max \
            ${music_arg[@]+"${music_arg[@]}"} {{args}} | ours
        # The providers' exit code, not the rewriter's: a failed provider is the one thing this
        # stage reports upward, and a pipeline would otherwise answer for its last command.
        weekly=${PIPESTATUS[0]}
    fi
    echo
    if [ -n "$booting" ]; then
        echo "==> 3/3  waiting for the box"
        tail -f "$log" & tailer=$!
        wait $booting; box=$?
        kill $tailer 2>/dev/null; wait $tailer 2>/dev/null
        if [ $box -ne 0 ]; then
            echo; echo "The box did not come up. Nothing is billing if it got no further than" >&2
            echo "capacity — see $log, and try again or a different --region." >&2
            exit $box
        fi
    else
        echo "==> 3/3  nothing to wait for"
    fi
    just tunnel-up
    echo
    if [ $weekly -ne 0 ]; then
        echo "Note: \`just petrograph weekly\` exited $weekly — a provider failed, so an" >&2
        echo "attachment is missing. The session exists; re-run weekly to fill it in." >&2
    fi
    # Nothing was left for you to write — `--blank-context` filled the title and said, in the
    # session itself, that no week context was. The paragraph is still worth writing on a week
    # you have one, and the read is better for it, so the file is named rather than hidden.
    echo "Ready — nothing to edit:"
    echo "    just sublimate $today"
    echo "  (or write this week's paragraph into {{sessions}}/sessions/$today/edit-me.md first)"
    # Said here because this is the recipe that leaves you at a prompt, and because the pair is
    # not petrograph's default — a run asking a different question than the docs describe should
    # say so before it costs forty minutes, not in the archive afterwards.
    echo "  and asks with experiments/prompts/{read-proportion,letter-detailed}.md"

# The read and the letter go to your own Ollama through the tunnel; the narration goes to your
# own GPU through the same ssh connection. Both `--llm` and `--tts-engine` are named here
# rather than left to petrograph's defaults, which are claude and ElevenLabs — this recipe's
# whole claim is that neither is involved, and a default is not a claim.
#
# Named with no session, this is the whole flow: `begin-sublimation` first — which makes a box
# only if there is not one and assembles the week only if it is not already assembled — and
# then this, on the session that leaves behind. Which is why that recipe had to become safe to
# re-run before this argument could become optional. Arguments here are still this recipe's:
# `just sublimate --dry-run` prices the letter, it does not dry-run the box.
# RUN: ACT read → compaction letter → narration, all on your own hardware
sublimate session="" *args:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{root}}"
    s="{{session}}"
    # `just sublimate --dry-run` means the whole flow with a flag on it, not a session called
    # --dry-run: an argument that arrived early, because the session ahead of it was left out.
    extra=""
    case "$s" in -*) extra="$s"; s="" ;; esac
    if [ -z "$s" ]; then
        just begin-sublimation
        echo
        s="{{sessions}}/sessions/$(date +%F)"
    else
        s="$(just _session "$s")"
    fi
    [ -e "$s" ] || { echo "no session at $s" >&2; exit 1; }
    # A box made with `--no-narration` has no ffmpeg and no uv, so there is nothing to warm and
    # stage 4 has no engine. A function because two paths through this recipe need it — the
    # whole run, and a re-run that is only stage 4 — and both need to say it before the wait
    # rather than after it. `$s` and not `$*`: just splices a variadic into a recipe as text
    # rather than as positional parameters, so `$*` here is empty and the suggestion would come
    # back missing the very session it is about.
    require_narration() {
        [ "$(just _narrates)" = no ] || return 0
        echo "This box was made with --no-narration: no ffmpeg, no uv, no stage 4." >&2
        echo "  just sublimate $s --skip-narration  # the read and the letter only" >&2
        echo "  just destroy && just box-up         # a box that can narrate" >&2
        exit 1
    }
    # Where this week already is. Three of the four stages are expensive and every one of them
    # is on disk the moment it finishes, so a re-run asks what is there before paying for any
    # of it twice: a run that died at the narration re-enters at the narration, and one that
    # died after the read re-enters at the letter — which is petrograph's own --resume, reusing
    # the diff and the read and reopening the model session that wrote them. The reading is
    # tools/stage.py's, which builds the names out of petrograph's own, and `cmd` comes back as
    # a bash array so a bed called `{Label Co., Ltd.}/1.mp3` survives the trip.
    #
    # Not asked under --dry-run: that is a question about what *would* be sent, and it wants
    # the bundle assembled however finished the session already is.
    case " $extra {{args}} " in
        *" --dry-run "*) ;;
        *)  # Assigned first and eval'd second, so a reader that fails takes the run down
            # here with its own message rather than three lines later as an unbound variable.
            reading="$(./tools/stage.py "$s" --shell)" || exit 1
            eval "$reading"
            case "$stage" in
                done)
                    echo "Nothing to do — $(basename "$s") has all four stages: $have."
                    echo "    just narrate $(basename "$s")   # the letter read aloud again"
                    echo "    rm -r $s"
                    echo "  (that last one is the week from the beginning — the providers would"
                    echo "   rewrite the attachments the letter was written from)"
                    exit 0 ;;
                narrate|mix)
                    case " $extra {{args}} " in
                        *" --skip-narration "*)
                            echo "Nothing to do — $(basename "$s") has $have, and"
                            echo "--skip-narration says to stop there."
                            exit 0 ;;
                    esac
                    require_narration
                    echo "==> stage 4 only — $have already in $(basename "$s")"
                    echo "    no model call, nothing billed but the box"
                    echo
                    exec "${cmd[@]}" ;;
                letter)
                    echo "==> stages 3 and 4 — $have already in $(basename "$s")"
                    echo "    the diff and the read are reused, in the session that wrote them"
                    echo
                    case " $extra {{args}} " in
                        *" --resume "*) ;;
                        *) extra="$extra --resume" ;;
                    esac ;;
            esac ;;
    esac
    # The letter's prompt travels as an environment variable rather than as a flag, because
    # petrograph gives only the read a `--prompt`. `:-` and not a plain assignment: one already
    # set in your shell is a deliberate answer to the same question, and this is the default.
    # Resolved up here rather than at the call, so the check below measures the prompt the run
    # will send rather than the one this recipe would have picked.
    export PETROGRAPH_COMPACTION_PROMPT="${PETROGRAPH_COMPACTION_PROMPT:-{{letter_prompt}}}"
    mkdir -p "{{state}}"
    just tunnel-up
    # Does the week fit? Asked here because both ways it does not fit are silent: ollama drops
    # the front of an over-length prompt and answers anyway, and opencode replaces the diff
    # with a summary of itself once the letter's prompt passes its compaction line. Either one
    # produces a letter that reads fine and was written from part of the week, and the archive
    # records nothing about it. Forty minutes and two model calls later is the wrong time to
    # find that out, and there is nothing left to find it out from.
    #
    # `--dry-run` assembles the real bundle — the same prompts, attachments and diff the run
    # is about to send — and makes no API call, so this costs the seconds it takes to write
    # one file. Skipped when the run is itself a dry run, which has already printed the size.
    case " $extra {{args}} " in
        *" --dry-run "*) ;;
        *)  dlog="{{state}}/preflight.log"
            if ! just petrograph review-and-narrate "$s" --out-root "{{sessions}}" \
                    --llm opencode --prompt "{{read_prompt}}" --dry-run > "$dlog" 2>&1; then
                echo "could not assemble the bundle to check it (see ${dlog#{{root}}/})" >&2
                tail -n 5 "$dlog" >&2
                exit 1
            fi
            ./tools/context_check.py "$s/bundle.md" \
                --opencode-config "{{root}}/petrograph/opencode.json" \
                --letter-prompt "$PETROGRAPH_COMPACTION_PROMPT"
            echo ;;
    esac
    # Torch and the Chatterbox weights — ~5 minutes on a box that has never held them — fetched
    # while opencode reads the week, which is the slowest stage of the run and the one that
    # asks nothing of the GPU. It used to happen in `begin-sublimation`, where it was five
    # minutes of watching a progress bar before anything could start. Backgrounded rather than
    # raced: the narrator takes the same lock for stage 4 as it does for this, so a render that
    # arrives early waits for the download instead of starting a second one. Its output goes to
    # a log so the read's own stages stay legible.
    warm=""
    plog="{{state}}/prewarm.log"
    # Said here, before the read, rather than left to stage 4 — which arrives forty minutes
    # and two model calls later, with the letter already written.
    case " $extra {{args}} " in
        *" --dry-run "*|*" --skip-narration "*) ;;   # no narration to have warmed up for
        *)  require_narration
            echo "  narrator: warming the box while the model reads (log: ${plog#{{root}}/})"
            "{{narrator}}" --prewarm > "$plog" 2>&1 &
            warm=$! ;;
    esac
    rc=0
    # Ahead of `$extra {{args}}`, so a `--prompt` you pass is the one argparse keeps.
    just petrograph review-and-narrate "$s" --out-root "{{sessions}}" \
        --llm opencode --tts-engine "{{narrator}}" --prompt "{{read_prompt}}" \
        $extra {{args}} || rc=$?
    # By here the prewarm has long since finished — stage 4 could not have run until it had.
    # This is for the run that never reached stage 4, so a box that went away is reported as
    # that rather than as whatever the letter failed with.
    if [ -n "$warm" ] && ! wait "$warm"; then
        echo "  note: the narrator's prewarm exited nonzero — see $plog" >&2
    fi
    exit $rc

# Stage 4 on its own: the letter that is already written, read aloud again. No model call, no
# tunnel — the read and the letter go to ollama through the forward, and the narration goes to
# the box's GPU over the narrator's own ssh, which is why this needs neither.
#
# `just sublimate` reaches the same command by itself on a session whose letter is written and
# whose audio is not, so this is for the times the answer is not "carry on": a voice you have
# since changed with `just voice`, a bed you have since named, a render you want re-rolled with
# `--exaggeration 0.7`. Whatever you pass goes to the narrator — `--concurrency`, the gap
# settings, `--seed`. `--mix-only` is this recipe's own, and means the voice is fine and it is
# the bed under it that is not: seconds of ffmpeg here, and the box is not woken at all.
#
# It overwrites the mp3 where a fresh run would have asked. That is what asking for it means.
# NARRATE: read an already-written letter aloud again (stage 4 alone, no model call)
narrate session="" *args:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{root}}"
    s="{{session}}"
    # As in `sublimate`: a flag where the session should be is a session that was left out.
    extra=""
    case "$s" in -*) extra="$s"; s="" ;; esac
    [ -n "$s" ] || s="$(date +%F)"
    s="$(just _session "$s")"
    # --mix-only is answered here rather than passed on: it names a stage, and everything else
    # in this list is a flag for whichever tool that stage runs.
    pick=narrate
    rest=()
    for a in $extra {{args}}; do
        if [ "$a" = "--mix-only" ]; then pick=mix; else rest+=("$a"); fi
    done
    reading="$(./tools/stage.py "$s" --shell --pick "$pick")" || exit 1
    eval "$reading"
    [ "${#cmd[@]}" -gt 0 ] || { echo "nothing to run for stage $pick in $s" >&2; exit 1; }
    if [ "$pick" = narrate ] && [ "$(just _narrates)" = no ]; then
        echo "This box was made with --no-narration: no ffmpeg, no uv, no stage 4." >&2
        echo "  just destroy && just box-up   # a box that can narrate" >&2
        exit 1
    fi
    echo "==> $(basename "$s") — $(basename "$letter") → $(basename "$voice") ($engine)"
    exec "${cmd[@]}" ${rest[@]+"${rest[@]}"}

# What a session has and what a re-run would do with it — the reading `just sublimate` makes
# for itself before it starts, printed for you instead.
# STAGES: which of the four this session already has, and the command that carries on
stages session="":
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{root}}"
    s="{{session}}"
    [ -n "$s" ] || s="$(date +%F)"
    # Assigned rather than nested: a command substitution inside an argument does not fail the
    # recipe, and `stage.py ""` would go and read the working directory instead of stopping.
    s="$(just _session "$s")"
    ./tools/stage.py "$s"

# STOP: close the tunnel and stop the box (the EBS volume, and the caches on it, survive)
end-sublimation:
    #!/usr/bin/env bash
    set -uo pipefail
    cd "{{root}}"
    just tunnel-down
    {{evanescent}} stop

# ── the voice ─────────────────────────────────────────────────────────────────────────────

# Chatterbox has no voice catalogue. Any voice but its built-in one is ten to twenty seconds of
# somebody speaking, which it clones zero-shot, so choosing a voice here means handing over a
# recording rather than naming an id. This takes a window out of one you already have — a voice
# memo, an old narration — and installs it as the clip every letter from here on is read in.
# It goes up with the letter and is used on the box; nothing else about the run changes.
#
# ffmpeg does the cutting inside the narrator rather than through petrograph's own
# `just petrograph voice-sample`, which is the same four arguments wrapped in a script whose
# dependencies are chatterbox and torch — reaching it to trim an mp3 would install 2.5GB of
# wheels on this Mac, which is the one thing the box exists to avoid.
#
# The clip lands in voices/, which is gitignored: it is a recording of a person, and this repo
# does not publish it any more than it publishes the sessions. Running this is not the only way
# to fill that directory — an mp3 or an m4a dropped in there by hand is found too, and
# converted on the way up so the box never has to decode it. This recipe is for the case the
# clip has to be cut out of something longer first. `just doctor` says which voice a run would
# use; a session's own `voice-sample:` line beats whatever is in voices/, and
# SUBLIMATION_VOICE_SAMPLE=<clip> beats both for a single run.
#
# 2nd arg = seconds to take; 3rd = where to start, to skip a quiet or noisy opening.
# Teach the narrator whose voice to read the letter in → voices/narrator.wav
voice recording seconds="20" start="0":
    @"{{narrator}}" --capture-reference "{{recording}}" \
        --reference-seconds {{seconds}} --reference-start {{start}}

# ── the tunnel ────────────────────────────────────────────────────────────────────────────

# opencode talks to localhost:11434 and the box's ollama listens only on its own loopback, so
# the forward is the only path between them. Backgrounded through a ControlMaster rather than
# held in a terminal: `sublimate` is one command, and the narrator's scp rides the same
# connection instead of opening its own.
# Open the localhost:11434 forward, if it is not already open
tunnel-up:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{root}}"
    mkdir -p "{{state}}"
    # Captured before it is eval'd: a box that is absent or stopped answers with a sentence,
    # and eval'ing a sentence runs its first word as a command.
    info=$({{evanescent}} conninfo 2>&1) || {
        # Which box you do not have decides what to do about it, and evanescent has already
        # said: a box that was never made wants `begin-sublimation`, and one the idle timer
        # stopped an hour after you last touched it wants `resume`. Its own sentence is
        # printed underneath either way, so a wording change there costs a vaguer hint and
        # nothing more.
        case "$info" in
            *"no evanescent instance"*)
                echo "  tunnel: no box yet — \`just begin-sublimation\` makes one" >&2 ;;
            *"instance is stopped"*)
                echo "  tunnel: the box is stopped — \`just resume\` starts it and reopens this" >&2 ;;
            *)
                echo "  tunnel: no reachable box — \`just status\` says what state it is in" >&2 ;;
        esac
        echo "    ${info}" >&2
        exit 1
    }
    eval "$info"
    mkdir -p "{{ctl_dir}}" && chmod 700 "{{ctl_dir}}"
    ctl="{{ctl_dir}}/$(printf '%s' "$user@$host:$port" | shasum -a 256 | cut -c1-8)"
    # A master that dies without cleaning up — a reboot, a kill -9 — leaves its socket behind,
    # and ssh will not bind over one: it says "ControlSocket already exists, disabling
    # multiplexing" and carries on, so the tunnel still opens and every later scp quietly pays
    # for its own handshake. `-O check` answers from the socket alone and ignores the host you
    # name, which makes it a liveness test for every socket in here — including ones stranded
    # by a box whose IP changed across a stop, whose names no longer hash to anything we ask
    # for. A live master, this one or another checkout's, answers and is left alone.
    for sock in "{{ctl_dir}}"/*; do
        [ -S "$sock" ] || continue
        ssh -O check -o "ControlPath=$sock" nobody >/dev/null 2>&1 || rm -f "$sock"
    done
    opts=(-i "$key" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new
          -o "UserKnownHostsFile=$known_hosts" -o ConnectTimeout=10
          -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -o LogLevel=ERROR
          -o ControlMaster=auto -o "ControlPath=$ctl" -o ControlPersist=10m)
    if ssh "${opts[@]}" -O check "$user@$host" 2>/dev/null; then
        # A master is not the same fact as a forward. Run the narrator on its own — no
        # `sublimate`, no `tunnel-up` — and ControlMaster=auto makes it the master, with no -L
        # anywhere in it; answering "already up" then would send opencode at a port nothing is
        # listening on. Ask the master we found for the forward instead, which is cheap when
        # it is the tunnel's own and the repair when it is not.
        if ! (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
            ssh "${opts[@]}" -O forward -L "$port:127.0.0.1:$port" "$user@$host"
        fi
        echo "  tunnel: already up (localhost:$port → $host)"
        exit 0
    fi
    ssh "${opts[@]}" -fN -L "$port:127.0.0.1:$port" "$user@$host"
    echo "  tunnel: localhost:$port → $host"

# Close it. The box keeps running — that is `end-sublimation`.
tunnel-down:
    #!/usr/bin/env bash
    set -uo pipefail
    cd "{{root}}"
    info=$({{evanescent}} conninfo 2>/dev/null) || { echo "  tunnel: no box"; exit 0; }
    eval "$info"
    ctl="{{ctl_dir}}/$(printf '%s' "$user@$host:$port" | shasum -a 256 | cut -c1-8)"
    ssh -o "ControlPath=$ctl" -O exit "$user@$host" 2>/dev/null \
        && echo "  tunnel: closed" || echo "  tunnel: was not open"

# ── the box ───────────────────────────────────────────────────────────────────────────────

# State, IP, uptime, and what it has cost so far
status:
    @{{evanescent}} status

# Shell on the box. `just box-ssh -- nvidia-smi` runs one command.
box-ssh *args:
    @{{evanescent}} ssh {{args}}

# Bootstrap log. `just box-logs --ollama -f` proves what actually reached your server, and
# `just box-logs --truncation` shows any prompt ollama had to cut down to fit the window.
box-logs *args:
    @{{evanescent}} logs {{args}}

# Make a box and nothing else — `just box-up --no-narration` for one that only writes
box-up *args:
    #!/usr/bin/env bash
    set -euo pipefail
    # `begin-sublimation` makes a box *and* builds the week, and passes its flags to the week
    # half. That is right for a Sunday and wrong for the two things that only want a model on
    # the end of a tunnel: `just experiment`, and `just ab`. Neither writes a letter anybody
    # narrates, so neither needs the ~4.5GB of torch and Chatterbox weights that the first
    # render pulls — and `--no-narration` means they are never fetched and never paid for.
    #
    # A box that already exists is resumed rather than replaced, the same promise
    # `begin-sublimation` makes, because making a second box is the one mistake here that bills.
    if info=$({{evanescent}} conninfo 2>&1); then
        echo "the box is up already — left alone"
        just tunnel-up
    else
        case "$info" in
            *"instance is stopped"*)
                echo "==> booting the stopped box back up"
                {{evanescent}} start
                just tunnel-up ;;
            *"no evanescent instance"*)
                echo "==> making a box"
                {{evanescent}} up --opencode-config "{{root}}/petrograph/opencode.json" {{args}}
                just tunnel-up ;;
            *)  echo "$info" >&2; exit 1 ;;
        esac
    fi

# Sweep configurations against a week you rate — resumable; `just experiment --report` reads the book
experiment *args:
    #!/usr/bin/env bash
    set -euo pipefail
    # `ab` answers "did that change help", one change at a time, held in your head. This answers
    # "which of these twenty helps", with the twenty written down in experiments/variants.toml
    # and each run scored against the week you already rate -- mechanically by the scorecard,
    # and, with --judge, by Claude reading the candidate and the known-good blind in both orders.
    #
    # The weeks are defaulted rather than required, because the ones with a letter worth
    # aiming at are a property of the archive and naming them every time is flags you would
    # always type the same way. Each supplies its own known-good by convention, so a third week
    # costs one line here. Pass --session yourself to point this somewhere else; the defaults
    # are dropped the moment you do.
    weeks=(2026-09-09 2026-09-16)
    flags=()
    case " {{args}} " in
        *" --session "*) ;;
        *)  for w in "${weeks[@]}"; do
                d="{{sessions}}/sessions/$w"
                [ -d "$d" ] || { echo "no session at $d — name one with --session" >&2; exit 1; }
                flags+=(--session "$d")
            done ;;
    esac
    # Scored against two weeks rather than one, because a prompt tuned against a single week is
    # a prompt that has been shown the answer key, and the only week any of this is for is next
    # week. `experiment.py` subtracts the spread between them from the mean for that reason.
    #
    # And carried over rather than re-run whole: the five best rows of the previous book come
    # with, the rest are dropped before the box is touched. Twenty-five rows against two weeks
    # is ten hours billed by the minute and twenty of those rows already lost once. A row that
    # is not in that book at all is new and is always kept. --top 0 runs everything.
    prev="{{root}}/.sublimation/experiments/2026-09-09"
    case " {{args}} " in
        *" --previous "*|*" --top "*) ;;
        *) [ -f "$prev/results.jsonl" ] && flags+=(--previous "$prev" --top 5) || true ;;
    esac
    # Fifteen variants is a couple of hours of a box billed by the minute, so this is built to
    # be left: every finished variant is in the book before the next one starts, and one
    # already there is skipped. Run what you have time for, `just end-sublimation`, come back.
    #     just experiment --judge --only baseline,topk-20
    #     just experiment --judge            # later: the rest, and nothing twice
    #     just experiment --report           # the table, without the box
    case " {{args}} " in
        *" --report "*|*" --dry-run "*) ;;
        *) just tunnel-up ;;
    esac
    # ${flags[@]+...} rather than "${flags[@]}": under `set -u` an empty array is an unbound
    # variable on the bash that ships with macOS, and flags is empty whenever you pass your own
    # --session.
    "{{root}}/tools/experiment.py" "{{root}}/experiments/variants.toml" \
        ${flags[@]+"${flags[@]}"} {{args}}

# Rewrite one week you already have, through opencode, and count what changed — `just ab sessions/2026-08-19`
ab session="" *args:
    #!/usr/bin/env bash
    set -euo pipefail
    # Every change here -- a sampler value, a line in an addendum, a different model -- has to
    # be judged against something, and judging it against *this* week means judging it against
    # a different week each time. So: one fixed session, replayed. A session that already holds
    # a letter you rate is the useful one to point this at, because then the scorecard has a
    # column to sit beside.
    s="{{session}}"
    if [ -z "$s" ]; then
        s=$(ls -d "{{sessions}}"/sessions/*/ 2>/dev/null | sort | tail -1 || true)
        [ -n "$s" ] || { echo "no sessions under {{sessions}}/sessions/ — name one" >&2; exit 1; }
    fi
    s="${s%/}"
    [ -d "$s" ] || { echo "no session at $s" >&2; exit 1; }
    for f in edit-me.md microlite.md; do
        [ -f "$s/$f" ] || { echo "$s has no $f — this replays a session that already ran" >&2; exit 1; }
    done
    stamp=$(date +%H%M%S)
    run="{{state}}/ab/$stamp/sessions/$(basename "$s")"
    mkdir -p "$run"
    cp "$s/edit-me.md" "$s/microlite.md" "$run/"
    # The copy points at its own diff, not at the original's: the run must not reach back into
    # the session it is replaying, or a bad one would overwrite the thing being compared to.
    /usr/bin/sed -i '' "s|^diff: .*|diff: $run/microlite.md|" "$run/edit-me.md"
    # Pinned rather than left blank, and written in when the session carries no `model:` line
    # at all: an A/B whose two runs went to whichever model opencode happened to pick is not
    # one. awk rather than sed because appending a line after a match is the one edit sed
    # cannot spell portably.
    m=$({{evanescent}} model)
    awk -v m="$m" '
        /^model:/ { print "model: " m; seen = 1; next }
        /^llm:/   { print; if (!seen) { print "model: " m; seen = 1 } next }
                  { print }
    ' "$run/edit-me.md" > "$run/edit-me.tmp" && mv "$run/edit-me.tmp" "$run/edit-me.md"
    echo "==> replaying $(basename "$s") through opencode → ${run#{{root}}/}"
    # A dry run assembles the bundle and reaches no CLI, so it wants no box and no tunnel --
    # which is what makes it the way to check this recipe's own plumbing for free.
    case " {{args}} " in *" --dry-run "*) ;; *) just tunnel-up ;; esac
    # No --prompt here, and that is the point rather than an omission: petrograph asks both
    # backends `prompts/act-analysis.md` now, so the prompt is held still upstream and pinning
    # it here would only be a second copy of that promise. What still moves with --llm is the
    # pair of reference sections, which is a property of the run being an opencode run and not
    # something a prompt flag reaches. Pass --prompt in {{args}} to ask a different question.
    just petrograph review-and-narrate "$run" --out-root "{{state}}/ab/$stamp" \
        --llm opencode --skip-narration {{args}}
    echo
    # An array rather than a string: the second column is a path, and a path that needed
    # quoting would otherwise arrive as two arguments.
    against=()
    [ -f "$s/claude-output.md" ] && against=(--against "$s/claude-output.md")
    for stage in claude-analysis claude-output; do
        [ -f "$run/$stage.md" ] || continue
        echo "── $stage ──────────────────────────────────────────────"
        if [ "$stage" = claude-output ]; then
            "{{root}}/tools/scorecard.py" "$run/$stage.md" --diff "$run/microlite.md" \
                ${against[@]+"${against[@]}"}
        else
            "{{root}}/tools/scorecard.py" "$run/$stage.md" --diff "$run/microlite.md"
        fi
        echo
    done

# Rebuild the model with the sampler evanescent.toml now says — `just retune --top-k 40` sweeps one value
retune *args:
    # Seconds, and no download: `ollama create` over a model already pulled reuses its blobs.
    # The sampler is the cheapest lever on how the letter reads and the one that was never
    # set — so this exists to make trying a value cost a command rather than an instance.
    @{{evanescent}} retune {{args}}

# Boot a stopped box back up (and reopen the tunnel)
resume:
    @{{evanescent}} start
    @just tunnel-up

# Terminate the box and delete everything — including the TTS chunk cache on its volume
destroy *args:
    @just tunnel-down
    @{{evanescent}} destroy {{args}}
