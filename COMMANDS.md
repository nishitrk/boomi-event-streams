# Command reference

Every command and flag, generated from the CLIs themselves and verified against
`--help`. Eight skills, 16 scripts, 26 distinct operations. All output is tabular.

In Claude you rarely type these — ask in plain language and the right skill loads.
The reference is for when you want to run something directly, or check what a skill
is about to do.

**Prefix for every command:** `python3 "$CLAUDE_PLUGIN_ROOT/scripts/…"` — set when
installed as a plugin. Run from the directory holding your `.env`.

---

## Setup — run once per machine

### `es-env-setup` — the skill

Shows a form, validates, **verifies against Boomi, and only then saves** — to the most
durable location available, which it names. Every future conversation finds it from any
directory.

Where it saves depends on the host: a connected folder is preferred over a home
directory, because some environments run with an ephemeral home where credentials
would not survive the session. Setup warns you if only such a location exists.
`BOOMI_ES_CONFIG` sets it explicitly.

### `es_setup.py` — the same thing from a terminal

| Flag | Effect |
|---|---|
| `--check` | Where credentials resolve from, and whether they are usable. |
| `--test` | Verify against Boomi; lists environments and which are **unprotected**. |
| `--save` | Verify against Boomi, then write. Needs `--account-id`, `--username`, `--token`. |
| `--force` | With `--save`: write even if Boomi rejects them. For unreachable networks. |
| `--protected "A,B"` | Environments refused for every write. Exact matching. |
| `--api-url URL` | UK/EU accounts only. |
| `--show` | What is stored, token masked. |
| `--clear` | Remove the stored credentials. |

Resolution order, most specific first: `./.env` → `$BOOMI_ES_CONFIG` →
`.boomi-event-streams/env` here or in any directory above → the same file in a
connected folder → your home directory → the shell. Files beat the shell deliberately.
`--check` lists every location it looked in and which one won.

**`--save` verifies before it writes.** Nothing is persisted unless Boomi accepts the
credentials, so a mistyped token never lands on disk and a failed attempt leaves
existing working credentials untouched. It also refuses a swapped account-ID/username
pair outright rather than letting Boomi reject it later as an unexplained 401.

---

## Reading — safe, no writes possible

### `es_discover.py` — what exists

| Flag | Effect |
|---|---|
| `--environment NAME\|ID` | One environment. Omit for all. |
| `--json` | Machine-readable output. |

Topics, subscriptions, tokens. Flags expired tokens and duplicate token names.
Warns if the returned list disagrees with the account's own counts.

```bash
es_discover.py                        # every environment
es_discover.py --environment Test
es_discover.py --json
```

### `es_find.py NAME` — where one thing lives, and where it doesn't

| Flag | Effect |
|---|---|
| `--kind any\|topic\|subscription\|token` | Restrict the search. Default `any`. |
| `--exact` | Full name match instead of substring. |
| `--json` | Machine-readable output. |

```bash
es_find.py orders                     # substring, any kind
es_find.py SO_Producer --kind token
es_find.py 01_SalesForce_Orders --exact
```

The useful half of the output is where the thing is **absent** — that's the shape of
a promotion that stopped halfway.

### `es_topology.py` — which processes use which topics

| Flag | Effect |
|---|---|
| `--environment NAME\|ID` | **Required.** A map only means something per environment. |
| `--topic NAME` | Filter output to one topic. Same scan cost. |
| `--skip-processes` | Topics and health only. Fastest. |
| `--limit N` | Cap component XML reads. |
| `--diagnose` | List every connector type in the account. Use when nothing matches. Pair with `--skip-processes` — the census does not need the scan, and is much faster without it. |
| `--exhaustive` | Read every operation. Slow; only if a targeted scan finds nothing. |
| `--no-reference-api` | Force the process XML scan instead of the dependency graph. |
| `--json`, `--quiet` | Output control. |

```bash
es_topology.py --environment Test
es_topology.py --environment Test --topic orders
es_topology.py --environment Test --diagnose
```

Finds **orphaned operations** — processes referencing a topic that doesn't exist in
that environment. They fail at runtime, not at deploy, so nothing else surfaces them.

### `es_monitor.py` — is it actually working

| Flag | Effect |
|---|---|
| `--environment NAME\|ID` | **Required.** |
| `--dlq` | Dead letter contents for every subscription that has any. |
| `--peek TOPIC` | Messages currently queued, per subscription on that topic. |
| `--subscription NAME` | Narrow `--peek` or `--dlq` to one subscription. |
| `--limit N` | Messages to fetch. Default 10. |
| `--payload` | Include message bodies. **Customer data — opt-in deliberately.** |
| `--json` | Machine-readable output. |

```bash
es_monitor.py --environment Test
es_monitor.py --environment Test --dlq
es_monitor.py --environment Test --peek Student_Data_Math
```

The distinction this exists to make: **backlog with no active consumer is a stalled
integration; backlog with one attached is a slow one.** Different problems, different
fixes. Redelivery count on a stuck message tells you whether anything ever tried.

### `es_report.py` — everything, one document

| Flag | Effect |
|---|---|
| `--environment NAME\|ID` | Adds the topology map and orphan check for that environment. |
| `--limit N` | Cap components scanned for topology. |
| `--out FILE` | Write to a file instead of stdout. |
| `--quiet` | Suppress progress. |

```bash
es_report.py                                    # all environments, fast
es_report.py --environment Production --out handover.md
```

Inventory, cross-environment drift matrix, token health, topology, prioritised
findings. `--environment` does **not** narrow the inventory or drift — those always
cover everything. It adds the process map, which needs a component scan.

