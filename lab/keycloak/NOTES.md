# Keycloak notes: first data points (Nov 10, 2026)

Taken while building and testing the lab kit, before any teardown. Keycloak **26.8.2**, dev mode, started with `--features=token-exchange-delegation,parameterized-scopes,admin-fine-grained-authz:v2`, realm built by [setup_realm.py](setup_realm.py), checks run by [check_exchange.py](check_exchange.py). Run in a throwaway cloud container, not yet on the maintainer's Mac; re-run `check_exchange.py` there to confirm.

These are lab notes, not findings yet. Each needs the novelty check in [procedure.md §5](../procedure.md#5-is-a-finding-new-the-novelty-check) before it counts.

| # | What happened | Label | Bears on |
| --- | --- | --- | --- |
| 1 | Plain exchange (sub-agent → audience `mcp-server`) keeps alice as `sub`, sets `azp` to the requester and narrows `aud` to the one client asked for | [Measured] | P1, P3 |
| 2 | Plain exchange **without** a `scope` parameter drops alice's `files:*` scopes (only the requester's default scopes remain). Asking `scope=files:read` narrows correctly | [Measured] | P3 |
| 3 | A second plain exchange (mcp-server → `canary-api`) keeps alice but leaves **no trace of sub-agent**: the chain is lost without delegation | [Measured] | P2, P5 |
| 4 | Asking for `admin`, which alice lacks, is silently left out of the token rather than refused | [Measured] | N4 |
| 5 | One-hop delegation works: after alice consents to `delegation:client:sub-agent`, sub-agent's exchange with its own `actor_token` gives `act: {sub: <sub-agent service account>, client_id: sub-agent}` | [Measured] | P2, P5 |
| 6 | **A token that carries `act` can't be exchanged again.** Delegating again (mcp-server with its own actor token) fails with `Invalid may_act claim in the subject_token`; a plain exchange fails with `Subject token with a delegation claim is not allowed for standard token exchange`. So in this version a signed chain stops at one actor: a 3-hop path can't produce nested `act` | [Measured] | P2, P5, S3 |
| 7 | Delegation needs a consent screen on every sign-in (consent is never stored), and the password grant is refused for a client that requires consent. So a scheduled run with no user present (S4) can't get a delegated token | [Sourced: Keycloak token exchange docs; measured: password grant refused] | P4, S4 |
| 8 | Default access token lifetime is 5 minutes; exchanged tokens don't outlive the subject token unless a client switch allows it | [Measured lifetime; Sourced switch] | P4 |

**Docs check so far.** The docs say a subject token containing `act` is rejected unless an `actor_token` is sent, which covers part of note 6. They don't say whether a delegated token can itself be delegated again, or show a nested `act`.

**Novelty check (Nov 10): note 6 is Documented.** Keycloak issue [#53040](https://github.com/keycloak/keycloak/issues/53040), "Support multi-hop delegation chaining" (open since Sep 22, 2026), describes the same failure ("Invalid may_act claim in the subject_token") with the same example path (user → agent client → MCP server client → downstream service) and proposes opt-in chaining with a maximum depth. The second error in note 6 is the fix for [#52691](https://github.com/keycloak/keycloak/issues/52691) (closed Sep 16, 2026), which stopped delegation tokens from slipping through standard exchange. Related open issues: [#51633](https://github.com/keycloak/keycloak/issues/51633) (a revoked chain can still be extended) and [#53996](https://github.com/keycloak/keycloak/issues/53996) (the actor subject is the mutable service-account user id, which is what `act.sub` showed here). So note 6 is good context for the paper, not an E3 finding.

**Seen in teardown 1.** A user token that carries `may_act` (alice consented to delegation) can't go through a plain exchange either, so an MCP server in `exchange` mode can't forward it. After alice signs out, Keycloak refuses to exchange alice's still-unexpired access token (`Invalid token`), while a resource server that validates offline keeps accepting it until `exp`.

**What it means for the teardowns [Judgment].** In this lab, the best any stack can do at the canary API (S3) is either keep alice and lose the chain (plain exchange), or keep one actor and stop there. Expect P2 Partial at best for S3 on every stack, and say in the write-up that it's an identity provider limit, not the stack's fault.
