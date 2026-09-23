#!/usr/bin/env python3
"""
evanescent.py -- an ephemeral, private GPU box for opencode.

Launches one GPU EC2 instance running Ollama and serves it to your laptop over an
SSH tunnel. Nothing is exposed to the internet except port 22, and only to your
current IP.

The two things this is really about:

  * Cost. A g6e.xlarge is ~$1.86/hr. The dominant failure mode is not a bug, it is
    forgetting the box is up. Hence `stop` vs `destroy`, an accrued-cost readout in
    `status`, and an idle watchdog on the instance itself.

  * Statefulness. The weights are an 18GB download. `stop` halts the instance but
    keeps the EBS volume, so coming back is seconds rather than another 18GB.

Commands: up, status, conninfo, tunnel, ssh, logs, stop, start, destroy.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import shlex
import subprocess
import sys
import time
import tomllib
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

try:
    import boto3
    import botocore.config
    import botocore.exceptions
except ImportError:
    sys.exit(
        "boto3 is not installed.\n"
        "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt\n"
        "  .venv/bin/python evanescent.py ..."
    )

# --------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / ".evanescent"
STATE_FILE = STATE_DIR / "state.json"
KNOWN_HOSTS = STATE_DIR / "known_hosts"
CONFIG_FILE = HERE / "evanescent.toml"
BOOTSTRAP_TMPL = HERE / "bootstrap.sh.tmpl"

NAME = "evanescent-opencode"
TAG_MANAGED = "evanescent:managed"
SSH_USER = "ubuntu"
OLLAMA_PORT = 11434
# What this box calls itself in opencode's config, and so the first half of every `provider/model`
# pair pointing at it. Named once because both the config writer and `model` say it.
PROVIDER = "evanescent"

# NVIDIA drivers preinstalled; resolved at launch so it tracks the current release.
AMI_SSM_PARAM = (
    "/aws/service/deeplearning/ami/x86_64"
    "/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id"
)

DEFAULTS = {
    "region": None,  # None -> boto3 session default
    "instance_type": "g6e.xlarge",
    "model": "qwen3.8:27b",
    # 262144 is qwen3.8's native maximum -- the GGUF declares
    # qwen35.context_length = 262144, and there is no 1M variant of this model.
    # We ask for half of it, and the size is arithmetic rather than taste. Measured on the
    # sweep week (2026-09-09), a bundle is ~30k tokens -- the diff alone is 21k -- and the
    # letter stage resumes that same session, so its prompt is the bundle plus the read.
    # Two independent ceilings sit above that, and both are set from this number:
    #
    #   opencode auto-compacts when the prompt passes `context - max(limit.output, 20000)`
    #   (SessionCompaction.compactIfNeeded; the 20000 is its default `compaction.buffer`),
    #   and compaction replaces the head of the session -- the diff -- with a summary. At
    #   65536 that line is 45,536, and a worst-case letter prompt (30k bundle + a read
    #   allowed to run to limit.output) crosses it. At 131072 it is 98,304, which nothing a
    #   week can produce comes near.
    #
    #   ollama truncates, silently, when prompt + generation exceed num_ctx. Same arithmetic,
    #   higher line: a worst-case stage 3 is a ~63k prompt and a letter allowed to run to
    #   limit.output on top of it, so ~96k against 131072.
    #
    # The cost is ~4GB of KV at q8_0 instead of ~2GB, on a card with 48GB. That is affordable
    # here in a way a quieter failure is not. `tools/context_check.py` does this arithmetic
    # against the real bundle before each run, so the numbers above are checked rather than
    # believed.
    "context_length": 131072,
    # Never land on a box that cannot serve at least this much. This is a floor, not a
    # recommendation: a heavy week is already marginal at 65536 -- it crosses opencode's
    # compaction line by about a thousand tokens -- which is exactly why the default window
    # above is twice it. What the floor is really for is keeping a capacity fallback from
    # quietly handing back a 32K box, so every 24GB card sits below it by construction.
    # See FALLBACK_LADDER.
    "min_context": 65536,
    "volume_size": 150,
    # Whether the box is built to narrate as well as write. True installs ffmpeg and uv at
    # bootstrap, which is what petrograph's chatterbox_tts.py needs pushed up to it per run;
    # torch and the Chatterbox weights (~4.5GB) then land in ~/.cache on the first render.
    # False leaves all of it off, and the box serves the read and the letter and nothing else
    # -- which is the whole of what `just experiment` and `just ab` ask of it.
    "narration": True,
    "idle_minutes": 60,
    "kv_cache_type": "q8_0",
    "keep_alive": "30m",
    # The sampler. Left unset these are whatever the model ships, which for qwen3.8:27b is
    # top_k 20, min_p 0 and repeat_penalty 1 -- its temperature and top_p are already 1.0 and
    # 0.95. Twenty candidates a token is most of why a letter comes back flat, and why it finds
    # one rhetorical figure and repeats it down the page. Dropping top_k lets the model reach the tail where the distribution is
    # genuinely flat -- word choice, image, figure -- while min_p still clamps it hard where the
    # distribution is peaked, which is names, dates and syntax. That pairing is what buys variety
    # without invented specifics. repeat_penalty stays light on purpose: above about 1.1 it starts
    # eating the legitimate repetitions that prose is built from.
    #
    # temperature and top_p are NOT here. They travel with the request instead, set on the agent
    # in petrograph's weekly_review.py, because those two are the ones worth sweeping and a
    # Modelfile change costs an `ollama create` (see `retune`). The three below cannot go that
    # way: Ollama's OpenAI-compatible endpoint has no field for them and drops them silently.
    "top_k": 0,
    "min_p": 0.05,
    "repeat_penalty": 1.05,
    # Where `up` writes the `evanescent` provider block. None means beside this script, which
    # is right when opencode is run from here and wrong when it is run from somewhere else --
    # opencode reads its config from the working directory, and petrograph runs it from the
    # petrograph checkout. Set once in evanescent.toml and every later `up` keeps it there.
    "opencode_config": None,
}

# Fallback ladder, best first: (instance type, GPU, vCPUs, max_context).
#
# max_context is what fits alongside the weights with a q8_0 KV cache. qwen3.8 is a hybrid
# model -- only every 4th of its 64 blocks is full attention (16 layers x 4 KV heads x 512)
# -- so its cache costs ~32KB/token at q8_0: the full 256K window is ~8GB, and the default
# 128K is ~4GB. Against ~18GB of Q4 weights plus a compute buffer, that wants ~24GB of VRAM
# to hold and rather more to hold comfortably.
#
# Which is why no single 24GB card is on this ladder any more. g5.xlarge (A10G) and
# g6.xlarge (L4) were, capped at 32768, and the comment here used to claim min_context
# excluded them -- it did not: the floor was 32768 and the cap was 32768, and `>=` let them
# through. A fallback to one of those is how this box ends up serving a window that silently
# reshapes every stage of the run, which is the failure this file exists to avoid.
#
# The rungs, and why in this order:
#
#   the five single-L40S g6e sizes first. Same GPU, so same speed and same context; they
#   differ only in vCPU, RAM and price. Falling down them costs money and nothing else.
#
#   then two 4-GPU rungs, which are here because they are a different pool of silicon: an
#   L40S shortage spans every g6e size at once, and these are what is left when it does.
#   4 x 24GB aggregates to ~89GB, which holds the weights and a 256K cache with room to
#   spare (ollama splits layers across the cards -- see OLLAMA_SCHED_SPREAD in the
#   bootstrap). A10G before L4 because the layers are walked in sequence, so throughput
#   tracks one card's memory bandwidth: ~600GB/s against ~300GB/s.
#
# Deliberately absent: the p-family. p5.4xlarge (1 x H100 80GB) and p4d.24xlarge (8 x A100)
# would both serve this happily, and this account cannot launch either -- its "Running
# On-Demand P instances" quota is 0, against 192 for G and VT. Every rung below fits inside
# that 192; the largest, g6e.16xlarge, takes 64.
FALLBACK_LADDER = [
    ("g6e.xlarge", "L40S 48GB", 4, 262144),
    ("g6e.2xlarge", "L40S 48GB", 8, 262144),
    ("g6e.4xlarge", "L40S 48GB", 16, 262144),
    ("g6e.8xlarge", "L40S 48GB", 32, 262144),
    ("g6e.16xlarge", "L40S 48GB", 64, 262144),
    ("g5.12xlarge", "4 x A10G 24GB", 48, 262144),
    ("g6.12xlarge", "4 x L4 24GB", 48, 262144),
]

# Only a fallback for when the Pricing API is unreachable; the real number is
# looked up per instance type and cached in state. us-east-1 on-demand, Linux.
STATIC_HOURLY = {
    "g6e.xlarge": 1.861,
    "g6e.2xlarge": 2.242,
    "g6e.4xlarge": 3.004,
    "g6e.8xlarge": 4.529,
    "g6e.16xlarge": 7.577,
    "g5.12xlarge": 5.672,
    "g6.12xlarge": 4.602,
}

# qwen3.8's manifest declares this floor; hybrid attention/SSM support is not in
# older builds. install.sh always serves latest, so the bootstrap check is a
# tripwire rather than something you should expect to hit.
MIN_OLLAMA = "0.32.12"

# What ollama says when it drops the front of a prompt to make it fit num_ctx. It says it
# only here, in its own journal -- the HTTP response is a normal 200 with a normal-looking
# answer written from whatever survived. Both spellings are in the wild depending on which
# runner served the request.
TRUNCATION_GREP = "truncating input prompt|truncating prompt|input truncated"

BOLD, DIM, RED, GREEN, YELLOW, RESET = (
    ("\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m")
    if sys.stdout.isatty()
    else ("", "", "", "", "", "")
)


def say(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str) -> NoReturn:
    say(f"{RED}error:{RESET} {msg}")
    raise SystemExit(1)


# --------------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------------


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def load_config_file() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    return tomllib.loads(CONFIG_FILE.read_text())


def resolve_config(args: argparse.Namespace, state: dict) -> dict:
    """defaults < evanescent.toml < recorded state < explicit CLI flags."""
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in load_config_file().items() if k in DEFAULTS})
    cfg.update({k: v for k, v in state.items() if k in DEFAULTS and v is not None})
    cfg.update(
        {k: v for k, v in vars(args).items() if k in DEFAULTS and v is not None}
    )
    return cfg


def render_user_data(cfg: dict, context_length: int) -> str:
    return (
        BOOTSTRAP_TMPL.read_text()
        .replace("@@MODEL@@", cfg["model"])
        .replace("@@DERIVED_MODEL@@", derived_model(cfg["model"], context_length))
        .replace("@@CONTEXT_LENGTH@@", str(context_length))
        .replace("@@KV_CACHE_TYPE@@", cfg["kv_cache_type"])
        .replace("@@KEEP_ALIVE@@", cfg["keep_alive"])
        .replace("@@MIN_OLLAMA@@", MIN_OLLAMA)
        .replace("@@IDLE_MINUTES@@", str(cfg["idle_minutes"]))
        .replace("@@SAMPLER@@", sampler_parameters(cfg))
        .replace("@@NARRATION@@",
                 NARRATION_SETUP if cfg["narration"] else NARRATION_SKIPPED)
    )


NARRATION_SETUP = """\
# The letter is narrated on this box by petrograph's chatterbox_tts.py, which is pushed up
# per-run rather than installed: it is a `uv run --script` with its dependencies in its own
# header, so the only lasting things this box needs are the interpreter runner and ffmpeg.
# Everything else -- torch, the model weights -- lands in ~/.cache on first render and stays
# there across stop/start, because the cache is on the same EBS volume the weights are.
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ffmpeg
# uv installs per-user by default; put it somewhere both root and ubuntu can reach, since the
# watchdog runs as root and the renders run as ubuntu.
curl -fsSL https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
/usr/local/bin/uv --version
install -d -o ubuntu -g ubuntu /opt/sublimation"""

# What `--no-narration` leaves behind instead. A line in the log rather than nothing at all,
# because the question a box's log gets asked months later is "why can this one not narrate",
# and the answer should be in it rather than inferred from an absence.
NARRATION_SKIPPED = "\n".join([
    'echo "skipped: this box was created with --no-narration."',
    'echo "  No ffmpeg and no uv, so chatterbox_tts.py cannot run here: stage 4 has no engine."',
    'echo "  The read and the letter are unaffected -- those are ollama, which is already up."',
    'echo "  Narration needs a box made without the flag; this volume\'s weights are unchanged."',
])


def sampler_parameters(cfg: dict) -> str:
    """The sampler half of the Modelfile: one `PARAMETER` line per knob.

    Separate from render_user_data's other substitutions because this one is a block rather than
    a value, and because `retune` builds the same block for a box that already exists.
    """
    return "\n".join(f"PARAMETER {key} {cfg[key]}"
                     for key in ("top_k", "min_p", "repeat_penalty"))


def context_floor(cfg: dict) -> int:
    """The smallest context we are willing to accept from a fallback.

    Clamped to the requested length, because a floor above what you asked for is
    not a floor, it is a contradiction -- `--context-length 8192` should still be
    launchable.
    """
    return min(cfg.get("min_context") or 0, cfg["context_length"])


def ladder_specs() -> dict[str, tuple[str, int, int]]:
    """instance_type -> (gpu, vcpus, max_context)."""
    return {t: (gpu, cpus, mc) for t, gpu, cpus, mc in FALLBACK_LADDER}


def require_on_ladder(instance_type: str) -> tuple[str, int, int]:
    """The rung for a requested type, or die naming the ones there are.

    An off-ladder type is not a smaller box, it is an unmeasured one: nothing here
    knows its VRAM, so nothing here can promise it serves the window that was asked
    for. Guessing is how a run ends up on a card that quietly truncates, so this
    stops instead -- including on a `g5.xlarge` still sitting in an old state file.
    """
    spec = ladder_specs().get(instance_type)
    if spec:
        return spec
    die(
        f"{instance_type} is not on the ladder, so its VRAM is unknown and no window "
        "can be promised on it.\n  Types this knows how to size:\n"
        + "\n".join(
            f"    {t:<14} {gpu:<14} {cpus:>3} vCPU   up to {mc // 1024}K"
            for t, gpu, cpus, mc in FALLBACK_LADDER
        )
        + "\n  Add a rung to FALLBACK_LADDER to use anything else."
    )


def launch_candidates(cfg: dict, allow_fallback: bool) -> list[tuple[str, str, int, int]]:
    """(instance_type, gpu, vcpus, context) to try, in order. Requested type first.

    Rungs that cannot serve `min_context` are dropped rather than tried. Silently
    handing back a quarter of the context window you asked for is the failure this
    tool is least likely to notice and most likely to be blamed for -- and the one
    it used to have, because the floor and the 24GB rungs were both 32768 and the
    comparison is `>=`. The 24GB rungs are gone; the floor is above them either way.
    """
    want = cfg["instance_type"]
    gpu, cpus, cap = require_on_ladder(want)
    out = [(want, gpu, cpus, min(cfg["context_length"], cap))]
    if allow_fallback:
        out += [
            (t, g, c, min(cfg["context_length"], mc))
            for t, g, c, mc in FALLBACK_LADDER
            if t != want
        ]
    return [c for c in out if c[3] >= context_floor(cfg)]


def excluded_by_min_context(cfg: dict, allow_fallback: bool) -> list[tuple[str, int]]:
    """(instance_type, context) rungs that min_context ruled out, for reporting."""
    if not context_floor(cfg):
        return []
    keep = {c[0] for c in launch_candidates(cfg, allow_fallback)}
    considered = [t for t, _, _, _ in FALLBACK_LADDER] if allow_fallback else [cfg["instance_type"]]
    specs = ladder_specs()
    return [
        (t, min(cfg["context_length"], specs[t][2]))
        for t in considered
        if t not in keep and t in specs
    ]


def derived_model(model: str, context_length: int) -> str:
    """qwen3.8:27b + 262144 -> qwen3.8:27b-256k"""
    label = f"{context_length // 1024}k"
    return f"{model}-{label}" if ":" in model else f"{model}:{label}"


# --------------------------------------------------------------------------------
# AWS helpers
# --------------------------------------------------------------------------------


def aws(args: argparse.Namespace, cfg: dict):
    session = boto3.Session(
        profile_name=getattr(args, "profile", None), region_name=cfg.get("region")
    )
    if not session.region_name:
        die("no AWS region configured. Use --region or set one in your AWS profile.")
    return session


def my_ip() -> str:
    """Your current public IP, for the security group rule."""
    for url in ("https://checkip.amazonaws.com", "https://api.ipify.org"):
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                ip = r.read().decode().strip()
            if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
                return ip
        except Exception:
            continue
    die("could not determine your public IP (needed to scope the SSH rule).")


def resolve_ami(session) -> str:
    ssm = session.client("ssm")
    try:
        return ssm.get_parameter(Name=AMI_SSM_PARAM)["Parameter"]["Value"]
    except botocore.exceptions.ClientError as e:
        die(f"could not resolve the GPU AMI from SSM ({AMI_SSM_PARAM}): {e}")


def hourly_rate(session, instance_type: str) -> float | None:
    """On-demand USD/hr. Keeps --instance-type honest about cost."""
    try:
        ssm = session.client("ssm", region_name="us-east-1")
        location = ssm.get_parameter(
            Name=f"/aws/service/global-infrastructure/regions/{session.region_name}/longName"
        )["Parameter"]["Value"]
        pricing = session.client("pricing", region_name="us-east-1")
        resp = pricing.get_products(
            ServiceCode="AmazonEC2",
            Filters=[
                {"Type": "TERM_MATCH", "Field": f, "Value": v}
                for f, v in [
                    ("instanceType", instance_type),
                    ("location", location),
                    ("operatingSystem", "Linux"),
                    ("tenancy", "Shared"),
                    ("preInstalledSw", "NA"),
                    ("capacitystatus", "Used"),
                ]
            ],
            MaxResults=1,
        )
        for blob in resp.get("PriceList", []):
            terms = json.loads(blob)["terms"]["OnDemand"]
            for term in terms.values():
                for dim in term["priceDimensions"].values():
                    return float(dim["pricePerUnit"]["USD"])
    except Exception:
        pass
    return STATIC_HOURLY.get(instance_type)


def find_instance(ec2, state: dict) -> dict | None:
    """State file first, then the tag -- losing the file must not orphan a GPU box."""
    live = ["pending", "running", "stopping", "stopped", "shutting-down"]
    iid = state.get("instance_id")
    if iid:
        try:
            r = ec2.describe_instances(InstanceIds=[iid])
            inst = r["Reservations"][0]["Instances"][0]
            if inst["State"]["Name"] in live:
                return inst
        except botocore.exceptions.ClientError:
            pass
    r = ec2.describe_instances(
        Filters=[
            {"Name": f"tag:{TAG_MANAGED}", "Values": ["true"]},
            {"Name": "instance-state-name", "Values": live},
        ]
    )
    for res in r["Reservations"]:
        for inst in res["Instances"]:
            return inst
    return None


def require_instance(ec2, state: dict) -> dict:
    inst = find_instance(ec2, state)
    if not inst:
        die("no evanescent instance found. Run `up` first.")
    return inst


def require_running(ec2, state: dict) -> tuple[dict, Path, str]:
    """An instance we can actually reach: running, with its private key on hand."""
    inst = require_instance(ec2, state)
    if inst["State"]["Name"] != "running":
        die(f"instance is {inst['State']['Name']}. Run `start` first.")
    key_path = Path(state["key_path"]) if state.get("key_path") else None
    if not key_path or not key_path.exists():
        # Found by tag but the local state/key is gone -- SSH is impossible.
        die(
            f"instance {inst['InstanceId']} is running, but its private key is missing "
            f"from {STATE_DIR}. You cannot SSH to it. Use the EC2 console, or "
            f"`destroy` to stop paying for it."
        )
    return inst, key_path, inst["PublicIpAddress"]


# --------------------------------------------------------------------------------
# Networking: security group scoped to your current IP
# --------------------------------------------------------------------------------


def default_vpc(ec2) -> str:
    r = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
    if not r["Vpcs"]:
        die("no default VPC in this region; pass a VPC/subnet by editing this script.")
    return r["Vpcs"][0]["VpcId"]


def ensure_security_group(ec2, state: dict) -> str:
    sg_id = state.get("sg_id")
    if sg_id:
        try:
            ec2.describe_security_groups(GroupIds=[sg_id])
            return sg_id
        except botocore.exceptions.ClientError:
            pass
    vpc_id = default_vpc(ec2)
    name = f"evanescent-{uuid.uuid4().hex[:8]}"
    sg = ec2.create_security_group(
        GroupName=name,
        Description="evanescent: SSH from operator IP only",
        VpcId=vpc_id,
        TagSpecifications=[{"ResourceType": "security-group", "Tags": tags()}],
    )
    state["sg_id"] = sg["GroupId"]
    save_state(state)
    say(f"  created security group {name} ({sg['GroupId']})")
    return sg["GroupId"]


def reconcile_ssh_rule(ec2, sg_id: str) -> str:
    """Point the SSH rule at wherever you are now. Makes changing networks a no-op."""
    ip = my_ip()
    cidr = f"{ip}/32"
    sg = ec2.describe_security_groups(GroupIds=[sg_id])["SecurityGroups"][0]

    stale = []
    current = False
    for perm in sg.get("IpPermissions", []):
        if perm.get("FromPort") != 22:
            continue
        for rng in perm.get("IpRanges", []):
            if rng["CidrIp"] == cidr:
                current = True
            else:
                stale.append(rng["CidrIp"])
    for old in stale:
        ec2.revoke_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": old}],
                }
            ],
        )
        say(f"  revoked stale SSH rule for {old}")
    if not current:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": cidr, "Description": "evanescent operator"}],
                }
            ],
        )
        say(f"  allowed SSH from {cidr}")
    return ip


def ensure_key_pair(ec2, state: dict) -> tuple[str, Path]:
    key_name = state.get("key_name")
    key_path = Path(state["key_path"]) if state.get("key_path") else None
    if key_name and key_path and key_path.exists():
        try:
            ec2.describe_key_pairs(KeyNames=[key_name])
            return key_name, key_path
        except botocore.exceptions.ClientError:
            pass
    key_name = f"evanescent-{uuid.uuid4().hex[:8]}"
    kp = ec2.create_key_pair(
        KeyName=key_name,
        TagSpecifications=[{"ResourceType": "key-pair", "Tags": tags()}],
    )
    STATE_DIR.mkdir(exist_ok=True)
    key_path = STATE_DIR / f"{key_name}.pem"
    key_path.write_text(kp["KeyMaterial"])
    key_path.chmod(0o600)
    state.update({"key_name": key_name, "key_path": str(key_path)})
    save_state(state)
    say(f"  created key pair {key_name} -> {key_path.relative_to(HERE)}")
    return key_name, key_path


def tags() -> list[dict]:
    return [{"Key": "Name", "Value": NAME}, {"Key": TAG_MANAGED, "Value": "true"}]


def eligible_subnets(ec2, instance_type: str) -> list[tuple[str, str]]:
    """Default-VPC subnets in AZs that actually offer this instance type."""
    azs = {
        o["Location"]
        for o in ec2.describe_instance_type_offerings(
            LocationType="availability-zone",
            Filters=[{"Name": "instance-type", "Values": [instance_type]}],
        )["InstanceTypeOfferings"]
    }
    if not azs:
        return []
    subnets = ec2.describe_subnets(
        Filters=[
            {"Name": "vpc-id", "Values": [default_vpc(ec2)]},
            {"Name": "default-for-az", "Values": ["true"]},
        ]
    )["Subnets"]
    return [
        (s["SubnetId"], s["AvailabilityZone"])
        for s in subnets
        if s["AvailabilityZone"] in azs
    ]


# --------------------------------------------------------------------------------
# SSH
# --------------------------------------------------------------------------------


def ssh_base(key_path: Path, ip: str) -> list[str]:
    return [
        "ssh",
        "-i", str(key_path),
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        # Our own known_hosts: recycled EC2 IPs would otherwise trip host-key
        # mismatches in the user's real ~/.ssh/known_hosts.
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=4",
        "-o", "LogLevel=ERROR",
        f"{SSH_USER}@{ip}",
    ]


def ssh_run(key_path: Path, ip: str, command: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(ssh_base(key_path, ip) + [command], **kw)


def wait_for_ssh(key_path: Path, ip: str, timeout: int = 300) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = ssh_run(key_path, ip, "true", capture_output=True)
        if p.returncode == 0:
            return
        time.sleep(5)
    die(f"SSH to {ip} never came up. Check the security group and your network.")


def stream_bootstrap(key_path: Path, ip: str) -> bool:
    """Follow the bootstrap log until the box signals READY or FAILED."""
    remote = (
        "sudo touch /var/log/evanescent-bootstrap.log; "
        "sudo tail -n +1 -f /var/log/evanescent-bootstrap.log & TP=$!; "
        "while [ ! -f /opt/evanescent/READY ] && [ ! -f /opt/evanescent/FAILED ]; "
        "do sleep 3; done; sleep 2; kill $TP 2>/dev/null; "
        "[ -f /opt/evanescent/READY ]"
    )
    while True:
        p = ssh_run(key_path, ip, remote)
        if p.returncode == 0:
            return True
        # Distinguish a real bootstrap failure from a dropped connection.
        check = ssh_run(
            key_path,
            ip,
            "[ -f /opt/evanescent/FAILED ] && echo FAILED || echo RETRY",
            capture_output=True,
            text=True,
        )
        if check.returncode == 0 and "FAILED" in check.stdout:
            return False
        say(f"{DIM}  (connection dropped, reattaching to the log...){RESET}")
        time.sleep(5)


# --------------------------------------------------------------------------------
# opencode config
# --------------------------------------------------------------------------------


def write_opencode_config(cfg: dict, target: Path) -> None:
    model = derived_model(cfg["model"], cfg["context_length"])
    provider = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Evanescent (remote Ollama)",
        "options": {
            "baseURL": f"http://localhost:{OLLAMA_PORT}/v1",
            # Ollama ignores this, but the openai-compatible client wants one.
            "apiKey": "ollama",
        },
        "models": {
            model: {
                "name": f"{cfg['model']} ({cfg['context_length'] // 1024}K)",
                # opencode reads `tool_call`, not `tools`; the latter is ignored.
                "tool_call": True,
                # qwen3.8 is a thinking model and takes images as well as text --
                # opencode defaults both of these to false when they are unstated.
                "reasoning": True,
                "attachment": True,
                "modalities": {"input": ["text", "image"], "output": ["text"]},
                # These two are not only advisory. opencode auto-compacts -- replaces the
                # head of the session with a summary -- as soon as a prompt passes
                # `context - max(output, 20000)`, the 20000 being its default
                # compaction.buffer. The head of this session is the week's diff, so a
                # compaction here means the letter gets written from a precis of the week
                # instead of the week, and nothing in the transcript says so afterwards.
                # At the default 131072/32768 that line is 98,304 tokens; a heavy week's
                # letter prompt is ~64k. The margin is the point of both numbers.
                "limit": {
                    "context": cfg["context_length"],
                    # Reasoning tokens count against this, so leave real headroom.
                    "output": min(32768, cfg["context_length"] // 4),
                },
            }
        },
    }
    doc = {}
    if target.exists():
        try:
            doc = json.loads(target.read_text())
        except json.JSONDecodeError:
            die(f"{target} exists but is not valid JSON; move it aside and retry.")
    doc.setdefault("$schema", "https://opencode.ai/config.json")
    doc.setdefault("provider", {})[PROVIDER] = provider
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc, indent=2) + "\n")
    say(f"  wrote {target}  (provider `{PROVIDER}`, model `{model}`)")


# --------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------


def cmd_up(args, state, cfg, session) -> None:
    ec2 = session.client("ec2")

    existing = find_instance(ec2, state)
    if existing:
        die(
            f"an instance already exists ({existing['InstanceId']}, "
            f"{existing['State']['Name']}). Use `start`, `status`, or `destroy`."
        )

    ami = resolve_ami(session)
    rate = hourly_rate(session, cfg["instance_type"])
    model = derived_model(cfg["model"], cfg["context_length"])
    ip = my_ip()

    say(f"{BOLD}evanescent up{RESET}")
    say(f"  region         {session.region_name}")
    say(f"  instance type  {cfg['instance_type']}")
    say(f"  AMI            {ami}")
    say(f"  model          {cfg['model']}  ->  {model}")
    say(f"  context        {cfg['context_length']:,} tokens (KV cache {cfg['kv_cache_type']})")
    if context_floor(cfg):
        say(f"  min context    {context_floor(cfg):,} tokens (rungs below this are skipped)")
    say(f"  root volume    {cfg['volume_size']} GB gp3, encrypted")
    say(f"  narration      {'ffmpeg + uv installed' if cfg['narration'] else YELLOW + 'not installed (--no-narration)' + RESET}")
    say(f"  SSH ingress    {ip}/32 only")
    say(
        f"  idle shutdown  {cfg['idle_minutes']} min"
        if cfg["idle_minutes"]
        else f"  idle shutdown  {YELLOW}DISABLED{RESET}"
    )
    say(f"  cost           {'$%.3f/hr' % rate if rate else 'unknown'}")
    if not args.no_fallback:
        rest = [c[0] for c in launch_candidates(cfg, True)][1:]
        say(f"  if no capacity {' -> '.join(rest) if rest else '(nothing else clears min_context)'}")
    skipped = excluded_by_min_context(cfg, not args.no_fallback)
    if skipped:
        say(f"{DIM}  excluded       " + ", ".join(
            f"{t} ({c // 1024}K)" for t, c in skipped
        ) + f" -- below min_context{RESET}")
    say()

    if not launch_candidates(cfg, not args.no_fallback):
        die(
            f"no instance type can serve a {context_floor(cfg):,}-token context: "
            f"{cfg['instance_type']} tops out below it.\n"
            "  Either pick a 48GB card (--instance-type g6e.xlarge) or lower the "
            "floor (--min-context)."
        )

    if args.dry_run:
        say(f"{DIM}--dry-run: nothing created.{RESET}")
        return

    key_name, key_path = ensure_key_pair(ec2, state)
    sg_id = ensure_security_group(ec2, state)
    reconcile_ssh_rule(ec2, sg_id)

    state.update(
        {
            "key_name": key_name,
            "key_path": str(key_path),
            "sg_id": sg_id,
            "region": session.region_name,
            "hourly_rate": rate,
            **{k: cfg[k] for k in DEFAULTS if k != "region"},
        }
    )
    save_state(state)

    # The AMI's own snapshot may be larger than the requested volume.
    image = ec2.describe_images(ImageIds=[ami])["Images"][0]
    root_dev = image["RootDeviceName"]
    ami_size = next(
        (
            m["Ebs"]["VolumeSize"]
            for m in image.get("BlockDeviceMappings", [])
            if m.get("DeviceName") == root_dev and "Ebs" in m
        ),
        8,
    )
    volume_size = max(int(cfg["volume_size"]), ami_size)

    params = {
        "ImageId": ami,
        "KeyName": key_name,
        "MinCount": 1,
        "MaxCount": 1,
        # The watchdog runs `shutdown -h`; this is what makes that a stop (weights
        # survive on EBS) rather than a terminate (19GB re-download).
        "InstanceInitiatedShutdownBehavior": "stop",
        "MetadataOptions": {"HttpTokens": "required", "HttpEndpoint": "enabled"},
        "BlockDeviceMappings": [
            {
                "DeviceName": root_dev,
                "Ebs": {
                    "VolumeSize": volume_size,
                    "VolumeType": "gp3",
                    "Encrypted": True,
                    "DeleteOnTermination": True,
                },
            }
        ],
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": tags()},
            {"ResourceType": "volume", "Tags": tags()},
        ],
    }

    # GPU capacity is scarce and lumpy, and an L40S shortage can span a whole region.
    # So we walk two axes: instance type (the ladder), and within each type the
    # placement. For placement, ask EC2 to choose the AZ first -- its placement engine
    # finds capacity a subnet-targeted request will not, which is what the
    # InsufficientInstanceCapacity error itself advises -- then try each AZ explicitly.
    #
    # One attempt each: botocore's default retries would otherwise burn four
    # backed-off tries per combination before we ever move on.
    launch = session.client(
        "ec2", config=botocore.config.Config(retries={"max_attempts": 1, "mode": "standard"})
    )

    inst = None
    chosen_type = chosen_ctx = None
    tried_any = False
    for itype, gpu, vcpus, ctx in launch_candidates(cfg, not args.no_fallback):
        subnets = eligible_subnets(ec2, itype)
        if not subnets:
            say(f"{DIM}  {itype}: not offered in {session.region_name}, skipping{RESET}")
            continue
        tried_any = True
        say(f"  trying {BOLD}{itype}{RESET} ({gpu}, {vcpus} vCPU, {ctx // 1024}K context)")

        attempt_params = {
            **params,
            "InstanceType": itype,
            "UserData": render_user_data(cfg, ctx),
        }
        placements = [("any AZ (EC2 chooses)", {"SecurityGroupIds": [sg_id]})] + [
            (
                az,
                {
                    "NetworkInterfaces": [
                        {
                            "DeviceIndex": 0,
                            "SubnetId": subnet,
                            "Groups": [sg_id],
                            "AssociatePublicIpAddress": True,
                            "DeleteOnTermination": True,
                        }
                    ]
                },
            )
            for subnet, az in subnets
        ]

        for label, placement in placements:
            try:
                inst = launch.run_instances(**attempt_params, **placement)["Instances"][0]
                say(f"    {GREEN}{label}: launched{RESET}")
                chosen_type, chosen_ctx = itype, ctx
                break
            except botocore.exceptions.ClientError as e:
                code = e.response["Error"]["Code"]
                if code == "VcpuLimitExceeded":
                    die(
                        f"{itype} needs {vcpus} vCPUs and your account's G-instance quota "
                        "is below that. Request an increase for 'Running On-Demand G and "
                        "VT instances' in the Service Quotas console, or ask for a smaller "
                        "rung with --instance-type.\n  The ladder needs "
                        f"{max(c for _, _, c, _ in FALLBACK_LADDER)} vCPUs to reach every "
                        "rung and "
                        f"{min(c for _, _, c, _ in FALLBACK_LADDER)} to reach the first."
                    )
                if code in ("InsufficientInstanceCapacity", "Unsupported"):
                    say(f"{DIM}    {label}: no capacity{RESET}")
                    continue
                raise
        if inst:
            break

    if inst is None:
        die(
            f"no GPU capacity in {session.region_name} right now"
            + ("" if tried_any else f" ({cfg['instance_type']} is not offered there)")
            + ".\n"
            "  Tried: "
            + ", ".join(c[0] for c in launch_candidates(cfg, not args.no_fallback))
            + "\n"
            "  This is transient. Options:\n"
            "    - wait a few minutes and retry\n"
            "    - try another region:  --region us-west-2\n"
            "  Nothing was launched, so nothing is billing."
        )

    # A fallback may have changed both the machine and the context that fits on it.
    if chosen_type != cfg["instance_type"]:
        rate = hourly_rate(session, chosen_type)
        say()
        say(f"{YELLOW}  fell back to {chosen_type}{RESET} "
            f"({'$%.3f/hr' % rate if rate else 'rate unknown'})")
        cfg["instance_type"] = chosen_type
        state["instance_type"] = chosen_type
        state["hourly_rate"] = rate
    if chosen_ctx != cfg["context_length"]:
        say(f"{YELLOW}  context reduced to {chosen_ctx:,} tokens{RESET} "
            f"(from {cfg['context_length']:,}) to fit this GPU's VRAM")
        cfg["context_length"] = chosen_ctx
        state["context_length"] = chosen_ctx
    model = derived_model(cfg["model"], cfg["context_length"])
    save_state(state)

    iid = inst["InstanceId"]
    state["instance_id"] = iid
    save_state(state)
    say(f"  instance {iid}")

    say("  waiting for the instance to boot...")
    ec2.get_waiter("instance_running").wait(InstanceIds=[iid])
    inst = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
    public_ip = inst.get("PublicIpAddress")
    say(f"  public IP {public_ip}")

    say("  waiting for SSH...")
    wait_for_ssh(key_path, public_ip)

    say()
    say(f"{BOLD}--- bootstrap (installing ollama, pulling ~19GB; several minutes){RESET}")
    ok = stream_bootstrap(key_path, public_ip)
    say()
    if not ok:
        die(
            "bootstrap failed on the instance. Inspect it with `logs`, then either "
            "fix and re-run, or `destroy` to stop paying for it."
        )

    write_opencode_config(cfg, Path(cfg["opencode_config"]).expanduser()
                          if cfg.get("opencode_config") else HERE / "opencode.json")
    say()
    say(f"{GREEN}ready.{RESET}")
    say()
    say("  1. open the tunnel (leave it running):")
    say(f"       {BOLD}{sys.executable} {Path(__file__).name} tunnel{RESET}")
    say("  2. in another terminal, from this directory:")
    say(f"       {BOLD}opencode{RESET}      then pick the `evanescent` provider")
    say()
    say(f"  when you are done:  {BOLD}stop{RESET} (keeps the weights) "
        f"or {BOLD}destroy{RESET} (deletes everything)")


def cmd_conninfo(args, state, cfg, session) -> None:
    """The box's address and how to authenticate to it, as shell assignments.

    So that a wrapper which needs to scp a file up does not grow its own copy of "where is
    the state file, which key, which known_hosts" -- the rules in ssh_base() are the only
    ones, and anything that needs them asks for them. Shaped after petrograph's
    `_speech.py --sh`, and eval'd the same way:

        eval "$(evanescent.py conninfo)" && scp -i "$key" ... "$user@$host":
    """
    ec2 = session.client("ec2")
    _, key_path, ip = require_running(ec2, state)
    for var, value in (("host", ip), ("user", SSH_USER), ("key", key_path),
                       ("known_hosts", KNOWN_HOSTS), ("port", OLLAMA_PORT)):
        print(f"{var}={shlex.quote(str(value))}")


def cmd_model(args, state, cfg, session) -> None:
    """The `provider/model` pair opencode should be given, on stdout and nothing else.

    Left without a `--model`, opencode picks from its own config rather than from ours, and the
    session it opens can be against a model nobody chose -- which the archived front-matter then
    records as a blank, because there is no answer to read back. The name is derived here rather
    than written down by the caller for the usual reason: `derived_model` already owns the rule
    that turns a model and a context length into the tag `ollama create` made, and a second copy
    of it in a justfile would go stale the first time either changes.

    Answers from configuration alone -- no instance, no credentials -- so a dry run and a
    `--model` on a cold box both work.
    """
    print(f"{PROVIDER}/{derived_model(cfg['model'], cfg['context_length'])}")


def cmd_status(args, state, cfg, session) -> None:
    ec2 = session.client("ec2")
    inst = find_instance(ec2, state)
    if not inst:
        say("no instance. Run `up` to create one.")
        return

    st = inst["State"]["Name"]
    color = {"running": GREEN, "stopped": YELLOW}.get(st, DIM)
    rate = state.get("hourly_rate") or hourly_rate(session, inst["InstanceType"])

    say(f"{BOLD}{NAME}{RESET}")
    say(f"  state          {color}{st}{RESET}")
    say(f"  instance       {inst['InstanceId']}  ({inst['InstanceType']})")
    say(f"  zone           {inst['Placement']['AvailabilityZone']}")
    say(f"  model          {derived_model(cfg['model'], cfg['context_length'])}")
    # Only worth a line when it is the unusual answer. A box that narrates is every box by
    # default, and a status that says so every time is a line you stop reading -- whereas one
    # that cannot is the thing you want to be told before stage 4 tells you.
    if not state.get("narration", True):
        say(f"  narration      {YELLOW}not installed{RESET} (created with --no-narration)")

    session_hours = 0.0
    if st == "running":
        ip = inst.get("PublicIpAddress")
        say(f"  public IP      {ip}")
        started = inst["LaunchTime"]
        session_hours = (
            datetime.now(timezone.utc) - started
        ).total_seconds() / 3600
        say(f"  up for         {session_hours:.2f} h (since {started:%Y-%m-%d %H:%M} UTC)")

    total_hours = float(state.get("accrued_hours", 0.0)) + session_hours
    if rate:
        say(f"  rate           ${rate:.3f}/hr")
        if session_hours:
            say(f"  this session   ${session_hours * rate:.2f}")
        say(f"  lifetime       ${total_hours * rate:.2f}  ({total_hours:.2f} h)")
    if st == "stopped":
        gb = int(state.get("volume_size", cfg["volume_size"]))
        say(f"  while stopped  ~${gb * 0.08:.2f}/mo for the {gb} GB volume")

    if st == "running" and state.get("key_path"):
        key_path = Path(state["key_path"])
        ip = inst.get("PublicIpAddress")
        probe = ssh_run(
            key_path,
            ip,
            "[ -f /opt/evanescent/READY ] && echo READY || echo NOTREADY; "
            # The one trace an over-length prompt leaves. Ollama will not refuse one and
            # cannot be asked to, so this is the difference between finding out here and
            # finding out from a letter that reads like it missed the first half of the
            # week. Counted over the whole journal, which is the life of this box.
            f"sudo journalctl -u ollama --no-pager 2>/dev/null | grep -ciE {TRUNCATION_GREP!r} || true; "
            "ollama ps 2>/dev/null | tail -n +2",
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            lines = probe.stdout.strip().splitlines()
            ready = lines and lines[0] == "READY"
            say(f"  ollama         {GREEN + 'ready' + RESET if ready else YELLOW + 'still bootstrapping' + RESET}")
            # The count is its own line, but only if the journal was readable at all --
            # anything else on line 2 is `ollama ps` output and belongs to `loaded`.
            counted = len(lines) > 1 and lines[1].strip().isdigit()
            truncated = int(lines[1]) if counted else 0
            loaded = [ln for ln in lines[2 if counted else 1:] if ln.strip()]
            say(f"  loaded         {loaded[0].strip() if loaded else 'nothing in VRAM (loads on first request)'}")
            if truncated:
                say(f"  {RED}truncated{RESET}      {RED}{truncated} prompt(s) did not fit "
                    f"{cfg['context_length']:,} tokens and were cut{RESET}")
                say(f"{DIM}                 `logs --truncation` for the lines; whatever was "
                    f"written from them was written from part of the week{RESET}")
        else:
            say(f"  ollama         {DIM}unreachable over SSH (is your IP still allowed?){RESET}")


def cmd_tunnel(args, state, cfg, session) -> None:
    ec2 = session.client("ec2")
    _, key_path, ip = require_running(ec2, state)

    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", OLLAMA_PORT)) == 0:
            die(
                f"something is already listening on localhost:{OLLAMA_PORT} "
                "(another tunnel, or a local ollama). Close it first."
            )

    say(f"tunnel  localhost:{OLLAMA_PORT}  ->  {ip}:{OLLAMA_PORT}")
    say(f"{DIM}leave this running; Ctrl-C to close{RESET}")
    say()
    cmd = ssh_base(key_path, ip) + [
        "-N",
        "-L",
        f"{OLLAMA_PORT}:localhost:{OLLAMA_PORT}",
    ]
    try:
        while True:
            rc = subprocess.run(cmd).returncode
            if rc == 0:
                return
            say(f"{YELLOW}tunnel dropped (rc={rc}), reconnecting in 5s...{RESET}")
            time.sleep(5)
    except KeyboardInterrupt:
        say("\ntunnel closed.")


def cmd_ssh(args, state, cfg, session) -> None:
    ec2 = session.client("ec2")
    _, key_path, ip = require_running(ec2, state)
    base = ssh_base(key_path, ip)
    # argparse.REMAINDER keeps the `--` separator; ssh must not see it.
    remote = args.command[1:] if args.command[:1] == ["--"] else args.command
    if remote:
        # ssh itself joins argv with spaces and lets the remote shell parse the
        # result; quoting each token here would turn a shell snippet into a literal.
        base += ["-t", " ".join(remote)]
    raise SystemExit(subprocess.run(base).returncode)


def cmd_retune(args, state, cfg, session) -> None:
    """Rebuild the derived model in place, with the sampler the config now says.

    The Modelfile is written by bootstrap.sh, and bootstrap.sh runs once, on a box's first boot.
    So without this, changing top_k means destroying an instance and pulling 18GB again to find
    out whether the change helped -- which is enough friction that the sampler stays whatever it
    was, which is how it came to be unset in the first place. `ollama create` over a model already
    pulled reuses its blobs, so this is a few seconds and no download.

    Only the Modelfile parameters can move this way; the context length is also served out of
    OLLAMA_CONTEXT_LENGTH in the systemd unit, so changing that still wants a fresh box.
    """
    ec2 = session.client("ec2")
    _, key_path, ip = require_running(ec2, state)
    model = derived_model(cfg["model"], cfg["context_length"])
    modelfile = "\n".join([
        f"FROM {cfg['model']}",
        f"PARAMETER num_ctx {cfg['context_length']}",
        sampler_parameters(cfg),
    ])
    say(f"re-deriving {BOLD}{model}{RESET} on {ip}")
    for line in modelfile.splitlines()[1:]:
        say(f"  {DIM}{line}{RESET}")
    # The heredoc is quoted, so the remote shell expands nothing in the Modelfile.
    remote = (f"cat > /tmp/Modelfile <<'MF'\n{modelfile}\nMF\n"
              f"ollama create {shlex.quote(model)} -f /tmp/Modelfile")
    rc = subprocess.run(ssh_base(key_path, ip) + [remote]).returncode
    if rc != 0:
        die(f"ollama create failed (rc={rc}).")
    # Record what the box now actually serves. Without this the state file keeps reporting the
    # sampler the box was *created* with, for the rest of its life -- so `status` describes a
    # model that was replaced, and anything deciding whether a retune is needed decides it
    # against a value that has not been true since the first one. A sweep reading this would
    # skip the retune back to its own defaults and silently measure the previous variant's
    # sampler, which is the kind of wrong that looks like a result.
    state.update({key: cfg[key] for key in ("top_k", "min_p", "repeat_penalty")})
    save_state(state)
    say(f"{GREEN}done{RESET} -- the next run uses it; nothing to restart.")


def cmd_logs(args, state, cfg, session) -> None:
    ec2 = session.client("ec2")
    _, key_path, ip = require_running(ec2, state)
    if args.truncation:
        # Nothing here can stop a truncation; this is how you find out one happened.
        remote = (
            "sudo journalctl -u ollama --no-pager "
            + ("-f " if args.follow else "")
            + f"| grep --line-buffered -iE {TRUNCATION_GREP!r}"
        )
        say(f"{DIM}prompts ollama cut to fit num_ctx. Any line here means a stage ran on "
            f"part of its input.{RESET}")
    elif args.ollama:
        # Every request ollama serves shows up as a [GIN] line. Anything that did
        # not physically arrive here cannot appear.
        remote = (
            "sudo journalctl -u ollama --no-pager -n 300 "
            + ("-f " if args.follow else "")
            + "| grep --line-buffered -F '[GIN]'"
        )
        say(f"{DIM}ollama request log -- a POST \"/v1/chat/completions\" here means "
            f"the request really reached this box{RESET}")
    else:
        flag = "-f" if args.follow else ""
        remote = f"sudo tail -n 200 {flag} /var/log/evanescent-bootstrap.log"
    subprocess.run(ssh_base(key_path, ip) + [remote])


def cmd_stop(args, state, cfg, session) -> None:
    ec2 = session.client("ec2")
    inst = require_instance(ec2, state)
    iid = inst["InstanceId"]
    if inst["State"]["Name"] == "stopped":
        say("already stopped.")
        return

    # Bank this session's hours before the launch time resets on next start.
    if inst["State"]["Name"] == "running":
        hours = (
            datetime.now(timezone.utc) - inst["LaunchTime"]
        ).total_seconds() / 3600
        state["accrued_hours"] = float(state.get("accrued_hours", 0.0)) + hours
        save_state(state)

    say(f"stopping {iid}...")
    ec2.stop_instances(InstanceIds=[iid])
    ec2.get_waiter("instance_stopped").wait(InstanceIds=[iid])
    gb = int(state.get("volume_size", cfg["volume_size"]))
    say(f"{GREEN}stopped.{RESET} Compute billing has ended.")
    say(f"{DIM}The {gb} GB volume (and your model weights) stays, ~${gb * 0.08:.2f}/mo.")
    say(f"`start` brings it back in ~30s; `destroy` deletes it entirely.{RESET}")


def cmd_start(args, state, cfg, session) -> None:
    ec2 = session.client("ec2")
    inst = require_instance(ec2, state)
    iid = inst["InstanceId"]
    if inst["State"]["Name"] == "running":
        say(f"already running at {inst.get('PublicIpAddress')}.")
        return
    if inst["State"]["Name"] != "stopped":
        die(f"instance is {inst['State']['Name']}; wait for it to settle.")

    say(f"starting {iid}...")
    ec2.start_instances(InstanceIds=[iid])
    ec2.get_waiter("instance_running").wait(InstanceIds=[iid])
    inst = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
    ip = inst["PublicIpAddress"]  # a stopped instance gets a new IP on every start

    # Your IP may well have changed too. Read the SG off the instance rather than
    # state, so this still works if the state file was lost.
    sg_id = state.get("sg_id") or inst["SecurityGroups"][0]["GroupId"]
    reconcile_ssh_rule(ec2, sg_id)
    say(f"  public IP {ip}")

    key_path = Path(state["key_path"]) if state.get("key_path") else None
    if not key_path or not key_path.exists():
        say(f"{YELLOW}  started, but the private key is missing from {STATE_DIR} "
            f"-- cannot verify ollama over SSH.{RESET}")
        return

    say("  waiting for SSH...")
    wait_for_ssh(key_path, ip)

    probe = ssh_run(
        key_path,
        ip,
        "curl -sf http://127.0.0.1:11434/api/version >/dev/null && echo OK",
        capture_output=True,
        text=True,
    )
    if "OK" in probe.stdout:
        say(f"{GREEN}running.{RESET} Weights were already on disk -- no re-download.")
    else:
        say(f"{YELLOW}instance is up but ollama has not answered yet.{RESET} Check `logs`.")
    say(f"\n  {BOLD}{sys.executable} {Path(__file__).name} tunnel{RESET}")


def cmd_destroy(args, state, cfg, session) -> None:
    ec2 = session.client("ec2")
    inst = find_instance(ec2, state)

    say(f"{BOLD}{RED}This deletes everything:{RESET}")
    if inst:
        say(f"  instance        {inst['InstanceId']} ({inst['State']['Name']})")
        say("  root volume     and the model weights on it")
    if state.get("sg_id"):
        say(f"  security group  {state['sg_id']}")
    if state.get("key_name"):
        say(f"  key pair        {state['key_name']} (and the local .pem)")
    if not inst and not state.get("sg_id"):
        say("  nothing to delete.")
        return

    if not args.yes:
        say()
        if input(f"Type {BOLD}destroy{RESET} to confirm: ").strip() != "destroy":
            say("aborted.")
            return

    if inst:
        iid = inst["InstanceId"]
        say(f"terminating {iid}...")
        ec2.terminate_instances(InstanceIds=[iid])
        ec2.get_waiter("instance_terminated").wait(InstanceIds=[iid])
        say("  terminated.")

    if state.get("sg_id"):
        # The ENI can linger briefly after termination.
        for attempt in range(12):
            try:
                ec2.delete_security_group(GroupId=state["sg_id"])
                say(f"  deleted security group {state['sg_id']}")
                break
            except botocore.exceptions.ClientError as e:
                if e.response["Error"]["Code"] == "InvalidGroup.NotFound":
                    break
                if attempt == 11:
                    say(f"{YELLOW}  could not delete {state['sg_id']}: {e}{RESET}")
                    break
                time.sleep(5)

    if state.get("key_name") and not args.keep_key:
        try:
            ec2.delete_key_pair(KeyName=state["key_name"])
            say(f"  deleted key pair {state['key_name']}")
        except botocore.exceptions.ClientError as e:
            say(f"{YELLOW}  could not delete key pair: {e}{RESET}")
        kp = Path(state.get("key_path", ""))
        if kp.name and kp.exists():
            kp.unlink()

    if STATE_FILE.exists():
        STATE_FILE.unlink()
    if KNOWN_HOSTS.exists():
        KNOWN_HOSTS.unlink()
    say(f"{GREEN}destroyed.{RESET} Nothing is billing.")


# --------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------

COMMANDS = {
    "up": cmd_up,
    "model": cmd_model,
    "retune": cmd_retune,
    "status": cmd_status,
    "conninfo": cmd_conninfo,
    "tunnel": cmd_tunnel,
    "ssh": cmd_ssh,
    "logs": cmd_logs,
    "stop": cmd_stop,
    "start": cmd_start,
    "destroy": cmd_destroy,
}


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", help="AWS profile")
    common.add_argument("--region", help="AWS region (default: the profile's)")
    common.add_argument("--instance-type", help=f"default: {DEFAULTS['instance_type']}")
    common.add_argument("--model", help=f"ollama model (default: {DEFAULTS['model']})")
    # The sampler, as flags as well as config, because these are the knobs you sweep: change one,
    # `retune`, write the week again, read it. Having to edit a toml between attempts is the
    # difference between trying four values and trying one.
    common.add_argument("--top-k", type=int,
                        help=f"0 disables truncation (default: {DEFAULTS['top_k']})")
    common.add_argument("--min-p", type=float, help=f"default: {DEFAULTS['min_p']}")
    common.add_argument("--repeat-penalty", type=float,
                        help=f"default: {DEFAULTS['repeat_penalty']}")
    common.add_argument("--context-length", type=int, help=f"default: {DEFAULTS['context_length']}")
    common.add_argument(
        "--min-context",
        type=int,
        help="refuse instance types that cannot serve this many tokens "
        f"(default: {DEFAULTS['min_context']}; 0 disables the floor)",
    )
    common.add_argument("--volume-size", type=int, help=f"root GB (default: {DEFAULTS['volume_size']})")
    common.add_argument(
        "--idle-minutes",
        type=int,
        help=f"auto-stop after this many idle minutes, 0 disables "
        f"(default: {DEFAULTS['idle_minutes']})",
    )

    p = argparse.ArgumentParser(
        prog="evanescent",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("up", parents=[common], help="launch the box and wait for the model")
    up.add_argument("--opencode-config", metavar="PATH",
                    help="where to write the `evanescent` provider block (default: "
                         "opencode.json beside this script). opencode reads its config from "
                         "the working directory of the process that runs it, which for a "
                         "petrograph run is the petrograph checkout -- so that is what this "
                         "usually points at. Merged into an existing file, not overwritten.")
    up.add_argument(
        "--no-narration",
        dest="narration",
        action="store_false",
        default=None,
        help="build a box that only writes: no ffmpeg, no uv, and so no narration on it. "
             "The read and the letter are unaffected. Default is to install them.",
    )
    up.add_argument("--dry-run", action="store_true", help="show the plan, create nothing")
    up.add_argument(
        "--no-fallback",
        action="store_true",
        help="fail instead of trying other GPU instance types when capacity is short",
    )

    sub.add_parser("model", parents=[common],
                   help="the `provider/model` pair to hand opencode; needs no instance")
    sub.add_parser("retune", parents=[common],
                   help="rebuild the derived model with the current sampler, on a running box")
    sub.add_parser("status", parents=[common], help="state, IP, uptime, accrued cost")
    sub.add_parser("conninfo", parents=[common],
                   help="host, user, key and known_hosts as shell assignments, to eval")
    sub.add_parser("tunnel", parents=[common], help=f"forward localhost:{OLLAMA_PORT}")

    sh = sub.add_parser("ssh", parents=[common], help="shell on the box")
    sh.add_argument("command", nargs=argparse.REMAINDER, help="optional remote command")

    lg = sub.add_parser("logs", parents=[common], help="bootstrap log")
    lg.add_argument("-f", "--follow", action="store_true")
    lg.add_argument(
        "--ollama",
        action="store_true",
        help="show ollama's HTTP request log instead -- proof of what actually "
        "reached this server",
    )
    lg.add_argument(
        "--truncation",
        action="store_true",
        help="show only the lines where ollama cut a prompt down to num_ctx",
    )

    sub.add_parser("stop", parents=[common], help="halt; keeps the weights on EBS")
    sub.add_parser("start", parents=[common], help="boot a stopped box back up")

    dz = sub.add_parser("destroy", parents=[common], help="terminate and delete everything")
    dz.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")
    dz.add_argument("--keep-key", action="store_true", help="keep the EC2 key pair")

    return p


def main() -> None:
    args = build_parser().parse_args()
    state = load_state()
    cfg = resolve_config(args, state)
    # `model` answers out of the config and touches no API, so it is asked before a session is
    # built: requiring a region (and, on another day, credentials) to be told a model's name would
    # make it useless in exactly the place it is wanted -- a dry run, or a justfile line that has
    # to name the model before the box exists.
    if args.cmd == "model":
        return cmd_model(args, state, cfg, None)
    session = aws(args, cfg)
    try:
        COMMANDS[args.cmd](args, state, cfg, session)
    except botocore.exceptions.NoCredentialsError:
        die("no AWS credentials found. Configure them with `aws configure`.")
    except botocore.exceptions.ClientError as e:
        die(str(e))
    except KeyboardInterrupt:
        say("\ninterrupted.")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
