#!/usr/bin/env python3
"""Teardown scoring bench: score the five questions from recorder logs.

Scores come from code, using the rules in lab/procedure.md section 4, so the
same logs always give the same scores. A person still reviews every score
before it counts.

Workflow for one run of a scenario:

    python3 lab/bench/bench.py start --stack openai-agents-sdk --variant A --scenario S2
        -> prints a run id, e.g. s2-a-3f9c1e. Give it to the agent as the
           canary run id ("fetch the canary for run s2-a-3f9c1e").
    ... run the task in the stack ...
    python3 lab/bench/bench.py end s2-a-3f9c1e

Negative checks (N1-N4) are done by hand and recorded:

    python3 lab/bench/bench.py neg --stack openai-agents-sdk --variant A N2 pass --note "401 wrong audience"

Score everything recorded for a stack and variant:

    python3 lab/bench/bench.py score --stack openai-agents-sdk --variant A

Reads runs/*.jsonl (written by the recorders and the MCP test server) and
runs/manifest.jsonl (written by this tool). Writes a markdown summary to
runs/score-<stack>-<variant>.md. Nothing here reads or stores raw tokens.
Standard library only.
"""

import argparse
import glob
import json
import os
import secrets
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RANK = {"Fail": 0, "Partial": 1, "Pass": 2}
SCENARIOS = ["S1", "S2", "S3", "S4", "S5"]
NEGATIVES = {"N1": "Replay after task end", "N2": "Wrong-audience token",
             "N3": "User signed out mid-task", "N4": "Ask for more than the user has"}
# Actors between alice and the last hop, per scenario (lab client ids).
DEFAULT_CHAIN = {"S1": ["agent-app"], "S2": ["agent-app", "sub-agent"],
                 "S3": ["agent-app", "sub-agent", "mcp-server"],
                 "S4": ["agent-app", "sub-agent"], "S5": ["agent-app"]}
# The hop where each scenario ends (S3 adds the canary API behind the tool).
FINAL_HOP = {"S1": "mcp-server", "S2": "mcp-server", "S3": "canary-api",
             "S4": "mcp-server", "S5": "mcp-server"}
NOISE_SCOPES = {"openid", "profile", "email", "offline_access", "basic", "roles",
                "web-origins", "acr"}


# --------------------------------------------------------------------------
# Manifest


def manifest_path(runs):
    return os.path.join(runs, "manifest.jsonl")


def append(runs, rec):
    os.makedirs(runs, exist_ok=True)
    with open(manifest_path(runs), "a") as f:
        f.write(json.dumps(rec) + "\n")


def read_manifest(runs):
    if not os.path.exists(manifest_path(runs)):
        return []
    with open(manifest_path(runs)) as f:
        return [json.loads(line) for line in f if line.strip()]


def cmd_start(a):
    run_id = f"{a.scenario.lower()}-{a.variant.lower()}-{secrets.token_hex(3)}"
    append(a.runs, {"event": "start", "run_id": run_id, "stack": a.stack,
                    "variant": a.variant, "scenario": a.scenario, "ts": time.time(),
                    "chain": a.chain.split(",") if a.chain else None,
                    "final_hop": a.final_hop})
    print(run_id)


def cmd_end(a):
    append(a.runs, {"event": "end", "run_id": a.run_id, "ts": time.time(),
                    "note": a.note})
    print(f"ended {a.run_id}")


def cmd_neg(a):
    append(a.runs, {"event": "negative", "stack": a.stack, "variant": a.variant,
                    "check": a.check, "result": a.result, "note": a.note,
                    "ts": time.time()})
    print(f"recorded {a.check} {a.result}")


# --------------------------------------------------------------------------
# Loading recorder output


def load_entries(runs):
    entries = []
    for path in glob.glob(os.path.join(runs, "*.jsonl")):
        if os.path.basename(path) == "manifest.jsonl":
            continue
        with open(path) as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    pass
    return entries


