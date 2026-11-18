# Lab kit

Everything runs locally: Keycloak as the identity provider, a small MCP test server, and a canary API, each logging the claims of every token it receives (never the token itself). Only a synthetic user and synthetic data.

```
 test user "alice"                   lab realm in Keycloak (identity provider)
      │  signs in                         issues and exchanges tokens (RFC 8693)
      ▼
 [agent app] ──handoff──▶ [sub-agent] ──MCP call──▶ [MCP test server] ──API call──▶ [canary API]
   hop 1                    hop 2                     hop 3 recorder               hop 4 recorder
```

| Component | What it is |
| --- | --- |
| **Keycloak** | Identity provider for realm `lab`. Delegation (`actor_token`, `act`, `may_act`) is a preview feature, turned on below. Built by [keycloak/setup_realm.py](keycloak/setup_realm.py), checked by [keycloak/check_exchange.py](keycloak/check_exchange.py). Observations: [keycloak/NOTES.md](keycloak/NOTES.md). |
| **Realm `lab`** | User `alice` (synthetic). Clients `agent-app`, `sub-agent`, `mcp-server`, `canary-api`. Scopes `files:read`, `files:write`, `admin`; alice gets the first two only. |
| **MCP test server** | [mcp_server/server.py](mcp_server/server.py): one tool, `read_canary(run_id)`. Validates every inbound token, records it, then calls the canary API. |
| **Canary API / recorder** | [harness/token_recorder.py](harness/token_recorder.py): logs `sub`, `act`, `aud`, `scope`, `exp` and a 12-character hash of each token to JSONL. |
| **Bench** | [bench/bench.py](bench/bench.py): starts and ends runs and scores them from the recorder logs, using the rules in [procedure.md](procedure.md). |

## Run the kit

Use two or three terminal tabs from the repo root.


**1. Install the Python parts** (once)
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r lab/requirements.txt
```

**2. Start Keycloak** with the delegation preview on. Pick a throwaway admin password; it never goes in the repo.
```bash
docker run --name lab-keycloak -p 8080:8080 \
  -e KC_BOOTSTRAP_ADMIN_USERNAME=admin -e KC_BOOTSTRAP_ADMIN_PASSWORD=<local-only> \
  quay.io/keycloak/keycloak:26.8.2 start-dev \
  --features=token-exchange-delegation,parameterized-scopes,admin-fine-grained-authz:v2
```
No Docker? The Keycloak 26.8.2 tarball runs the same way with Java 21: `bin/kc.sh start-dev --features=...`.

**3. Build the realm** (users, clients, scopes, exchange and delegation settings)
```bash
export KC_ADMIN_PASSWORD=<local-only>
python3 lab/keycloak/setup_realm.py --reset
```
Writes client secrets and alice's password to `lab/keycloak/secrets/lab-clients.json` (git-ignored, mode 600) and alice's id to `lab/bench/lab-config.json` (git-ignored).

**4. Check exchange and delegation by hand**
```bash
python3 lab/keycloak/check_exchange.py
```
Prints the claims after each exchange (never the tokens). Compare with [keycloak/NOTES.md](keycloak/NOTES.md); anything different on your machine is worth a note.

**5. Start the canary API (hop 4) and the MCP test server (hop 3)**, each in its own tab
```bash
python3 lab/harness/token_recorder.py --port 9100 --hop canary-api --out runs \
  --jwks-url http://localhost:8080/realms/lab/protocol/openid-connect/certs
python3 lab/mcp_server/server.py --port 9000 --forward exchange
```
The MCP endpoint is `http://127.0.0.1:9000/mcp` (streamable HTTP, one tool: `read_canary(run_id)`). `--forward` picks what the server sends to the canary API: `service` (its own identity), `passthrough` (the inbound token, reused), `exchange` (RFC 8693 for `canary-api`, scope `files:read`) or `delegate` (exchange with its own actor token). `--mode record` accepts every token and only logs; the default `strict` rejects bad tokens with 401, as a real server should.

**6. Smoke test the whole kit** (no agent stack needed)
```bash
python3 lab/bench/smoke_test.py --forward exchange
```
Runs S1 to S3, N2 and N4 in code and prints a scorecard. Expect P1 Pass, P2 Partial, P3 Pass, P4 Pass, P5 Partial with `exchange`; P3 Fail with `passthrough`; P1 Fail at S3 with `service`. If that's what you see, the lab works.

## Run the Agents SDK teardown

With Keycloak, the canary API (`:9100`) and the MCP server (`:9000`, `--forward exchange`) running:

```bash
python3 lab/teardowns/01-openai-agents-sdk/run_teardown.py --runs 3
python3 lab/teardowns/01-openai-agents-sdk/run_teardown.py --n1   # replay check, waits 10.5 min
```

The script prints only token hashes and decoded claim summaries. Recorder output goes to `runs/` (git-ignored). Compare your hashes for the triage call and the specialist call in each run with [teardowns/01-openai-agents-sdk/scorecard.md](teardowns/01-openai-agents-sdk/scorecard.md).

## Rules for this folder

- Never commit tokens, client secrets, API keys or passwords. Copy only claim summaries into a teardown's `evidence/` folder.
- Synthetic data only.
- Pin versions: every teardown records the package versions it tested.
