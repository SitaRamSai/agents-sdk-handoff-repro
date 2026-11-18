# Teardown: OpenAI Agents SDK (handoff + remote MCP)

| | |
| --- | --- |
| **Version tested** | `openai-agents` 0.24.0 (PyPI), MCP Python SDK 2.3.0, Keycloak 26.8.2 |
| **Date** | 2026-11-10 |
| **Documented secure setup used** | SDK MCP docs: bearer token in the server's static `headers`, or a custom `httpx` `auth` object; `tool_meta_resolver` for per-call `_meta` ([README](README.md#what-the-docs-say-sourced-openaigithubioopenai-agents-python-mcp-and-handoffs-pages-read-nov-10)) |
| **Hours** | build 0.3 · runs 0.2 · write-up 0.3 · total 0.8 (Claude's time, cloud container) |
| **Tester** | throwaway local Keycloak, synthetic user `alice`, no real accounts |
| **Model** | The SDK's own `agents.testing.ScriptedModel`: every handoff and tool call is fixed in advance, so no API key was used. This tests what the SDK does with credentials, not whether a live model would pick the tool. |
| **Harness** | [run_teardown.py](run_teardown.py) · claim summaries (no tokens): [evidence/claims.json](evidence/claims.json), [evidence/runs.json](evidence/runs.json) |

Setup: `triage` agent (stands for `agent-app`, where alice signed in) → handoff → `specialist` agent (stands for `sub-agent`) → lab MCP test server (strict validation, `--forward exchange`) → canary API. In S2 to S4 the triage agent also calls the tool once before handing off, so the two calls can be compared.

Variants are the ways a developer can fill the MCP `Authorization` header:

| Variant | Header | Counts as |
| --- | --- | --- |
| **A** | One client-credentials token for the app, same for every user | What the docs lead to |
| **A-meta** | As A, plus alice's id in MCP `_meta` through the documented `tool_meta_resolver` | What the docs lead to |
| **B** | Alice's own access token from the sign-in | What the docs lead to |
| **C** | A custom `httpx2.Auth` that does an RFC 8693 delegated exchange per request as `sub-agent`, with its own actor token, on a separate MCP server object for the specialist | Effort to fix (15 lines plus Keycloak's delegation preview), not the stack's score |

## Expectations (written before running) [Judgment]
From the [README](README.md#expectations-written-before-running-judgment), unchanged:

| Question | Expected | Why |
| --- | --- | --- |
| P1 Who | A: Fail. B: Pass at the MCP server | Nothing carries the user unless the developer adds it |
| P2 Chain | Fail in A and B | Both agents share one MCP connection config |
| P3 Narrowing | A: Partial. B: Fail if one token shows up at two hops | Header set once per server |
| P4 Lifetime | Partial or Fail | No task binding in the SDK |
| P5 Proof | Fail | No signed record of who handed off to whom |

## Results
Bench output reviewed by hand; overall = worst across scenarios. S1 to S4 run 3 times per variant (A has 3 extra runs from setup and N1; all agreed). S5 not run: the lab MCP server has one tool, so there is no second scope to fan out to.

| Question | A | A-meta | B | C (fix) | Evidence |
| --- | --- | --- | --- | --- | --- |
| P1 Who | **Fail** | **Fail** (Partial at the MCP server, Fail at the canary API) | **Pass** | Pass S1–S2; **Fail S3** | A: `sub` is agent-app's service account at every hop. A-meta: alice only in unsigned `_meta`, which doesn't travel past the MCP server. B: signed `sub` = alice at both hops |
| P2 Chain | **Partial** | **Partial** | **Partial** | Partial S2; **Fail S3** | Signed token names only `agent-app` (`azp`); the specialist never appears. C names `sub-agent` in `act` but not `agent-app` |
| P3 Narrowing | Pass | Pass | Pass | Pass S1–S2 | Audience names each hop; scope never grows. No token reused across hops (the MCP server exchanges before calling the canary API) |
| P4 Lifetime | **Fail** (S4 reviewed) | **Fail** (S4 reviewed) | Pass | Pass | Tokens expire 5.0 min after task end (median, all variants). But in A the scheduled run (S4) works on a standing app credential with no user present, which the rules score Fail. B's 5 minutes is Keycloak's default, not anything the SDK sets |
| P5 Proof | Partial | Partial | Partial | Partial S2; **Fail S3** | Final token is signed but doesn't carry the chain; the SDK's own record is its traces |

**Minutes valid after task end (median):** 5.0 (Keycloak's access token lifetime).

**C at S3 and S4.** In C the MCP server can't pass the user on: Keycloak refuses to exchange a token that carries `act` (or alice's `may_act`) again, so the canary API is never reached. That is the identity provider's limit, already tracked upstream (see below), not the SDK's. In S4 Keycloak refused the exchange because alice's session had ended (`Invalid token`), so no call was made after sign-out: the correct outcome, reviewed as Pass for P4 rather than the bench's "never reached".

## Negative checks
| Check | A | A-meta | B | C | Evidence |
| --- | --- | --- | --- | --- | --- |
| N1 Replay 10.5 min after task end | Pass | Pass | Pass | Pass | MCP server answered 401 (expired) for each variant's last token: [evidence/n1.json](evidence/n1.json). That comes from Keycloak's 5-minute lifetime, not from the SDK |
| N2 Wrong-audience token | Pass | Pass | Pass | Pass | Every token the SDK sent names `mcp-server` and `sub-agent`, not `canary-api`; a validating canary would reject it |
| N3 Alice signs out mid-task | **Fail** | **Fail** | **Fail** | Pass | A: sign-out has no effect. B: alice's stored token kept working at the MCP server after sign-out, because it validates offline until `exp`. C: Keycloak refused the specialist's exchange |
| N4 Ask for `admin` | Pass | Pass | Pass | Pass | A and B can't ask (fixed header). C's exchange asking for `admin` got a token without it |

## What surprised me (3 sentences)
The handoff is invisible at the tool: in all 35 two-call runs of A, A-meta and B, the triage agent and the specialist reached the MCP server with the same token (same hash) and the same `_meta`. The handoff changes nothing in the credential the server receives.¹ Getting the specialist recorded took only about 15 lines (an `httpx2.Auth` that exchanges per request) plus a separate MCP server object per agent, yet it cost three Keycloak round trips per tool call and then hit Keycloak's one-actor limit at the next hop. When the MCP server returns 401, the SDK surfaces a doubly nested `ExceptionGroup` ending in "MCPError: Server returned an error response", with no status or reason, so an auth refusal is hard to tell apart from a network fault in the app's own logs (this comes from the MCP Python SDK client).

## Novelty check
| Finding | Status | Where checked |
| --- | --- | --- |
| Static headers are per server, not per user or per run (behind N3 and P1 in A) | **Documented** | SDK MCP docs; issues [#1566](https://github.com/openai/openai-agents-python/issues/1566), [#1969](https://github.com/openai/openai-agents-python/issues/1969) (both closed) |
| `_meta` is unsigned and developer-built | **Documented** | SDK MCP docs; [#2367](https://github.com/openai/openai-agents-python/issues/2367) |
| Handoff target indistinguishable at the MCP server by its credential | **Undocumented** (provisional) | Not on the MCP or handoffs docs pages read Nov 10; issue searches for agent identity, handoff auth, per-agent `_meta` and current agent (Nov 10) found per-user, per-run and per-tool requests ([#1566](https://github.com/openai/openai-agents-python/issues/1566), [#1969](https://github.com/openai/openai-agents-python/issues/1969), [#2868](https://github.com/openai/openai-agents-python/issues/2868)) but nothing on which agent made a call. See the correction below on the metadata hook |
| A delegated token can't be exchanged again (C at S3) | **Documented** (Keycloak) | [keycloak#53040](https://github.com/keycloak/keycloak/issues/53040) "Support multi-hop delegation chaining", open since Sep 22, 2026, describes exactly this |

## Undocumented findings
1. **The MCP server can't tell from the credential which agent called it.** With the documented static-header setup, a call made before a handoff and a call made after it carry byte-for-byte the same credential at the MCP server (35 of 35 two-call runs across A, A-meta and B). In variant C, one MCP server object per agent with its own exchanged token, the two calls differed in all 7 two-call runs. Severity: design gap, not a vulnerability. Disclosure: a normal issue on the SDK repo once the write-up is reviewed.

¹ **Correction (Nov 18, 2026).** An earlier version of this scorecard said the `tool_meta_resolver` hook isn't told which agent is calling. That was wrong. In 0.24.0 the hook's `run_context` is a `ToolContext` built with `agent=` the active agent (`agents/run_internal/tool_execution.py`, `ToolContext.from_agent_context`), so a resolver can read `run_context.agent.name` and put it in `_meta`. That label would be unsigned and set by the app, so it doesn't change the credential result above, and it was not tested here.

## Disclosure log
| Date | Told | Channel | Reply |
| --- | --- | --- | --- |
| — | nobody yet | — | — |
