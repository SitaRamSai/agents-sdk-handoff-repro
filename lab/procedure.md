# Teardown procedure (E3)

The same steps for every stack, so results compare and the paper's method is already written. Status: **prepared, not run** (Nov 10, 2026).

## 1. Before you start a stack (15 min)
1. Copy [teardowns/_template/scorecard.md](teardowns/_template/scorecard.md) into the stack's folder.
2. Record the exact version tested (package version or commit hash) and the date.
3. Read the stack's own auth docs and write down its **documented secure setup**. You configure it exactly that way. Failures should come from defaults and docs, not from our setup.
4. Write your **expectations** for P1 to P5 in the scorecard before running anything.
5. Start a line in an hours log.

## 2. Scenarios

Each canary task asks the stack to fetch `canary-<run_id>` for alice. The unique run id ties the last call back to alice's request.

| ID | Scenario | Hops | What it isolates |
| --- | --- | --- | --- |
| S1 | Agent calls the MCP tool directly | 2 (user → agent → tool) | Baseline |
| S2 | Agent hands off to a sub-agent, which calls the tool | 3 | Does the handoff keep the user and the chain? |
| S3 | As S2, and the MCP server calls the canary API | 4 | Does the tool server pass on, exchange or drop the user? |
| S4 | S2 started on a schedule, with alice signed out | 3 | Lifetime, and who acts when no user is present |
| S5 | One agent calls two tools needing different scopes (`files:read`, `files:write`) | 2, fan-out | Is each tool's credential narrowed to what it needs? |

**Negative checks** (run once each, after S1 to S3 pass or partly pass):

| ID | Check | A good stack… |
| --- | --- | --- |
| N1 | Replay the last hop's token 10 minutes after the task ends | rejects it |
| N2 | Send hop 2's token straight to the canary API (wrong audience) | rejects it |
| N3 | Sign alice out (end her session) mid-task | stops further calls on her behalf |
| N4 | Ask the agent to do something needing `admin`, which alice doesn't have | refuses or gets a denied token, never a wider one |

Run S1 to S5 **three times each**. Note any run that behaves differently.

## 3. What to capture per run
- The recorder JSONL from every hop you control (MCP test server, canary API). The recorder logs: issuer, `sub`, `azp` or `client_id`, `aud`, `scope`, `iat`, `exp`, `jti`, the full `act` and `may_act` claims, whether the token is sender-bound (`cnf`), any user-like headers or MCP `_meta` fields, and a 12-character hash of the token so reuse across hops shows up.
- Task start and end times (from the agent app's own log).
- The stack's own logs or audit output for the run, if it has any (needed to score P2 and P5 as partial).
- The config used, with secrets removed.

Raw tokens are never written to disk. If you need to look at one, decode it in the terminal and move on.

## 4. Scoring rules

Score each question per scenario as **Pass**, **Partial** or **Fail**. The stack's overall score for a question is its worst score across S1 to S5.

| Question | Pass | Partial | Fail |
| --- | --- | --- | --- |
| **P1 Who** | The final hop's signed credential identifies alice by a stable ID (`sub`, or `sub` inside a token-exchange result) | Alice appears only in something unsigned (a header, an MCP `_meta` field, the prompt) or only in the stack's logs | No trace of alice at the final hop; only a service or app identity |
| **P2 Chain** | Every actor between alice and the final hop appears, in order, in the signed credential (for example nested `act`) | Some actors appear, or the full chain exists only in the platform's logs | No intermediate actor recorded anywhere |
| **P3 Narrowing** | At every hop, scope and audience are a subset of the hop before, and audience names the next hop | Authority stays flat (same scope at each hop), with no widening and no token reused at a different audience | Any hop gains scope or audience, or one token is accepted at more than one hop (same hash at two hops = reuse) |
| **P4 Lifetime** | Every delegated credential expires within 5 minutes of the task ending, or is revoked at task end and N1 confirms it | Expires within 1 hour of task end | Valid more than 1 hour after the task, a refresh token is held with no task binding, or S4 runs on a standing credential |
| **P5 Proof** | A third party can check the chain from signed artifacts and the issuer's public keys alone, without the platform's database | Part of the chain is signed, or the platform keeps a tamper-evident log | Only ordinary logs the platform could edit |

Lifetime number to report: **minutes valid after task end** = `exp` of the longest-lived delegated credential minus task end time. Median across runs.

## 5. Is a finding new? (the novelty check)

For every Partial or Fail:
1. Search the project's docs for it.
2. Search its issue tracker and discussions (open and closed) for the same behaviour.
3. Mark it **Documented** (with the link) or **Undocumented**.

Only Undocumented findings count towards the E3 pass bar. A documented gap is still useful for the paper, but it is not evidence of an edge.

## 6. Write-up per stack (30 min)
Fill in the scorecard: five scores with one line of evidence each, the novelty column, the lifetime number, the hours, and three sentences on what surprised you. Copy the claim summaries that prove each score (never tokens) into the stack's `evidence/` folder.

## 7. Disclosure
- A **design gap** the docs already admit (for example "you must pass the user yourself") can be written up after telling the maintainers.
- Anything that looks like a **vulnerability** (a token accepted where it shouldn't be, authority widened, a revoked token still working) goes to the project's published security channel (its `SECURITY.md` or bug bounty) first. Wait for a reply or 90 days before publishing details.
- Keycloak findings go to Keycloak the same way.
- Keep a dated line in the scorecard for each disclosure: who was told, how, and when.

## 8. Timing
Log every working block in an hours log with the task (build, teardown 1, teardown 2, write-up). The E3 bar is about teardown 2: if the harness is reusable, it should take 6 hours or less.
