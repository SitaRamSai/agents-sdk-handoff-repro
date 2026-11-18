# Teardown 1: OpenAI Agents SDK (handoff + remote MCP)

Status: **run Nov 10, 2026** in a cloud container against the lab, with the SDK's scripted test model (no API key). Results: [scorecard.md](scorecard.md). Harness: [run_teardown.py](run_teardown.py).

## What the docs say [Sourced: openai.github.io/openai-agents-python, MCP and handoffs pages, read Nov 10]
- Remote MCP auth is a bearer token in static headers, for example `"headers": {"Authorization": f"Bearer {token}"}`, or a custom `httpx` auth object the developer writes. The docs say to keep tokens in headers, not URLs.
- No OAuth flow, per-user token storage or token exchange is described for MCP.
- `tool_meta_resolver` can add per-call MCP `_meta` built from the run context (the example is a tenant ID). That is developer-coded and unsigned.
- Handoffs pass the conversation history to the next agent and stay inside one run. Handoff metadata (`input_type`) is separate from the run context. Nothing says user identity or auth travels with a handoff.

## Setup
Triage agent → handoff → specialist agent → remote MCP test server (streamable HTTP) → canary API. Model: the cheapest model that reliably calls tools.

Test three realistic ways a developer fills that `Authorization` header, because the docs leave it to them:

| Variant | What goes in the header | Why test it |
| --- | --- | --- |
| A. Service token | One client-credentials token for the app (`agent-app`), same for every user | The simplest thing that works; likely the most common |
| B. User's token forwarded | Alice's own access token from her sign-in | Keeps the user, but reuses one token at the MCP server and beyond. The MCP security guidance warns against passing tokens through [Judgment: confirm exact wording in the MCP authorization spec before citing] |
| C. Exchange per call | A custom `httpx` auth that does an RFC 8693 exchange with Keycloak for audience `mcp-server`, with the sub-agent as actor | What it takes to get it right. Record the lines of code and hours: that is the "cost of doing it right" number |

Variants A and B are scored as **what the docs lead to**. Variant C is reported as **effort to fix**, not as the stack's score.

S4 (scheduled run): the SDK has no scheduler, so start S2 from a cron job 30 minutes after alice signs out, using whatever credential the app kept. Record what it was and how long it would have stayed valid.

## Expectations (written before running) [Judgment]
| Question | Expected | Why |
| --- | --- | --- |
| P1 Who | A: Fail. B: Pass at the MCP server, then depends on what the server forwards | Nothing carries the user unless the developer adds it |
| P2 Chain | Fail in A and B | Both agents share one MCP connection config, so the server can't tell triage from specialist |
| P3 Narrowing | A: Partial (flat). B: Fail if the same token hash shows up at the canary API | The header is set once per server, not per hop |
| P4 Lifetime | Depends on Keycloak's token lifetime, not the task. Likely Partial or Fail | No task binding in the SDK |
| P5 Proof | Fail | No signed record of who handed off to whom; only the SDK's traces |

**Likely undocumented finding to check first:** whether the handoff target is distinguishable at the tool at all (P2), and whether variant B leads to one token being accepted at two audiences (P3). Run the novelty check on both.

## Time box
Target 4 h for runs and write-up after the lab build. Variant C capped at 1.5 h; if it isn't working by then, record how far it got.
