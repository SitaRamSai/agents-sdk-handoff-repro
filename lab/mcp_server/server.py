#!/usr/bin/env python3
"""MCP test server for the delegation lab (hop 3).

One tool, `read_canary(run_id)`. Every HTTP request that reaches the server is
recorded before anything else happens: the claims of the bearer token, a
12-character hash of it (never the token), any user-like headers, the MCP
method and, for tool calls, the run id. The token is then validated against
the lab realm's keys. The tool calls the canary API (hop 4) with a credential
chosen by --forward, so the teardowns can see what each choice does to the
chain:

  service   mcp-server's own client-credentials token (the user is dropped)
  passthrough  the inbound token as-is (token reuse across hops)
  exchange  RFC 8693 exchange as mcp-server for audience canary-api,
            scope narrowed to files:read
  delegate  as exchange, plus mcp-server's own actor_token (asks for act)

Log: runs/mcp-server-<date>.jsonl, same shape as lab/harness/token_recorder.py
plus run_id, mcp_method, decision and forward fields.

Usage:
    python3 lab/mcp_server/server.py --port 9000 --forward exchange
    # MCP endpoint: http://127.0.0.1:9000/mcp
"""

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(LAB, "harness"))
sys.path.insert(0, os.path.join(LAB, "keycloak"))

import jwt  # noqa: E402  PyJWT
import uvicorn  # noqa: E402
from mcp.server.mcpserver import Context, MCPServer  # noqa: E402

import kc  # noqa: E402
from token_recorder import USER_HINT_HEADERS, inspect_token  # noqa: E402

CFG = {}  # filled in main()


# --------------------------------------------------------------------------
# Validation and recording


def validate(token):
    """Return (accepted, reason). Signature, issuer, expiry and audience."""
    try:
        key = CFG["jwks"].get_signing_key_from_jwt(token).key
        jwt.decode(token, key, algorithms=["RS256", "ES256", "PS256"],
                   issuer=CFG["issuer"], audience=CFG["audience"],
                   options={"require": ["exp", "iat", "sub"]})
        return True, "valid"
    except jwt.InvalidAudienceError:
        return False, "wrong audience"
    except jwt.ExpiredSignatureError:
        return False, "expired"
    except jwt.InvalidIssuerError:
        return False, "wrong issuer"
    except jwt.PyJWTError as exc:
        return False, f"invalid: {type(exc).__name__}"


def record(entry):
    with open(CFG["log"], "a") as f:
        f.write(json.dumps(entry) + "\n")


class RecordingGate:
    """ASGI wrapper: record every request, then accept or reject it."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks, more = [], True
        while more:  # buffer the body so it can be read here and replayed
            msg = await receive()
            chunks.append(msg.get("body", b""))
            more = msg.get("more_body", False)
        body = b"".join(chunks)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}

        entry = {"ts": time.time(), "hop": "mcp-server", "method": scope["method"],
                 "path": scope["path"], "forward": CFG["forward"]}
        try:
            rpc = json.loads(body) if body else {}
        except ValueError:
            rpc = {}
        if isinstance(rpc, dict):
            entry["mcp_method"] = rpc.get("method")
            params = rpc.get("params") or {}
            if rpc.get("method") == "tools/call":
                entry["tool"] = params.get("name")
                entry["run_id"] = (params.get("arguments") or {}).get("run_id")
            if params.get("_meta"):
                entry["mcp_meta"] = params["_meta"]  # unsigned: Partial at most
        hints = {h: headers[h] for h in USER_HINT_HEADERS if h in headers}
        if hints:
            entry["user_hint_headers"] = hints

        scheme, _, token = headers.get("authorization", "").partition(" ")
        entry["auth_scheme"] = scheme or None
        token = token.strip()
        if token:
            entry.update(inspect_token(token, CFG["jwks_url"]))
            accepted, reason = validate(token)
        else:
            accepted, reason = False, "no bearer token"
        if CFG["mode"] == "record":
            entry["decision"] = f"accepted (record mode; would be: {reason})"
            accepted = True
        else:
            entry["decision"] = "accepted" if accepted else f"rejected: {reason}"
        record(entry)

        if not accepted:
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", b'Bearer error="invalid_token"')]})
            await send({"type": "http.response.body",
                        "body": json.dumps({"error": "invalid_token",
                                            "reason": reason}).encode()})
            return

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


# --------------------------------------------------------------------------
# Downstream call (hop 3 -> hop 4)


def downstream_token(inbound):
    mode, s = CFG["forward"], CFG["secrets"]
    if mode == "passthrough":
        return inbound
    if mode == "service":
        return kc.client_credentials("mcp-server", s)
    actor = kc.client_credentials("mcp-server", s) if mode == "delegate" else None
    return kc.exchange("mcp-server", s, inbound, audience="canary-api",
                       scope="files:read", actor_token=actor)


def call_canary(run_id, token):
    req = urllib.request.Request(f"{CFG['canary_url']}/canary/{run_id}",
                                 headers={"authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.read().decode()[:200]}


mcp = MCPServer(name="delegation-lab-mcp",
                instructions="Lab server. read_canary fetches the canary for a run id.")


@mcp.tool()
async def read_canary(run_id: str, ctx: Context) -> dict:
    """Fetch the canary file for a lab run. Pass the run id you were given."""
    headers = ctx.headers or {}
    inbound = headers.get("authorization", "").partition(" ")[2].strip()
    result = {"run_id": run_id, "forward": CFG["forward"]}
    try:
        token = await asyncio.to_thread(downstream_token, inbound)
    except kc.KeycloakError as exc:
        result["error"] = f"token exchange refused: {exc.body[:200]}"
        record({"ts": time.time(), "hop": "mcp-server", "run_id": run_id,
                "event": "downstream_token_failed", "forward": CFG["forward"],
                "error": exc.body[:300]})
        return result
    status, body = await asyncio.to_thread(call_canary, run_id, token)
    result.update({"canary_status": status, "canary": body})
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--forward", choices=["service", "passthrough", "exchange", "delegate"],
                   default="exchange")
    p.add_argument("--mode", choices=["strict", "record"], default="strict",
                   help="strict rejects bad tokens with 401; record accepts all and logs")
    p.add_argument("--audience", default="mcp-server")
    p.add_argument("--canary-url", default="http://127.0.0.1:9100")
    p.add_argument("--out", default="runs")
    a = p.parse_args()

    os.makedirs(a.out, exist_ok=True)
    secrets = kc.load_secrets()
    issuer = f"{secrets['kc_url']}/realms/{secrets['realm']}"
    jwks_url = f"{issuer}/protocol/openid-connect/certs"
    CFG.update(forward=a.forward, mode=a.mode, audience=a.audience,
               canary_url=a.canary_url.rstrip("/"), secrets=secrets,
               issuer=issuer, jwks_url=jwks_url, jwks=jwt.PyJWKClient(jwks_url),
               log=os.path.join(a.out, f"mcp-server-{time.strftime('%Y%m%d')}.jsonl"))

    app = RecordingGate(mcp.streamable_http_app(stateless_http=True, json_response=True))
    print(f"MCP test server on http://127.0.0.1:{a.port}/mcp  forward={a.forward} "
          f"mode={a.mode}  log={CFG['log']}")
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