### `es_schema.py` — what this account supports

| Flag | Effect |
|---|---|
| `--type NAME` | Inspect one GraphQL type instead of the standard set. |

Run it when a query fails with "Field X is undefined". On some accounts introspection
advertises fields the query validator then rejects; this shows which.

---

## Migration — additive only, never removes

### `es_migrate.py plan` — read-only comparison

| Flag | Effect |
|---|---|
| `--source NAME\|ID` | **Required.** |
| `--target NAME\|ID` | **Required.** |
| `--topics a,b,c` | Only these topics. |
| `--subscriptions a,topic/b` | Only these. Bare name or `topic/name`. |
| `--tokens a,b` | Only these tokens. |
| `--no-tokens` | Exclude tokens entirely. |
| `--out FILE` | Plan path. Default `es-migration-plan.json`. |

Changes nothing. Also the precise way to answer "what differs between these two".

### `es_migrate.py apply`

| Flag | Effect |
|---|---|
| `--plan FILE` | Default `es-migration-plan.json`. |
| `--confirm` | Actually write. Without it, dry run. |

### `es_migrate.py verify`

| Flag | Effect |
|---|---|
| `--source`, `--target` | **Both required.** |

Exit code 2 means differences remain.

```bash
es_migrate.py plan   --source Test --target "Local Test Atm"
es_migrate.py apply  --plan es-migration-plan.json --confirm
es_migrate.py verify --source Test --target "Local Test Atm"
```

---

## Administration — the only script that destroys

Ten subcommands. All take `--environment`. Subscription commands also take `--topic`,
because a subscription is identified by its topic as well as its own name.

### Create

| Command | Flags |
|---|---|
| `create-topic` | `--name` · `--description` · `--partitions` · `--persistent` |
| `create-subscription` | `--topic` · `--name` · `--description` |
| `create-token` | `--name` · `--produce` · `--consume` · `--expires` · `--description` |

Two defaults worth setting on purpose: **a topic with no subscription discards
everything published to it**, and **`--persistent` is off by default** — a
non-persistent topic loses in-flight messages across a broker restart with no error
raised. A token needs `--produce`, `--consume`, or both.

### Update

| Command | Flags |
|---|---|
| `update-topic` | `--name` · `--description` · `--partitions` |
| `update-subscription` | `--topic` · `--name` · `--description` |
| `update-token` | `--name` · `--token-id` · `--rename` · `--expires` · `--produce` / `--no-produce` · `--consume` / `--no-consume` · `--description` |

**`update-token --expires` is the right fix for an expiring token** — it keeps the
existing JWT value, so connection components keep working. Migrating a token mints a
new value that every connection then has to be repointed at.

**`update-topic --partitions` will not do what it looks like.** Partition count is
fixed at creation; the flag exists because the API accepts the field. To repartition,
create a new topic and move producers and consumers onto it.

Token names are not unique. If several share one, the tool refuses rather than
guessing — use `--token-id` from `es_discover.py --json`.

### Destroy — permanent, no restore exists

| Command | Flags | What goes |
|---|---|---|
| `delete-topic` | `--name` · `--confirm` | The topic, its subscriptions, everything queued |
| `delete-subscription` | `--topic` · `--name` · `--confirm` | The subscription and its backlog |
| `delete-token` | `--name` · `--token-id` · `--confirm` | The token; anything using it fails at once |
| `clear-backlog` | `--topic` · `--name` · `--confirm` | Every unconsumed message; the subscription survives |

**Run without `--confirm` first, always.** It describes the cost in counts and changes
nothing:

```
Topic `POC_Topic`
  6 subscription(s) will be removed with it: POC_Sub_Shared, POC_Listen_Exclusive, …
  1457 queued message(s) will be discarded

Nothing was changed. Re-run with --confirm to proceed.
```

That message count is the number people most often turn out not to have known.

`clear-backlog` is the quietest of the four: the subscription survives, so nothing
looks broken afterwards and the messages are simply gone. If someone wants it because
messages are stuck, check `es_monitor.py` first — a backlog with no active consumer
means nothing ever read them, and clearing destroys them while leaving the cause.

---

## Safety

**`BOOMI_PROTECTED_ENVIRONMENTS`** refuses every write in `es_admin` and `es_migrate`
— creates, updates, deletes, with or without `--confirm`. **Empty by default, so
nothing is protected until set**, and matching is **exact**: `Production` does not
cover `Production US`.

**The read scripts cannot destroy anything.** `EventStreamsClient`, the class they
import, has no delete or update method at all. Mutations live in `es_admin_ops.py`,
which nothing imports unless you ran `es_admin.py`. Tests assert both halves.

**`--confirm` is a convention, not a guarantee.** It protects absolutely against
accident, but an agent or script can type it. The rule that a *human* supplies it is
enforced by documentation. Treat it accordingly.

---

## Common tasks

| You want to | Run |
|---|---|
| See what exists | `es_discover.py --environment X` |
| Check if something is in prod yet | `es_find.py NAME` |
| Find what a topic change would break | `es_topology.py --environment X --topic NAME` |
| Work out why a message never arrived | `es_monitor.py --environment X --dlq` |
| Hand over to someone | `es_report.py --environment X --out handover.md` |
| Compare two environments | `es_migrate.py plan --source A --target B` |
| Promote configuration | `plan` → review → `apply --confirm` → `verify` |
| Extend a token before it lapses | `es_admin.py update-token --expires …` |
| Debug an unexplained field error | `es_schema.py` |
