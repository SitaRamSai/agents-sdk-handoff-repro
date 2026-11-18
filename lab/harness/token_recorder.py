#!/usr/bin/env python3
"""Token recorder for the delegation lab.

Runs a tiny HTTP endpoint that answers every request with a canary body and
logs, as one JSON line per request, the claims of the credential it received.
It never writes the raw token: only a 12-character SHA-256 prefix, so the same
token showing up at two hops can be spotted (P3 reuse check).

Standard library only. If PyJWT (with cryptography) is installed and --jwks-url
is given, signatures are verified too; otherwise claims are logged as
"unverified".

Usage:
    python3 token_recorder.py --port 9100 --hop canary-api --out runs/
"""

import argparse
import base64
import hashlib
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLAIMS = ["iss", "sub", "azp", "client_id", "aud", "scope", "scp", "iat",
          "nbf", "exp", "jti", "act", "may_act", "cnf", "sid"]
USER_HINT_HEADERS = ["x-user", "x-user-id", "x-forwarded-user",
                     "x-on-behalf-of", "x-actor", "x-request-id"]


def b64url_json(part):
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part.encode()))


def act_chain(act):
    """Flatten nested act claims into an ordered list of actor subjects."""
    chain = []
    while isinstance(act, dict):
        chain.append(act.get("sub") or act.get("client_id") or "?")
        act = act.get("act")
    return chain


def inspect_token(token, jwks_url=None):
    rec = {"token_hash": hashlib.sha256(token.encode()).hexdigest()[:12]}
    parts = token.split(".")
    if len(parts) != 3:
        rec["format"] = "opaque"
        return rec
    try:
        header, payload = b64url_json(parts[0]), b64url_json(parts[1])
    except ValueError:
        rec["format"] = "malformed"
        return rec
    rec["format"] = "jwt"
    rec["alg"], rec["kid"] = header.get("alg"), header.get("kid")
    rec["claims"] = {k: payload[k] for k in CLAIMS if k in payload}
    rec["actor_chain"] = act_chain(payload.get("act"))
    if "exp" in payload:
        rec["expires_in_s"] = int(payload["exp"] - time.time())
    rec["signature"] = verify(token, jwks_url) if jwks_url else "unverified"
    return rec


def verify(token, jwks_url):
    try:
        import jwt  # PyJWT
        key = jwt.PyJWKClient(jwks_url).get_signing_key_from_jwt(token).key
        jwt.decode(token, key, algorithms=["RS256", "ES256", "PS256"],
                   options={"verify_aud": False})
        return "valid"
    except ImportError:
        return "unverified (PyJWT not installed)"
    except Exception as exc:  # report, never crash the lab
        return f"invalid: {type(exc).__name__}"


class Recorder(BaseHTTPRequestHandler):
    hop, out_file, jwks_url = "hop", None, None

    def _handle(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        entry = {"ts": time.time(), "hop": self.hop, "method": self.command,
                 "path": self.path}
        auth = self.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        entry["auth_scheme"] = scheme or None
        if token:
            entry.update(inspect_token(token.strip(), self.jwks_url))
        hints = {h: self.headers[h] for h in USER_HINT_HEADERS if h in self.headers}
        if hints:
            entry["user_hint_headers"] = hints  # unsigned: scores Partial at most
        try:  # MCP JSON-RPC calls may carry _meta in params
            meta = json.loads(body).get("params", {}).get("_meta")
            if meta:
                entry["mcp_meta"] = meta
        except (ValueError, AttributeError):
            pass
        with open(self.out_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
        reply = json.dumps({"canary": self.path.rsplit("/", 1)[-1], "hop": self.hop})
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(reply.encode())

    do_GET = do_POST = _handle

    def log_message(self, *args):
        pass


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--port", type=int, default=9100)
    p.add_argument("--hop", default="canary-api")
    p.add_argument("--out", default="runs")
    p.add_argument("--jwks-url", default=None)
    a = p.parse_args()
    os.makedirs(a.out, exist_ok=True)
    Recorder.hop, Recorder.jwks_url = a.hop, a.jwks_url
    Recorder.out_file = os.path.join(a.out, f"{a.hop}-{time.strftime('%Y%m%d')}.jsonl")
    print(f"recording {a.hop} on 127.0.0.1:{a.port} -> {Recorder.out_file}")
    ThreadingHTTPServer(("127.0.0.1", a.port), Recorder).serve_forever()


if __name__ == "__main__":
    main()
