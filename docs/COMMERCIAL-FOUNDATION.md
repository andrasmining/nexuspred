# Commercial architecture foundation — single-server first

This is an incremental execution boundary, not a SaaS launch or a distributed
trading engine. The deployment remains one Linux server, Caddy, one Fluxbridge
process and the existing SQLite database. No service, dependency, environment
variable, broker account, webhook URL or settings key needs to be replaced.

**Coverage of this first slice:** dashboard manual orders, position close and
emergency flatten pass through the new execution service. Only manual orders
use the new command ledger and `manual_trading` entitlement. Webhook, copy,
marketplace and automation execution paths retain their existing implementation.
The new entitlement is **not a workspace-wide trading suspension**.

## Boundaries

| Module | Responsibility |
|---|---|
| `app/commercial/workspaces.py` | Explicit actor/workspace identity and membership authorization. `WorkspaceId` is the existing `area_id`, not a second identity. |
| `app/commercial/entitlements.py` | Commercial permission snapshot, separate from user roles and risk configuration. |
| `app/commercial/commercial_policy.py` | Pure risk-direction policy; never authenticates a caller or bypasses broker risk locks. |
| `app/execution/contracts.py` | Versioned, serializable manual intent, account target, close command, result, errors and the `ExecutionService` port. No HTTP or broker dependencies. |
| `app/execution/service.py` | In-process implementation: authorize, claim identity, evaluate policy, resolve target, persist dispatch, call existing adapter, record outcome. |
| `app/execution/local.py` | Existing account lookup and close/flatten implementation, including the existing signal locks and tracking cleanup. |
| `app/db/execution.py` | Tenant-scoped manual command ledger and startup classification of interrupted commands. No queue consumer or automatic replay. |
| `app/db/entitlements.py` | Internal, explicitly scoped manual-trading override. No public grant-mutation endpoint. |
| HTTP routers | Request parsing, rate limiting, response compatibility and domain-error to HTTP translation. |

Existing platform roles (`user`, `broadcaster`, `admin`), feature flags, MFA,
support controls and broker risk guards are not replaced. The facade
rechecks workspace membership before execution; a global administrator does not
automatically acquire execution rights in another workspace. New command-status
reads likewise require membership; delegated support access to this new endpoint
has not been added. Existing support endpoints are unchanged.

An `Actor` must come from the trusted authentication boundary. User-supplied
`area_id`, `user_id`, `effect` and `reduce_only` fields do not establish identity
or grant a risk bypass. The legacy context is accepted only when explicitly set;
new service calls do not default to workspace 1.

## Manual-order identity and outcomes

The existing `POST /api/orders/manual` payload and successful JSON response stay
compatible. Optional `Idempotency-Key` identifies **one intended manual order**:

```http
POST /api/orders/manual
Idempotency-Key: ticket-20260913-001
Content-Type: application/json

{"lid":"login-identifier","spec":"DEMO11","symbol":"MNQZ6","action":"buy","qty":1}
```

Keep the same key and normalized payload for a retry of that instruction. Use a
new key only for a genuinely new instruction. Keys are scoped to a workspace;
reuse with a different actor or normalized intent returns HTTP 409. Prefer a
stable `lid`; `token_idx` is retained only for compatibility. Resolved account,
login, broker, environment and contract are recorded before dispatch.

A key is 1–128 ASCII characters, beginning with a letter or digit; subsequent
characters may also include `.`, `_`, `:` and `-`. Account selectors `spec` and
`lid` are bounded to 128 characters so a malformed request cannot persist an
arbitrarily large selector. Legacy numeric quantity coercion is deliberately
unchanged in this architecture change.

Without a key, every request gets a fresh server-generated command ID. Repeated
legacy requests therefore remain separate instructions, just as before. **There
is no payload/time-window deduplication and no automatic retry in the browser.**
The optional feature does not retroactively make old clients idempotent.

