# Agents SDK handoff repro

A small, self-contained lab that shows what an MCP server sees when an OpenAI Agents SDK agent hands off to another agent.

**Result.** With the documented setup (a bearer token in the MCP server's static `headers`), the MCP server received the identical token before and after a handoff in **35 of 35** two-call runs. Nothing in the credential says which agent made the call. Giving each agent its own MCP server object, with its own exchanged token, made the two calls distinguishable in all 7 runs of that variant.

| | |
| --- | --- |
| **Tested** | `openai-agents` 0.24.0, MCP Python SDK 2.3.0, Keycloak 26.8.2, on 2026-11-10 |
| **Model** | The SDK's own `agents.testing.ScriptedModel`. Every handoff and tool call is scripted, so no API key is needed. This tests what the SDK does with credentials, not whether a live model would choose the tool. |
| **Data** | One synthetic user (`alice`) in a throwaway local Keycloak. No real accounts. |
| **Severity** | Design gap, not a vulnerability. Per-server static headers are documented behavior; what this adds is measuring that a handoff leaves the credential unchanged. |

Full results, including the other variants and negative checks: [lab/teardowns/01-openai-agents-sdk/scorecard.md](lab/teardowns/01-openai-agents-sdk/scorecard.md).

## The setup

```
alice ──signs in──▶ [triage agent] ──handoff──▶ [specialist agent]
                         │                            │
                         └──── same MCP server object ┘
                                      │  Authorization: Bearer <static token>
                                      ▼
                             [MCP test server]  logs claims + a 12-char token hash
                                      │
                                      ▼
                               [canary API]     logs claims + hash
```

In each run the triage agent calls the MCP tool once, hands off, and the specialist calls it again. The MCP test server records a short hash of every token it receives (never the token itself), so the two calls can be compared.

| Variant | What goes in the `Authorization` header | Same token before and after handoff |
| --- | --- | --- |
| **A** | One client-credentials token for the app | Yes, every run |
| **A-meta** | As A, plus alice's id in MCP `_meta` via `tool_meta_resolver` | Yes, every run |
| **B** | Alice's own access token | Yes, every run |
| **C** | A custom `httpx2.Auth` doing an RFC 8693 exchange per request, on a separate MCP server object per agent | No, every run |

The per-run hashes are in [evidence/claims.json](lab/teardowns/01-openai-agents-sdk/evidence/claims.json) (claims and hashes only, no tokens).

## What this does not show

- It does not test a live model.
- `tool_meta_resolver` *can* see the active agent: its `run_context` is a `ToolContext` with an `agent` field, so an app could add the agent's name to `_meta`. That label would be unsigned and app-supplied, and it was not tested here. See the correction note in the scorecard.
- Variant C needs Keycloak's delegation preview and still hits Keycloak's one-actor limit at the next hop ([keycloak#53040](https://github.com/keycloak/keycloak/issues/53040)).

## Run it yourself

Needs Docker (or Java 21), Python 3.11+, and about an hour the first time. Step-by-step instructions: [lab/README.md](lab/README.md). The short version:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r lab/requirements.txt
# start Keycloak with the delegation preview, build the realm, start the recorder and MCP server (see lab/README.md)
python3 lab/teardowns/01-openai-agents-sdk/run_teardown.py --runs 3
```

## Rules

No tokens, client secrets or passwords are ever written to the repo. The recorder stores claim values and a 12-character hash only, and run outputs go to `runs/`, which is git-ignored.