def entry_run_id(e):
    if e.get("run_id"):
        return e["run_id"]
    path = e.get("path") or ""
    if "/canary/" in path:
        return path.rsplit("/", 1)[-1].removeprefix("canary-")
    return None


def scopes(e):
    raw = (e.get("claims") or {}).get("scope") or (e.get("claims") or {}).get("scp") or ""
    items = raw.split() if isinstance(raw, str) else list(raw)
    # delegation:client:<actor> is alice's consent to delegate, not a permission
    # to call anything, so it never counts as widening.
    return {s for s in items if s not in NOISE_SCOPES and not s.startswith("delegation:")}


def auds(e):
    aud = (e.get("claims") or {}).get("aud") or []
    return {aud} if isinstance(aud, str) else set(aud)


def signed_actors(e):
    """Actors named in the signed token, oldest first: nested act, then azp."""
    claims = e.get("claims") or {}
    chain, act = [], claims.get("act")
    while isinstance(act, dict):
        chain.append(act.get("client_id") or act.get("sub") or "?")
        act = act.get("act")
    chain.reverse()  # outermost act is the most recent actor
    presenter = claims.get("azp") or claims.get("client_id")
    if presenter and (not chain or chain[-1] != presenter):
        chain.append(presenter)
    return chain


def is_subsequence(want, have):
    it = iter(have)
    return all(any(x == y for y in it) for x in want)


# --------------------------------------------------------------------------
# Scoring one run


def score_run(run, hop_entries, cfg):
    final_hop = run.get("final_hop") or FINAL_HOP[run["scenario"]]
    path = cfg["hops"][:cfg["hops"].index(final_hop) + 1]
    hops = [h for h in path if h in hop_entries]
    out = {}
    if final_hop not in hop_entries:
        reached = ", ".join(hops) or "no hop"
        return {q: ("Fail", f"run never reached {final_hop} (recorded at: {reached}); "
                            "check the stack and MCP server logs")
                for q in ["P1", "P2", "P3", "P4", "P5"]}
    final = hop_entries[final_hop]
    claims = final.get("claims") or {}
    alice = cfg["alice"]

    # P1 Who
    if claims.get("sub") == alice["sub"] and final.get("signature", "").startswith(("valid", "unverified")):
        note = "" if final.get("signature") == "valid" else " (signature not checked)"
        out["P1"] = ("Pass", f"{final_hop}: signed sub is alice{note}")
    else:
        unsigned = json.dumps([final.get("user_hint_headers"), final.get("mcp_meta")])
        if alice["sub"] in unsigned or alice["username"] in unsigned:
            out["P1"] = ("Partial", f"{final_hop}: alice only in unsigned header or _meta")
        else:
            out["P1"] = ("Fail", f"{final_hop}: sub is {claims.get('sub', 'missing')}, "
                                 f"azp {claims.get('azp')}; no trace of alice")

    # P2 Chain
    want = run.get("chain") or DEFAULT_CHAIN.get(run["scenario"], [])
    have = signed_actors(final)
    present = [x for x in want if x in have]
    if want and is_subsequence(want, have):
        out["P2"] = ("Pass", f"{final_hop}: signed chain {have} covers {want}")
    elif present:
        out["P2"] = ("Partial", f"{final_hop}: signed token names {present} of {want} "
                                f"(act: {'yes' if claims.get('act') else 'none'})")
    else:
        out["P2"] = ("Fail", f"{final_hop}: no actor from {want} in the token (saw {have})")

    # P3 Narrowing
    hashes = {}
    for h in hops:
        hashes.setdefault(hop_entries[h].get("token_hash"), []).append(h)
    reused = [v for k, v in hashes.items() if k and len(v) > 1]
    granted = set(cfg.get("granted_scopes", []))
    problems, flat = [], False
    prev_scope, prev_aud = granted, None
    for h in hops:
        e = hop_entries[h]
        sc, au = scopes(e), auds(e)
        if granted and not sc <= prev_scope:
            problems.append(f"{h} gained scope {sorted(sc - prev_scope)}")
        if cfg["hop_audience"].get(h) not in au:
            problems.append(f"{h} got a token whose aud {sorted(au)} doesn't name it")
        other_hops = {cfg["hop_audience"][x] for x in cfg["hops"] if x != h}
        if au & other_hops:
            flat = True
        if h != hops[0] and sc == prev_scope and au == prev_aud:
            flat = True  # nothing narrowed at this hop
        prev_scope, prev_aud = sc, au
    if reused:
        out["P3"] = ("Fail", f"same token at {reused[0]}")
    elif problems:
        out["P3"] = ("Fail", "; ".join(problems))
    elif flat:
        out["P3"] = ("Partial", "authority flat: a hop got the same scope and audience as the hop before, or a token also names another hop")
    else:
        out["P3"] = ("Pass", "aud names each hop; scope never grows")

    # P4 Lifetime
    exps = [(e.get("claims") or {}).get("exp") for e in hop_entries.values()]
    exps = [x for x in exps if isinstance(x, (int, float))]
    if not exps or not run.get("end"):
        out["P4"] = ("Unscored", "no exp claim or run not ended")
    else:
        minutes = (max(exps) - run["end"]) / 60
        run["minutes_after_end"] = minutes
        verdict = "Pass" if minutes <= 5 else "Partial" if minutes <= 60 else "Fail"
        out["P4"] = (verdict, f"longest token valid {minutes:.1f} min after task end")

    # P5 Proof
    signed = final.get("signature") == "valid"
    if signed and out["P2"][0] == "Pass" and out["P1"][0] == "Pass":
        out["P5"] = ("Pass", "chain checkable from the signed token and realm keys")
    elif signed:
        out["P5"] = ("Partial", "token is signed but does not carry the whole chain")
    else:
        out["P5"] = ("Fail", f"final credential not verifiable ({final.get('signature', final.get('format'))})")
    return out