Successful responses add `X-Execution-Command-Id` and `X-Execution-Outcome`
headers without changing their JSON body. A replay also has
`Idempotency-Replayed: true`. Execution errors after claim carry the command ID.
`GET /api/execution/commands/{command_id}` returns a scoped diagnostic summary;
it never exposes the raw request, credentials or another workspace's record.

| Ledger state | Meaning and replay behavior |
|---|---|
| `claimed` | Identity committed; no acknowledgement recorded. A concurrent duplicate returns 409, not another adapter call. |
| `dispatching` | Target and dispatch decision committed before calling the broker. A concurrent duplicate returns 409. The actual broker outcome may already be unknown. |
| `accepted` | Adapter returned an order acknowledgement and it was persisted. Same-intent replay returns the recorded response, even if routing/settings changed afterward. **Not a guarantee of a fill or protective stop.** |
| `rejected` | Validation/policy/adapter rejection or a failure known to occur before submission. Replays never execute. Broker classification still depends on the adapter's documented contract. |
| `unknown` | Timeout, cancellation, malformed acknowledgement, unexpected post-dispatch error or interrupted startup state. Replays never execute. Reconcile with the broker first. |

The ledger enforces at-most-one service dispatch for the same **retained**
workspace/key. It is not a claim of exactly-once broker fills, protection against
a client deliberately choosing a different key, or deduplication across database
loss/restore. Adapter-internal behavior and existing non-manual paths are not
certified by this ledger.

## Failure and emergency behavior

The claim and dispatch writes use short, independent SQLite transactions with
`PRAGMA synchronous=FULL`; they do not join the asynchronous history writer's
batched/NORMAL connection. They run off the event loop and no transaction is held
while awaiting a broker. This adds bounded per-manual-order I/O, not another
server or database service. Storage durability still depends on the underlying
filesystem and hardware honoring synchronization.

A failed claim/dispatch prevents submission. A failed result write after a broker
acknowledgement is reported as uncertain, never as a safely retryable failure.
Unfinished records are classified `unknown` at process startup; neither startup
nor the status endpoint sends an order. Never run startup recovery against a
second, still-active executor. If recovery fails, it is logged; existing records
still prevent same-key replay, and manual ledger failures fail closed.

`POST /api/positions/close` and `POST /api/flatten-all` use the same existing
cancel/liquidate/tracking behavior behind the facade. They do not read the new
commercial entitlement and do not require a ledger write. Authentication,
membership, existing broker behavior and risk safeguards still apply. Existing
partial-error summaries are preserved: an aggregate `status: ok` with errors is
not proof that every account is flat. These operations are **not** advertised as
ledger-idempotent in this slice.

A manual sell or stop order can increase exposure. Arbitrary stop changes can
widen risk. The commercial policy therefore accepts a REDUCE/PROTECT bypass only
when a trusted backend has verified the reduction. The current manual-order
service always classifies its arbitrary ticket as opening potential risk; client
labels cannot bypass the restriction. Dedicated close/flatten remain available.

`manual_trading` defaults to legacy allowance when no override exists for an
existing workspace. Missing workspaces and unreadable entitlement storage do not
become grants. The internal setter is reserved for an authorized future operator
integration. Do not connect subscription cancellation to this manual-only flag:
all entry paths need coordinated policy integration before global suspension,
plan limits or billing enforcement can be offered.

## Database compatibility and rollback

The migration adds `execution_commands`, `commercial_entitlements` and one
ledger index. It does not rename columns, transform credentials, rewrite existing
settings, alter roles or renumber workspaces/accounts. New records reference
`areas` with `ON DELETE CASCADE`; the existing user/workspace deletion path removes
them with foreign-key enforcement enabled.

Schema creation is repeatable. Existing webhooks, strategy algorithms, agent
protocols and active-trade snapshots retain their current lifecycle. The ledger
is not a replacement for active-position state or the journal.

The reviewed baseline is upstream `24eeb93937f297a86ec3f184d95366abaaaefa5a`
(`5.0.0-alpha.96`). A local compatibility exercise creates populated state using
that original code, upgrades, runs the original code against the upgraded
database, then re-upgrades. It checks legacy schema/rows, authentication,
encrypted settings, webhook identity and active-trade hydration.

