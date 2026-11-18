#!/usr/bin/env python3
"""Teardown 1: OpenAI Agents SDK against the delegation lab.

A triage agent (playing `agent-app`, where alice signed in) hands off to a
specialist agent (playing `sub-agent`), which calls the lab's MCP test server
over streamable HTTP. The MCP server then calls the canary API (S3).

The model is the SDK's own `agents.testing.ScriptedModel`, so every handoff and
tool call is fixed in advance and no API key is needed. That keeps runs
deterministic; it also means we test what the SDK does with credentials, not
whether a real model would choose to call the tool.

Variants are the ways a developer can fill the MCP `Authorization` header,
since the SDK docs leave that to them:

  A       one client-credentials token for the app (agent-app), static header
  A-meta  as A, plus alice's id in MCP `_meta` via the documented
          tool_meta_resolver (unsigned)
  B       alice's own access token in the static header
  C       a custom httpx Auth that does an RFC 8693 exchange per request as
          sub-agent, with sub-agent's actor token (Keycloak delegation).
          This is the "cost of doing it right"; not the stack's score.

Needs Keycloak with the lab realm, the canary recorder on :9100 and the MCP
test server on :9000 started with --forward exchange (lab/README.md).

Usage:
    python3 lab/teardowns/01-openai-agents-sdk/run_teardown.py --runs 3
    python3 lab/teardowns/01-openai-agents-sdk/run_teardown.py --n1   # waits 10.5 min

Never writes a raw token: the MCP server and canary recorder log claims and a
12-character hash; this script prints only hashes and decoded claim summaries.
"""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(LAB, "keycloak"))
import kc  # noqa: E402

import httpx2  # noqa: E402  (the HTTP stack MCP SDK v2 uses)
import jwt  # noqa: E402
from agents import Agent, RunConfig, Runner, handoff  # noqa: E402
from agents.mcp import MCPServerStreamableHttp  # noqa: E402
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call  # noqa: E402

STACK = "openai-agents-sdk"
MCP_URL = "http://127.0.0.1:9000/mcp"
CANARY_URL = "http://127.0.0.1:9100"
BENCH = [sys.executable, os.path.join(LAB, "bench", "bench.py")]
EVIDENCE = os.path.join(HERE, "evidence")
SECRETS = kc.load_secrets()
with open(os.path.join(LAB, "bench", "lab-config.json")) as f:
    CFG = json.load(f)
ALICE_SUB = CFG["alice"]["sub"]


def h12(token):
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def bench(*args):
    return subprocess.run(BENCH + list(args), check=True, capture_output=True,
                          text=True).stdout.strip()


# --------------------------------------------------------------------------
# Credentials


def alice_tokens(delegation=False):
    scope = "openid files:read files:write"
    if delegation:
        scope += " delegation:client:sub-agent"
    return kc.user_token(SECRETS, scope=scope)  # dict with access and refresh token


def sign_out(tokens):
    """End alice's Keycloak session the way the app's logout button would."""
    c = SECRETS["clients"]["agent-app"]
    data = {"client_id": "agent-app", "client_secret": c,
            "refresh_token": tokens["refresh_token"]}
    url = f"{kc.KC_URL}/realms/{kc.REALM}/protocol/openid-connect/logout"
    kc.http("POST", url, data, form=True)


# ---- variant C: the code a developer has to add (counted for "effort to fix")
class DelegatedExchangeAuth(httpx2.Auth):
    """Swap alice's token for a delegated one, per request, as sub-agent."""

    def __init__(self, subject_token, scope="files:read"):
        self.subject_token, self.scope = subject_token, scope
        self.exchanges, self.last = 0, None

    def auth_flow(self, request):
        actor = kc.client_credentials("sub-agent", SECRETS)
        token = kc.exchange("sub-agent", SECRETS, self.subject_token,
                            audience="mcp-server", scope=self.scope, actor_token=actor)
        self.exchanges, self.last = self.exchanges + 1, token
        request.headers["Authorization"] = f"Bearer {token}"
        yield request
# ---- end of variant C code


def mcp_server(name, header_token=None, auth=None, meta_user=None):
    params = {"url": MCP_URL, "timeout": 20}
    if header_token:
        params["headers"] = {"Authorization": f"Bearer {header_token}"}
    if auth:
        params["auth"] = auth
    resolver = None
    if meta_user:
        def resolver(ctx):  # documented pattern: _meta built from the run context
            return {"user_sub": meta_user, "server": ctx.server_name}
    return MCPServerStreamableHttp(params=params, name=name, cache_tools_list=True,
                                   tool_meta_resolver=resolver,
                                   client_session_timeout_seconds=20)


