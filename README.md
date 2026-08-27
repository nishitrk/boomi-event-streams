# boomi-event-streams

Discover, inspect, monitor, migrate, and administer Boomi Event Streams from Claude
Code — or from the command line, if you prefer.

Eight skills, no dependencies beyond Python 3.9, and nothing deployed into your Boomi
account.

**New here? Read [`SETUP.md`](SETUP.md) first** — it is four steps, and step 3 catches
everyone.

## What it does

| Skill | Answers |
|---|---|
| `es-discover` | What topics, subscriptions and tokens exist? |
| `es-find` | Where is this one thing — and where is it missing? |
| `es-topology` | Which processes produce to and consume from which topics? |
| `es-monitor` | Is it actually working right now? What is stuck or dead-lettered? |
| `es-report` | One document covering everything, for a handover or an audit |
| `es-migrate` | Promote configuration between environments: plan, apply, verify |
| `es-admin` | Create, update, delete — the only skill that can destroy anything |
| `es-env-setup` | Set up or check credentials, and diagnose why they went missing |

Ask in natural language and the right skill loads itself:

> "What topics do we have in QA?"
> "Which processes publish to the orders topic?"
> "Is anything stuck in Test?"
> "What's in the dead letter queue?"
> "Is SO_Producer set up in production yet?"
> "Give me a full Event Streams report for the customer."
> "Promote the Event Streams config from Dev to Test."
> "Extend that token's expiry to next June."

## Why it exists

This is a port of the Event Streams Assistant agent (published to the Boomi
Marketplace, May 2026) from Agent Studio to a Claude Code plugin.

The agent worked by deploying six Boomi processes that exposed the Platform REST and
Event Streams GraphQL APIs through a Shared Web Server, because Agent Studio tools can
only call HTTP endpoints. Claude Code calls those APIs directly, so the entire middle
layer disappears — and with it the setup that existed only to build it.

| | Agent Studio agent | This plugin |
|---|---|---|
| Import bundle, create package, deploy | required | — |
| Environment extensions | 2 connections + 1 process property | — |
| Shared Web Server | Advanced auth, base URL, username, token | — |
| Copy endpoint paths | 3, trailing-slash sensitive | — |
| Configure agent tools | ~20 fields across 4 tools | — |
| Environment IDs | hardcoded per account | discovered at runtime |
| **Setup** | **8 steps, ~20 fields, per environment** | **3 environment variables** |
| Runtime footprint | deployed processes consume the runtime | none |
| Updates | re-export, re-import, redeploy | `/plugin marketplace update` |

The agent is still the right choice for anyone without Claude Code — it works through
the Agent Garden chat UI, and one admin can configure its token for everyone. This
plugin needs a token per person. The two are meant to coexist.

## Install

**In Cowork** — upload `boomi-event-streams.plugin` (or the `.zip`) through **Upload
plugin**.

If an older copy is already installed, **remove it first**. Two registrations of the
same plugin name can leave the new one disabled with no way to enable it, and that
failure gives no useful message.

**From GitHub** — the repository must be a *marketplace*, which is a different thing
from a plugin. Cowork looks for `.claude-plugin/marketplace.json`; without it you get
_"This repository isn't a marketplace."_ This repo is both: `.claude-plugin/` holds
`plugin.json` (what the plugin is) and `marketplace.json` (what is on offer, with
`"source": "./"` pointing at the repo root).

Push the whole repo, then add `owner/repo` as a marketplace. A **private** repo needs
your account authorised for it, or the fetch fails.