**Data readability on downgrade is not financial-safety compatibility.** Old code
ignores the command ledger and manual entitlement, so it cannot enforce their
new protections. A rollback is a controlled operation: pause new entries, finish
or reconcile in-flight/unknown orders, take a consistent backup, stop the process,
then change code. Do not restore an old database and replay requests, and do not
run both versions against the same live account/database. A restored snapshot
may have lost command identities or trade state created after that snapshot.

There is no automatic cleanup of command identities in this slice: expiring a key
silently would permit a delayed replay. Monitor database size; a future retention
policy should archive details while retaining appropriate idempotency tombstones.

## What can scale later, and what still needs work

This change deliberately keeps one active application process. It does **not**
make `uvicorn --workers 4`, multiple writers trading the same account, horizontal
failover or shared SQLite on network storage safe.

The port and explicit identities provide a seam for later extraction. Progress
should be evidence-driven:

1. Keep the current deployment for a small customer cohort; measure event-loop
   lag, broker latency, database size, storage errors and recovery behavior.
2. Extend the execution contract and ledger to signal and copy paths one at a
   time, preserving strategy identity, per-account fan-out results, protections,
   cancellation, partial fills and the real adapters' unknown-outcome semantics.
3. Add coordinated commercial policy only after every entry path is covered;
   keep protective management independent of billing. Add onboarding, legal
   acceptance and payments as separate platform changes.
4. Extract processes only when measured demand justifies it. A remote port needs
   authenticated workload identity, a durable transport, per-account ownership,
   ordering, backpressure and reconciliation. It is not just replacing a Python
   call with HTTP.
5. Add HA only with effective fencing of the previous executor and broker-truth
   reconciliation before new exposure. Database technology alone does not solve
   duplicate execution or split-brain ownership.

Upstream alpha.95-99 operations changes are preserved.
Configuring and validating those operations features, release governance,
broker conformance testing, customer legal
agreements, official Discord integration and production support remain separate
launch requirements. This branch neither provisions them nor changes production.

## Upstream integration

The branch integrates upstream alpha.99
(`cf74f38b0a6185ec6d266eabe7b09b0d317400bd`). All notification, announcement,
escalation and settings-history initializers coexist with the foundation tables.
Pending database restore runs before database initialization and ledger recovery.

Alpha.99 introduced `app/platform.py`. The foundation's new package was moved to
`app/commercial/` so it cannot shadow that upstream module; only foundation
imports changed. Upstream operations configuration, Telegram, canary, broadcasts
and their existing imports are preserved without wrappers or duplicate state.

Assisted support may grant configuration-write access under upstream policy.
That grant does not create workspace membership for the execution facade:
delegated manual orders, close/flatten and command-status access remain outside
this slice. The workspace owner retains those operations. No cross-workspace
execution bypass was added to resolve the merge.

Regression cases cover repeated initialization, either missing schema family,
retained rows, SQLite integrity and module/package coexistence. Existing
foundation tests retain their assertions after the import-path change.

## Validation

New deterministic tests cover concurrent/replayed keys, changed intent, unknown
outcomes, cancellation, storage failure before/after dispatch, malformed broker
replies, tenant isolation, existing roles/support restrictions, final adapter risk
locks, commercial/emergency separation, additive migrations and restart behavior.
Existing manual-order and close tests retain their assertions; only private-helper
patch locations move to the new backend.

```sh
PYTHONWARNINGS=error::DeprecationWarning python -m pytest -v -n 4
python -m pytest -q tests/test_execution_foundation.py tests/test_foundation_persistence.py
python -m compileall -q app agent run.py
```

These are mock/isolated-database checks, not broker sandbox/live certification,
load capacity measurements, or a guarantee that all pre-existing execution paths
are defect-free. Validate the affected dashboard paths with a demo account before
production rollout.

SQLite references: [synchronous pragma](https://www.sqlite.org/pragma.html#pragma_synchronous)
and [transaction behavior](https://www.sqlite.org/lang_transaction.html).