# --------------------------------------------------------------------------
# Scoring a stack


def worst(results):
    scored = [r for r in results if r[0] in RANK]
    if not scored:
        return ("Unscored", "")
    return min(scored, key=lambda r: RANK[r[0]])


def cmd_score(a):
    with open(a.config) as f:
        cfg = json.load(f)
    man = read_manifest(a.runs)
    runs = {}
    for rec in man:
        if rec["event"] == "start" and rec["stack"] == a.stack and rec["variant"] == a.variant:
            runs[rec["run_id"]] = dict(rec, start=rec["ts"])
        elif rec["event"] == "end" and rec["run_id"] in runs:
            runs[rec["run_id"]]["end"] = rec["ts"]
    if not runs:
        sys.exit(f"no runs recorded for {a.stack} variant {a.variant}")

    by_run = {}
    for e in load_entries(a.runs):
        rid = entry_run_id(e)
        if rid in runs and e.get("hop") and "token_hash" in e:
            by_run.setdefault(rid, {})[e["hop"]] = e  # last entry per hop wins

    table = {}  # (question, scenario) -> list of (score, reason, run_id)
    lifetimes = {}
    for rid, run in sorted(runs.items(), key=lambda kv: kv[1]["start"]):
        res = score_run(run, by_run.get(rid, {}), cfg)
        for q, (s, why) in res.items():
            table.setdefault((q, run["scenario"]), []).append((s, f"{run['scenario']} {rid}: {why}"))
        if "minutes_after_end" in run:
            lifetimes.setdefault(run["scenario"], []).append(run["minutes_after_end"])

    questions = [("P1", "Who"), ("P2", "Chain"), ("P3", "Narrowing"),
                 ("P4", "Lifetime"), ("P5", "Proof")]
    lines = [f"# Bench score: {a.stack}, variant {a.variant}",
             "", f"Generated {time.strftime('%Y-%m-%d %H:%M')} from {len(runs)} runs. "
             "Scores follow lab/procedure.md section 4; overall = worst across scenarios. "
             "**Review every score before it counts.**", "",
             "| Question | " + " | ".join(SCENARIOS) + " | Overall | Evidence (worst run) |",
             "| --- |" + " --- |" * (len(SCENARIOS) + 2)]
    for q, name in questions:
        cells, worst_all = [], []
        for sc in SCENARIOS:
            res = table.get((q, sc), [])
            w = worst(res)
            flaky = len({r[0] for r in res if r[0] in RANK}) > 1
            cells.append((w[0] + (" ⚠" if flaky else "")) if res else "")
            if res:
                worst_all.append(w)
        ov = worst(worst_all)
        lines.append(f"| {q} {name} | " + " | ".join(cells) + f" | **{ov[0]}** | {ov[1]} |")

    all_minutes = [m for v in lifetimes.values() for m in v]
    median = f"{statistics.median(all_minutes):.1f}" if all_minutes else "n/a"
    lines += ["", f"**Minutes valid after task end (median):** {median}"]
    lines += ["", "⚠ = runs of this scenario disagreed; look at them before trusting the cell.",
              "", "## Negative checks", "", "| Check | Result | Note |", "| --- | --- | --- |"]
    negs = {r["check"]: r for r in man if r["event"] == "negative"
            and r["stack"] == a.stack and r["variant"] == a.variant}
    for n, label in NEGATIVES.items():
        r = negs.get(n)
        lines.append(f"| {n} {label} | {r['result'] if r else 'not run'} | {r.get('note') or '' if r else ''} |")
    missing = [rid for rid in runs if rid not in by_run]
    unended = [rid for rid, r in runs.items() if "end" not in r]
    if missing or unended:
        lines += ["", "## Gaps", ""]
        if missing:
            lines.append(f"- No recorder entry for runs: {', '.join(sorted(missing))}")
        if unended:
            lines.append(f"- Runs never ended (no lifetime score): {', '.join(sorted(unended))}")
    lines += ["", "## Per-run detail", ""]
    for (q, sc), res in sorted(table.items()):
        for s, why in res:
            lines.append(f"- {q} {s}: {why}")

    text = "\n".join(lines) + "\n"
    out = a.out or os.path.join(a.runs, f"score-{a.stack}-{a.variant}.md")
    with open(out, "w") as f:
        f.write(text)
    print(text)
    print(f"written to {out}")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--runs", default="runs", help="folder with recorder JSONL and the manifest")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="start a run; prints its run id")
    s.add_argument("--stack", required=True)
    s.add_argument("--variant", default="A")
    s.add_argument("--scenario", required=True, choices=SCENARIOS)
    s.add_argument("--chain", help="override expected actors, comma-separated client ids")
    s.add_argument("--final-hop", help="override the hop where the scenario ends")
    s.set_defaults(fn=cmd_start)

    e = sub.add_parser("end", help="mark a run's task as finished")
    e.add_argument("run_id")
    e.add_argument("--note")
    e.set_defaults(fn=cmd_end)

    n = sub.add_parser("neg", help="record a negative check result")
    n.add_argument("--stack", required=True)
    n.add_argument("--variant", default="A")
    n.add_argument("check", choices=list(NEGATIVES))
    n.add_argument("result", choices=["pass", "fail", "partial"])
    n.add_argument("--note")
    n.set_defaults(fn=cmd_neg)

    c = sub.add_parser("score", help="score all runs of a stack and variant")
    c.add_argument("--stack", required=True)
    c.add_argument("--variant", default="A")
    c.add_argument("--config", default=os.path.join(HERE, "lab-config.json"))
    c.add_argument("--out")
    c.set_defaults(fn=cmd_score)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