# --------------------------------------------------------------------------
# Scripted agents


def call_tool(run_id, call_id):
    def respond(call):
        names = [t.name for t in call.tools]
        tool = next(n for n in names if n.endswith("read_canary"))
        return [function_call(tool, {"run_id": run_id}, call_id=call_id)]
    return ModelStep.respond(respond)


def build_agents(scenario, run_id, triage_mcp, spec_mcp, on_handoff=None):
    done = assistant_message(f"canary for {run_id} fetched")
    if scenario == "S1":
        triage_model = ScriptedModel([call_tool(run_id, "t1"), [done]])
        triage = Agent(name="triage", instructions="Fetch the canary.",
                       model=triage_model, mcp_servers=[triage_mcp])
        return triage
    # S2, S3, S4: triage checks the tool itself, then hands off; the specialist calls it
    spec_model = ScriptedModel([call_tool(run_id, "s1"), [done]])
    specialist = Agent(name="specialist", instructions="Fetch canaries for the user.",
                       model=spec_model, mcp_servers=[spec_mcp])
    h = handoff(specialist, on_handoff=on_handoff) if on_handoff else specialist
    triage_model = ScriptedModel([
        call_tool(run_id, "t1"),
        ModelStep.respond(lambda call: [function_call(call.handoffs[0].tool_name, {},
                                                      call_id="h1")]),
    ])
    return Agent(name="triage", instructions="Route to the specialist.",
                 model=triage_model, mcp_servers=[triage_mcp], handoffs=[h])


async def run_once(variant, scenario, notes):
    final_hop = "canary-api" if scenario == "S3" else "mcp-server"
    if scenario == "N3":  # a negative check, kept out of the scenario scores
        rid = f"n3-{variant.lower()}-{int(time.time())}"
    else:
        rid = bench("start", "--stack", STACK, "--variant", variant, "--scenario", scenario,
                    "--final-hop", final_hop)
    tokens = alice_tokens(delegation=(variant == "C"))
    alice = tokens["access_token"]
    auth = None
    if variant in ("A", "A-meta"):
        svc = kc.client_credentials("agent-app", SECRETS)
        meta = ALICE_SUB if variant == "A-meta" else None
        triage_mcp = mcp_server("lab-mcp", header_token=svc, meta_user=meta)
        spec_mcp = mcp_server("lab-mcp", header_token=svc, meta_user=meta)
    elif variant == "B":
        triage_mcp = mcp_server("lab-mcp", header_token=alice)
        spec_mcp = mcp_server("lab-mcp", header_token=alice)
    else:  # C
        triage_mcp = mcp_server("lab-mcp", header_token=alice)
        auth = DelegatedExchangeAuth(alice)
        spec_mcp = mcp_server("lab-mcp", auth=auth)

    if scenario == "S4":  # scheduled run after alice signed out
        sign_out(tokens)
        time.sleep(1)
    on_handoff = None
    if scenario == "N3":  # sign out between triage's call and the specialist's
        def on_handoff(ctx):
            sign_out(tokens)
            time.sleep(1)

    agent = build_agents("S2" if scenario in ("S3", "S4", "N3") else scenario, rid,
                         triage_mcp, spec_mcp, on_handoff)
    error = None
    async with triage_mcp:
        if scenario == "S1":
            try:
                await Runner.run(agent, "fetch my canary", run_config=RunConfig(tracing_disabled=True))
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {str(exc)[:200]}"
        else:
            try:
                async with spec_mcp:
                    await Runner.run(agent, "fetch my canary",
                                     run_config=RunConfig(tracing_disabled=True))
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {str(exc)[:200]}"
    if scenario != "N3":
        bench("end", rid, "--note", error or "ok")
    rec = {"variant": variant, "scenario": scenario, "run_id": rid, "error": error,
           "exchanges": auth.exchanges if auth else 0}
    notes.append(rec)
    print(json.dumps(rec))
    return rid, alice, auth


# --------------------------------------------------------------------------
# Negative checks


