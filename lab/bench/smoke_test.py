#!/usr/bin/env python3
"""End-to-end smoke test of the lab kit, with no agent stack involved.

Plays the part of a tiny "stack" in code so you can check that Keycloak, the
MCP test server, the canary recorder and the bench all work together before
teardown 1. Needs all three running (see lab/README.md, "Run the kit").

What it does, tagged as stack `smoke` and variant = the MCP server's
--forward mode you pass here:
  S1  agent-app sends alice's own token to the MCP tool
  S2  sub-agent exchanges alice's token (with delegation if it is on)
      and calls the tool
  S3  as S2 without delegation; the MCP server then calls the canary API
      using its --forward mode
  N2  sends a token meant for canary-api to the MCP server (should get 401)
  N4  asks Keycloak for `admin`, which alice lacks (should be left out)
Then it prints the bench score.

Usage:
    python3 lab/bench/smoke_test.py --forward exchange
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "keycloak"))
import kc  # noqa: E402

BENCH = [sys.executable, os.path.join(HERE, "bench.py")]


def mcp_call(url, token, run_id):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "read_canary", "arguments": {"run_id": run_id}}}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode()[:300]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:300]


def bench(*args):
    return subprocess.run(BENCH + list(args), check=True, capture_output=True,
                          text=True).stdout.strip()


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--forward", required=True,
                   help="the --forward mode the MCP server was started with")
    p.add_argument("--mcp-url", default="http://127.0.0.1:9000/mcp")
    p.add_argument("--runs", type=int, default=3, help="runs per scenario")
    a = p.parse_args()
    s = kc.load_secrets()
    with open(os.path.join(HERE, "lab-config.json")) as f:
        delegation = json.load(f).get("delegation_enabled")

    for _ in range(a.runs):
        rid = bench("start", "--stack", "smoke", "--variant", a.forward, "--scenario", "S1")
        alice = kc.user_token(s)["access_token"]
        print("S1", rid, *mcp_call(a.mcp_url, alice, rid))
        bench("end", rid)

        rid = bench("start", "--stack", "smoke", "--variant", a.forward, "--scenario", "S2")
        if delegation:
            subject = kc.user_token(s, scope="openid files:read files:write "
                                    "delegation:client:sub-agent")["access_token"]
            token = kc.exchange("sub-agent", s, subject, audience="mcp-server",
                                scope="files:read",
                                actor_token=kc.client_credentials("sub-agent", s))
        else:
            subject = kc.user_token(s)["access_token"]
            token = kc.exchange("sub-agent", s, subject, audience="mcp-server",
                                scope="files:read")
        print("S2", rid, *mcp_call(a.mcp_url, token, rid))
        bench("end", rid)

        rid = bench("start", "--stack", "smoke", "--variant", a.forward, "--scenario", "S3")
        subject = kc.user_token(s)["access_token"]
        token = kc.exchange("sub-agent", s, subject, audience="mcp-server", scope="files:read")
        print("S3", rid, *mcp_call(a.mcp_url, token, rid))
        bench("end", rid)

    # N2: a token for canary-api sent to the MCP server
    alice = kc.user_token(s)["access_token"]
    wrong = kc.exchange("mcp-server", s,
                        kc.exchange("sub-agent", s, alice, audience="mcp-server"),
                        audience="canary-api")
    status, _ = mcp_call(a.mcp_url, wrong, "n2-probe")
    bench("neg", "--stack", "smoke", "--variant", a.forward, "N2",
          "pass" if status == 401 else "fail", "--note", f"MCP server answered {status}")
    print("N2 wrong audience ->", status)

    # N4: ask for admin, which alice doesn't have
    claims = kc.decode(kc.user_token(s, scope="openid files:read admin")["access_token"])
    got_admin = "admin" in claims.get("scope", "").split()
    bench("neg", "--stack", "smoke", "--variant", a.forward, "N4",
          "fail" if got_admin else "pass",
          "--note", "admin granted" if got_admin else "admin left out of the token")
    print("N4 admin in token ->", got_admin)

    print(bench("score", "--stack", "smoke", "--variant", a.forward))


if __name__ == "__main__":
    main()
