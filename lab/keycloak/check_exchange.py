#!/usr/bin/env python3
"""Check by hand that token exchange and delegation work in the lab realm.

Step 3 of the lab build (lab/README.md). Runs each exchange the teardowns rely
on and prints the claims that matter (sub, azp, aud, scope, act, may_act,
lifetime). Never prints a raw token. Results also go to
runs/keycloak-check-<date>.json (git-ignored) so they can be quoted later.

Usage:
    python3 lab/keycloak/check_exchange.py
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kc import (KeycloakError, client_credentials, decode, exchange,  # noqa: E402
                load_secrets, user_token)

FIELDS = ["sub", "azp", "aud", "scope", "act", "may_act"]


def summary(token):
    c = decode(token)
    out = {k: c[k] for k in FIELDS if k in c}
    out["lifetime_s"] = c.get("exp", 0) - c.get("iat", 0)
    return out


def main():
    s = load_secrets()
    results = []

    def step(name, fn, expect):
        try:
            res = {"check": name, "ok": True, "claims": summary(fn())}
        except KeycloakError as exc:
            res = {"check": name, "ok": False, "error": exc.body[:300]}
        res["expect"] = expect
        results.append(res)
        mark = "OK " if res["ok"] else "ERR"
        print(f"\n[{mark}] {name}\n      expect: {expect}")
        print("      " + json.dumps(res.get("claims") or res.get("error")))
        return res

    alice = user_token(s)["access_token"]
    step("1. alice signs in at agent-app", lambda: alice,
         "sub = alice; aud includes sub-agent and mcp-server; scope has files:read files:write")
    step("2. alice asks for admin (N4)",
         lambda: user_token(s, scope="openid files:read admin")["access_token"],
         "admin is silently left out (alice lacks the role)")
    t1 = step("3. sub-agent exchanges alice's token for mcp-server, no scope asked",
              lambda: exchange("sub-agent", s, alice, audience="mcp-server"),
              "aud = mcp-server, azp = sub-agent; files:* scopes dropped (only defaults)")
    step("4. same, asking scope files:read",
         lambda: exchange("sub-agent", s, alice, audience="mcp-server", scope="files:read"),
         "aud = mcp-server; scope narrowed to files:read")
    if t1["ok"]:
        hop2 = exchange("sub-agent", s, alice, audience="mcp-server", scope="files:read")
        step("5. mcp-server exchanges that for canary-api",
             lambda: exchange("mcp-server", s, hop2, audience="canary-api", scope="files:read"),
             "sub still alice; aud = canary-api; azp = mcp-server; no record of sub-agent")

    deleg = user_token(s, scope="openid files:read files:write delegation:client:sub-agent")["access_token"]
    step("6. alice signs in consenting to delegation to sub-agent", lambda: deleg,
         "may_act names sub-agent's service account")
    actor = client_credentials("sub-agent", s)
    d = step("7. sub-agent exchanges with its own actor_token",
             lambda: exchange("sub-agent", s, deleg, audience="mcp-server",
                              scope="files:read", actor_token=actor),
             "sub = alice; act names sub-agent; aud = mcp-server")
    if d["ok"]:
        delegated = exchange("sub-agent", s, deleg, audience="mcp-server",
                             scope="files:read", actor_token=actor)
        step("8. mcp-server delegates again (second hop, nested act?)",
             lambda: exchange("mcp-server", s, delegated, audience="canary-api",
                              actor_token=client_credentials("mcp-server", s)),
             "a nested act (mcp-server, then sub-agent) if Keycloak supports chains")
        step("9. mcp-server does a plain exchange of the delegated token",
             lambda: exchange("mcp-server", s, delegated, audience="canary-api"),
             "a token for canary-api, ideally keeping act")

    os.makedirs("runs", exist_ok=True)
    out = os.path.join("runs", f"keycloak-check-{time.strftime('%Y%m%d-%H%M')}.json")
    with open(out, "w") as f:
        json.dump({"ts": time.time(), "results": results}, f, indent=2)
    print(f"\nsaved claim summaries (no tokens) to {out}")


if __name__ == "__main__":
    main()
