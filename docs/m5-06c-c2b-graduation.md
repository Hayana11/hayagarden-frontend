# M5-06C-C2B Graduation

- Production SHA: `78320de8bb83069bb5704389147373f4b8fb0535`
- Provider: GitHub Remote MCP
- Endpoint: `https://api.githubcopilot.com/mcp/readonly`
- Transport: `streamable_http`
- Graduated tool: `list_branches`
- Target: `Hayana11/Elpis`
- REST validation: `GET /user = 200`; Elpis branches `= 200`, result count `0`

## Milestone progression

C2A authenticated discovery succeeded.

C2B reviewed one read-only tool, approved it, classified it as
`none / autonomous`, passed the execution fence, materialized bearer
authentication only inside the runtime, and completed one real Remote MCP
tool invocation successfully.

The three-canary progression was:

1. **001** — exposed the original observability gap.
2. **002** — after O1, exposed the safe inner reason `AUTH_REQUIRED`.
3. **Credential investigation** — the old PAT was invalid.
4. **Credential rotation** — the replacement PAT passed GitHub REST
   `/user = 200` and Elpis branches `= 200`.
5. **003** — the real MCP invocation returned `SUCCEEDED`.

The first two failures remain immutable historical evidence. They must not be
rewritten or deleted merely to make the graduation history look clean.

## Regression contract

Existing M5 tests cover the graduation invariants: safe `NOT_INVOKED`
reason observability, non-persistence of unsafe summaries, terminal
`OUTCOME_UNKNOWN` behavior, duplicate exactly-once execution, fail-closed
authority checks, child argv/environment secret isolation, successful terminal
mapping and audit ordering, and `NONE + AUTONOMOUS` fence semantics.

This closeout adds no production-state test and no new regression test. Future
counts are expected to grow after graduation; the snapshot is historical
evidence, not a permanent count assertion.

See
[`artifacts/external-mcp/m5-06c-c2b-graduation.json`](../artifacts/external-mcp/m5-06c-c2b-graduation.json)
for the machine-readable snapshot.