> **Known Cowork limitation.** Adding this repo as a marketplace in Cowork works — the
> plugin and its skills appear in the Directory — but **Install** fails with
> `Failed to fetch content: 403`. The same repo and commit installs cleanly through the
> Claude Code CLI, so this is Cowork's install path, not the plugin. Tracked upstream as
> [claude-code#39400](https://github.com/anthropics/claude-code/issues/39400)
> ("Marketplace plugins fail to load skills in Cowork — zip upload of same plugin works
> fine"). **In Cowork, use Upload plugin with the `.zip`;** in the CLI, the marketplace
> route works. Nothing in this repo needs changing for either.

**In Claude Code**, from a local copy:

```
/plugin marketplace add /path/to/boomi-event-streams
/plugin install boomi-event-streams@boomi-event-streams
```

Point it at the folder containing `.claude-plugin/`, not at `skills/`.

## Configure

Full instructions are in [`SETUP.md`](SETUP.md). The short version, and the one thing
worth repeating here:

**Credentials live in a file in a folder you connect. That folder has to be selected
in each new conversation.** Connecting it once makes it available, not automatic — a
new chat starts with nothing attached, and a chat that cannot see the folder cannot
see the credentials. It then reports "no credentials found" although the file is
exactly where you left it.

If that happens, select the folder and re-run your command. **Do not re-run credential
setup** — the credentials are fine, and setting up again means handling a live API
token you did not need to touch.

From the command line:

```bash
python3 scripts/es_setup.py --save --account-id yourcompany-A1B2C3 \
        --username you@yourcompany.com --token YOUR_TOKEN \
        --protected "Production"
python3 scripts/es_setup.py --check    # where do credentials resolve from?
python3 scripts/es_setup.py --test     # do they work, and what is unprotected?
```

Credentials resolve most-specific-first: `./.env` in the current directory, then
`$BOOMI_ES_CONFIG` if set, then `.boomi-event-streams/env` in this directory or any
above it, then the same file in a connected folder, then your home directory. Files
beat the shell deliberately — a stale `export` silently overriding a file someone just
edited produces a 401 with no visible cause. `--check` shows every location it looked
in and which one won.

<details>
<summary>Setting it up by hand instead</summary>

Copy `.env.example` to `.env` in your project:

```
BOOMI_ACCOUNT_ID=yourcompany-A1B2C3
BOOMI_USERNAME=you@yourcompany.com
BOOMI_API_TOKEN=your-platform-api-token
```

Two mistakes cause most first-run failures, and both surface as a bare 401:

- **`BOOMI_USERNAME` is your plain email.** The scripts add the `BOOMI_TOKEN.` prefix
  Boomi's Basic auth expects; including it yourself double-prefixes it.
- **`BOOMI_ACCOUNT_ID` is the `company-A1B2C3` string** from Settings → Account
  Information, not an email. These two get swapped often enough that the scripts check
  and refuse to run rather than letting Boomi return an unexplained 401.

Generate the token at **Settings → Platform API Tokens**. UK/EU accounts also need
`BOOMI_API_URL=https://api.platform.gb.boomi.com`.

**Before using `es-admin` or `es-migrate`, set this:**

```
BOOMI_PROTECTED_ENVIRONMENTS=Production,Prod-EU
```

Environments named here are refused for every write — creates, updates, deletes and
migration targets alike. It is **empty by default, so nothing is protected** until you
fill it in, and matching is **exact**: `Production` does not cover `Production US`.

</details>

## Commands

Everything works from the command line as well as through Claude.

```bash
# What exists
python3 scripts/es_discover.py --environment Test
python3 scripts/es_find.py SO_Producer --kind token

# Which processes use what
python3 scripts/es_topology.py --environment Test
python3 scripts/es_topology.py --environment Test --topic orders
python3 scripts/es_topology.py --environment Test --diagnose --skip-processes  # if it finds nothing

# Is it working
python3 scripts/es_monitor.py --environment Test
python3 scripts/es_monitor.py --environment Test --dlq
python3 scripts/es_monitor.py --environment Test --peek orders --payload

# Everything, one document
python3 scripts/es_report.py --environment Test --out report.md

# Promote between environments
python3 scripts/es_migrate.py plan   --source Dev --target Test
python3 scripts/es_migrate.py apply  --plan es-migration-plan.json --confirm
python3 scripts/es_migrate.py verify --source Dev --target Test

# Create, update, destroy
python3 scripts/es_admin.py create-topic  --environment Test --name orders
python3 scripts/es_admin.py update-token  --environment Test --name SO_Consumer \
                                          --expires 2027-06-01T00:00:00Z
python3 scripts/es_admin.py delete-topic  --environment Test --name orders   # dry run: shows the cost
python3 scripts/es_admin.py delete-topic  --environment Test --name orders --confirm  # PERMANENT — only after a human has seen the dry run

# What this account's schema actually supports
python3 scripts/es_schema.py
```

## Safety model

Three structural properties and one convention. The distinction is worth stating,
because a convention can be talked around and a structural property cannot.

**Destructive capability is isolated, not merely discouraged.** `EventStreamsClient` —
the class every read skill imports — has no delete or update method at all. Those
skills cannot destroy anything however they are prompted, because the capability is
not present in the object they hold. Deletes, updates and clear-backlog live in
`es_admin_ops.py`, which nothing imports unless someone explicitly ran `es_admin.py`.
Tests assert both halves: that the read client stays clean, and that the dependency
only ever runs one way.

**Protected environments are refused in the write path.** Checked before planning,
re-checked against the live environment record at apply time, and applied to updates
as well as deletes — renaming a production topic is not destructive the way a delete
is, but it silently breaks every producer pointing at the old name.

**Writing requires an explicit `--confirm` — and this one is a convention, not a
guarantee.** Destructive commands dry-run by default and report the cost in counts
rather than adjectives: how many subscriptions go with a topic, how many queued
messages get discarded. That number is what people most often turn out not to have
known. The flag protects absolutely against accident, but an agent or a script can
type it, so the rule that **a human must be the one to supply it** is enforced by
documentation rather than by code. Treat it accordingly.

**Credentials stay out of the conversation.** They live in `.env`, are never read into
context, and token values — which the API does return — are never printed.

The honest limit: a user can edit their own `.env` and unprotect an environment. That
is weaker than a platform-enforced guardrail, and is stated here rather than glossed
over.

## Behaviours that look like bugs and are not

**Subscription type shows `NONE`.** The broker assigns type when a consumer attaches;
the API accepts no type at creation. `es-monitor` confirms this directly —
subscriptions with `activeConsumerCount: 0` are exactly the ones reporting `NONE`.

**New tokens have new JWT values.** Token values cannot be copied between environments,
so connection components in the target must be repointed after a migration. Use
`es-admin update-token` to extend an expiry instead — it keeps the existing value.

**Environments appear with no topics.** Event Streams may not be provisioned there.
The output says so explicitly, because that is usually the real answer.

**Some fields are missing on some accounts.** `persistent` and `partitions` are
documented and advertised by introspection, and rejected at execution on at least one
account. The client adapts; see below.

Full detail in [`reference/limitations.md`](reference/limitations.md).

## Scale and pagination

Two different APIs with two different answers, which matters once an account grows
past a hundred of anything.

**Platform REST API — paginated, and handled.** `ComponentMetadata` and
`ComponentReference`, used by the topology scan, return 100 rows per page with a
`queryToken` for the next. `rest_query_all()` follows it to the end. Proven on a real
account at 794 connector operations and 1,726 processes.

**Event Streams GraphQL — no pagination at all.** `eventStreamsTopics` accepts only
`environmentId`; `environments`, `subscriptions`, `tokens`, `producers` and
`consumers` accept no arguments whatsoever, and the schema defines no Relay connection
types. Lists come back whole. `queryMore` does not apply here.

That second point carries a risk worth naming: the response has no `hasNextPage` and
no `totalCount`, so **if the server ever did cap a large list, nothing in the reply
would say so** — the output would look complete and simply be short. An inventory that
under-reports is the kind of error that gets acted on rather than noticed.

So every inventory cross-checks the returned list against the account's own
`topicCount` and `subscriptionCount`, which are computed independently of it. A
mismatch prints a warning at the top of the output, before anything that was built
from the incomplete data. Accounts that do not expose those counts produce no false
alarm.

## Notes on the Boomi APIs

Hard-won, and all documented in [`reference/graphql-reference.md`](reference/graphql-reference.md).
If you extend this plugin, read them first — several cost real debugging time.

- **Introspection is not a reliable guide to what will execute.** On some accounts the
  schema advertises fields the query validator then rejects. The client builds queries
  optimistically and prunes whatever the executor refuses, remembering it for the run.
- **The GraphQL endpoint returns HTTP 200 on authentication failure.** The error is
  only in the response body, so status-code-only checks turn "Unauthorized" into an
  empty result set that reads as "you have no topics".
- **`queryMore` takes the raw token as a plain-text body** — not JSON, not a wrapper
  object.
- **`ComponentReference` nests its results** at `result[].references[]`, and does not
  recurse. It answers one level; walking further is your job.
- **Message indices are 1-based**, and `subscriptionName` is required. `startIndex=0`
  fails with a message about invalid indices that reads like a range problem.
- **Boomi's published Altair sample** compares a JWT `exp` claim in seconds against
  `Date.now()` in milliseconds, so its token cache never hits.

## Tests

```bash
python3 scripts/test_offline.py       # 48 unit checks
python3 scripts/test_integration.py   # 133 end-to-end checks
```

Neither needs network or credentials. The HTTP transport is disabled outright in the
integration suite, so an accidental real request is an error rather than a slow
success.

The fixture deliberately reproduces a real account's misbehaviour rather than a
convenient one: a schema that advertises fields it then rejects, four tokens sharing a
name, an expired token in production, topics present in some environments and not
others, a connector whose subType is not a recognisable Event Streams string, and real
component XML including the `operationType="EXECUTE"` decoy that misclassifies every
operation as a producer if matched. Each of those caught a genuine bug.

## Layout

```
boomi-event-streams/
├── skills/
│   ├── es-discover/SKILL.md     inventory topics, subscriptions, tokens
│   ├── es-find/SKILL.md         one entity, across every environment
│   ├── es-topology/SKILL.md     process-to-topic map and health checks
│   ├── es-monitor/SKILL.md      live health: dead letters, consumers, messages
│   ├── es-report/SKILL.md       combined report for handover or audit
│   ├── es-migrate/SKILL.md      plan, apply, verify
│   ├── es-admin/SKILL.md        create, update, delete
│   └── es-env-setup/SKILL.md    credential setup, and recovering a lost folder mount
├── scripts/
│   ├── boomi_auth.py            credentials, JWT caching, HTTP transport
│   ├── es_setup.py              credential store CLI — verifies before it writes
│   ├── es_client.py             reads and additive creates — no delete, no update
│   ├── es_admin_ops.py          updates and deletes, imported only by es_admin.py
│   ├── es_inspect.py            component scanning and health analysis
│   ├── es_discover.py           inventory CLI
│   ├── es_find.py               lookup CLI
│   ├── es_topology.py           topology and health CLI
│   ├── es_monitor.py            live health and message inspection CLI
│   ├── es_report.py             combined report CLI
│   ├── es_migrate.py            migration CLI
│   ├── es_admin.py              create / update / delete CLI
│   ├── es_schema.py             what this account's schema supports
│   ├── test_offline.py          unit tests
│   └── test_integration.py      end-to-end tests against a mocked account
├── reference/
│   ├── graphql-reference.md     API shapes, auth flow, documented traps
│   └── limitations.md           what the API cannot do, and why
├── SETUP.md                     start here
├── COMMANDS.md                  every command and flag
└── CHANGELOG.md                 what changed, and why
```

Requires Python 3.9+. Standard library only.