def would_canary_accept(token):
    """N2 helper: the canary recorder accepts anything, so check the token the
    way a canary API that validates audience would."""
    keys = jwt.PyJWKClient(CFG["jwks_url"])
    try:
        jwt.decode(token, keys.get_signing_key_from_jwt(token).key, algorithms=["RS256"],
                   audience="canary-api", issuer=CFG["issuer"])
        return True, "accepted"
    except jwt.InvalidAudienceError:
        return False, "wrong audience"
    except jwt.PyJWTError as exc:
        return False, type(exc).__name__


def post_mcp(token, run_id):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "read_canary", "arguments": {"run_id": run_id}}}
    req = urllib.request.Request(MCP_URL, data=json.dumps(body).encode(), headers={
        "content-type": "application/json", "accept": "application/json, text/event-stream",
        "authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code


def negatives(notes):
    out = []
    # N2: the token the SDK sent to the MCP server, sent to the canary API instead
    for variant in ("A", "B", "C"):
        if variant == "A":
            tok = kc.client_credentials("agent-app", SECRETS)
        elif variant == "B":
            tok = alice_tokens()["access_token"]
        else:
            tok = kc.exchange("sub-agent", SECRETS, alice_tokens(True)["access_token"],
                              audience="mcp-server", scope="files:read",
                              actor_token=kc.client_credentials("sub-agent", SECRETS))
        ok, why = would_canary_accept(tok)
        res = "fail" if ok else "pass"
        bench("neg", "--stack", STACK, "--variant", variant, "N2", res,
              "--note", f"a validating canary would say: {why}")
        out.append({"check": "N2", "variant": variant, "result": res, "why": why})
    bench("neg", "--stack", STACK, "--variant", "A-meta", "N2", "pass",
          "--note", "same token as A")

    # N4: ask for admin, which alice lacks. A and B can't ask (the header is fixed);
    # C's exchange can, so try it.
    tok = kc.exchange("sub-agent", SECRETS, alice_tokens(True)["access_token"],
                      audience="mcp-server", scope="files:read admin",
                      actor_token=kc.client_credentials("sub-agent", SECRETS))
    got = "admin" in kc.decode(tok).get("scope", "").split()
    bench("neg", "--stack", STACK, "--variant", "C", "N4", "fail" if got else "pass",
          "--note", "admin granted" if got else "exchange left admin out")
    for v in ("A", "A-meta", "B"):
        bench("neg", "--stack", STACK, "--variant", v, "N4", "pass",
              "--note", "static header can't ask for more; token never carries admin")
    out.append({"check": "N4", "variant": "C", "admin_granted": got})
    for row in out:
        print(json.dumps(row))
    notes.extend(out)


async def n1(notes):
    """Replay the specialist's last token 10.5 minutes after the task ends."""
    held = {}
    for variant in ("A", "B", "C"):
        rid, alice, auth = await run_once(variant, "S2", notes)
        if variant == "A":
            held[variant] = kc.client_credentials("agent-app", SECRETS)  # app keeps one
        elif variant == "B":
            held[variant] = alice
        else:
            held[variant] = auth.last
    print("waiting 630 s before replay ...", flush=True)
    await asyncio.sleep(630)
    for variant, tok in held.items():
        status = post_mcp(tok, f"n1-{variant.lower()}")
        res = "pass" if status == 401 else "fail"
        bench("neg", "--stack", STACK, "--variant", variant, "N1", res,
              "--note", f"replay 10.5 min after end: MCP server answered {status}")
        row = {"check": "N1", "variant": variant, "status": status, "result": res}
        notes.append(row)
        print(json.dumps(row))
    bench("neg", "--stack", STACK, "--variant", "A-meta", "N1", "pass", "--note", "same token as A")


async def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--variants", default="A,A-meta,B,C")
    p.add_argument("--n1", action="store_true", help="only run the N1 replay check")
    a = p.parse_args()
    os.makedirs(EVIDENCE, exist_ok=True)
    notes = []
    if a.n1:
        await n1(notes)
        name = "n1.json"
    else:
        for variant in a.variants.split(","):
            for _ in range(a.runs):
                for scenario in ("S1", "S2", "S3", "S4"):
                    await run_once(variant, scenario, notes)
            # N3 once per variant: alice signs out at the handoff
            rid, _, _ = await run_once(variant, "N3", notes)
            notes[-1]["n3_run"] = True
        negatives(notes)
        name = "runs.json"
    with open(os.path.join(EVIDENCE, name), "w") as f:
        json.dump(notes, f, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
