---
name: es-topology
description: Map which Boomi processes publish to and consume from which Event Streams topics, and run a health check that flags orphaned operations, topics with no consumer, backlogs with nothing draining them, and non-persistent topics that have subscribers. Use this whenever someone asks which processes touch a topic, wants an Event Streams topology or architecture picture, is doing an impact assessment before changing a topic, is investigating why messages seem to disappear or pile up, is handing over or inheriting an integration, or asks whether their Event Streams setup looks healthy. Also reach for it on vaguer prompts like "why isn't this message arriving", "what would break if I renamed this topic", or "can you make sense of this Event Streams setup" — they all need the process-to-topic map.
---

# Event Streams topology and health

Boomi has no direct answer to "which processes use this topic". The link only exists
inside component XML, so this skill reconstructs it, then uses the resulting map to
find problems that are invisible from either side alone.

## Credentials

**If a command reports missing credentials, do this before asking the user for
anything.** Stored credentials live in a folder on the user's own machine, but the
sandbox only sees folders mounted into *this* conversation, and mounts do not carry
over from the last one. Missing credentials therefore usually means an unmounted
folder, not an unconfigured plugin. Asking the user to set up again in that state is
wrong twice over: it wastes their time, and it invites them to re-enter a live API
token into the chat transcript.

1. Call `request_cowork_directory` for the user's workspace folder — its path is in
   your own system prompt. If it is already their selected folder, this mounts it
   with no prompt.
2. Re-run `es_setup.py --check`. If it now reports a stored file, carry on; there is
   nothing else to do and nothing to tell the user.
3. Only if it still finds nothing is this genuinely first-time setup. Use the
   `es-env-setup` skill.

Never ask the user to paste an API token into the conversation. If setup is truly
needed, have them write it into the credentials file directly.

Three variables, from the environment or a `.env` in the directory you run from:

```
BOOMI_ACCOUNT_ID=yourcompany-A1B2C3
BOOMI_USERNAME=you@yourcompany.com
BOOMI_API_TOKEN=your-platform-api-token
```

Two mistakes account for most first-run failures, and both surface as a bare 401:

- **`BOOMI_USERNAME` is the plain email.** The scripts add the `BOOMI_TOKEN.` prefix
  that Boomi's Basic auth expects. Including it yourself double-prefixes it.
- **`BOOMI_ACCOUNT_ID` is the `company-A1B2C3` string**, from Settings → Account
  Information — not an email. These two get swapped often enough that the scripts
  check for it and refuse to run.

Generate the token at **Settings → Platform API Tokens**. UK/EU accounts also need
`BOOMI_API_URL=https://api.platform.gb.boomi.com`.

One more variable exists, and matters when someone moves from reading to changing:

```
BOOMI_PROTECTED_ENVIRONMENTS=Production,Prod-EU
```

Environments named here are refused by every write in `es-migrate` and `es-admin`.
**It is empty by default, so nothing is protected until it is filled in**, and matching
is exact — `Production` does not cover `Production US`, so list each one. Nothing in
this skill writes, but if the conversation turns toward migrating or deleting and this
is unset, say so.

## Running the scripts

The scripts ship inside the plugin, in its `scripts/` directory. They are **not** in
the skill folder — that holds only this file — so a relative path will not find them.

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/SCRIPT.py"
```

`$CLAUDE_PLUGIN_ROOT` is set when this is installed as a plugin, which is the normal
case. If it is unset, locate the scripts by a filename unique to this plugin rather
than by folder name, since the folder can be called anything:

```bash
find / -name es_discover.py -path "*/scripts/*" 2>/dev/null | head -3
```

Its parent directory is the `scripts/` path to use. If that finds nothing, the plugin
was installed without its scripts — copying only `skills/` leaves them behind — and
the fix is to install the whole plugin, not to hunt further.

Run from the directory holding your `.env`, since that is where credentials are read
from. The scripts themselves work from any working directory.

## Running it

`--environment` is **required** — a process-to-topic map only means something
against one environment's topics, so there is no all-environments form.

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_topology.py" --environment Test
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_topology.py" --environment Test --topic orders
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_topology.py" --environment Test --skip-processes   # fast, topics only
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_topology.py" --environment Test --limit 50         # bound the scan
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_topology.py" --environment Test --diagnose         # why nothing matched
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_topology.py" --environment Test --json
```

Every flag is listed under **What it costs** below, with the cost implications that
make the choice between them meaningful.

**If the user did not name an environment**, ask which one rather than guessing —
`--environment` is required and the answer differs per environment. Offer to run it
for each if they want the whole account.

## How operations are recognised, and why the Match column matters

Matching on connector subType is the obvious approach and it is fragile — the
identifier has varied across Boomi releases, and guessing wrong makes the scan find
nothing, which looks identical to "no process uses Event Streams".

So the operation's own configuration is read instead. The Event Streams connector
stores its topic in a `<field id="topic" value="..."/>` element and its direction in
`customOperationType` (PRODUCE / CONSUME / LISTEN). Reading the declared topic is
better than scanning for known topic names, because it also returns topics that exist
*nowhere* — and those are the orphaned operations, the most valuable thing this scan
finds. A known-names scan is structurally blind to them: you cannot find a topic by
matching against a list it is not on.

Connector types are then learned from evidence rather than assumed, so the scan works
on accounts whose Event Streams connector has an unrecognisable name. One real account
publishes it as `officialboomi-X3979C-events-prod`.

The **Match** column records how each row was identified:

- `exact` — the operation declares this topic and it exists here. Trustworthy.
- `declared` — the operation declares a topic that does **not** exist in this
  environment. This is an orphaned operation and it will fail at runtime.
- `dynamic` — the topic is resolved at runtime from a process property. The value
  shown is an expression, not a topic name, and it is excluded from the orphan check.
- `pattern` / `by-connector` — inferred rather than declared. Treat with suspicion.

When reporting results, mention the match type for anything surprising. "Process X
publishes to topic Y" reads as fact; if the row is `dynamic`, the honest version is
"publishes to whatever `DPP_TargetTopic` resolves to at runtime".

One caution about direction. `operationType` sits next to `customOperationType` in the
same element and is always `EXECUTE`, whatever the operation does. If direction cannot
be read, the analysis reports `unknown` and **skips** the publisher and consumer
checks rather than guessing — "no process publishes to this topic" is too strong a
claim to make on an unreadable attribute.

## es-topology or es-monitor?

This skill *infers* whether anything consumes a topic, by reconstructing the process
map from component XML. `es-monitor` reads `activeConsumerCount` directly and returns
a fact rather than an inference — and it is far faster, since it needs no component
scan.

If the question is "is anything consuming this" or "why is this stuck", run
`es-monitor` first. Come here when the question is *which process*, or when you need
orphaned operations, which only the component map can find.

## When it finds nothing

Run `--diagnose`. It lists every connector subType in the account with a count, which
answers the actual question — whether this account has an Event Streams connector at
all, and under what name. If one is clearly there but unmatched, add its subType to
`BOOMI_ES_CONNECTOR_TYPES` in `.env`.

## What it costs

Two things make this cheap that are worth understanding, because both were expensive
before and the reasons are instructive.

**Finding operations scales with connector types, not operations.** Event Streams is a
property of a connector type, so the scan decides per type rather than per operation:
every operation on a type that looks like Event Streams, plus three probes from each
remaining type to rule it in or out, then all the operations of anything confirmed. On
a real account that was 112 reads instead of 794.

**Finding which processes use them uses the dependency graph.** Boomi can be asked
which components reference a given component, so there is no need to read every
process. On the same account that replaced 1,726 component fetches with a handful of
queries. If the dependency graph is not queryable, it falls back to scanning process
XML automatically and says so.

Component XML caches under `.es-cache/`, keyed by ID and version, so repeat runs are
fast and changed components are re-read.

Flags worth knowing:

- `--skip-processes` — topics and health only, no process map. Fastest.
- `--limit N` — caps XML reads. Bounds a first look on an unfamiliar account.
- `--exhaustive` — reads every operation. Only needed if a targeted scan finds nothing
  and you suspect a connector whose probed operations all set their topic at runtime.
- `--topic NAME` — filters the output to one topic (substring match). The scan costs
  the same either way, so this is for readability when someone asks about a single
  topic. Health findings are computed across the whole environment first, so the
  filtered view still reflects reality rather than a partial picture.
- `--no-reference-api` — forces the process XML scan. For comparing the two.
- `--json` — machine-readable output, for computing on the result rather than showing it.
- `--quiet` — suppresses progress messages. Useful when capturing output.

## Related tools in this plugin

- `scripts/es_schema.py` — shows what this account's Event Streams schema actually
  supports. Run it when a query fails with "Field X is undefined", since on some
  accounts introspection advertises fields the query validator then rejects.
- `reference/limitations.md` — what the API cannot do and why, including the
  introspection-versus-execution mismatch above.
- `reference/graphql-reference.md` — auth flow, endpoints, and the documented traps.

## Health findings, and what they mean

Findings are ordered by severity because the first two change behaviour silently,
which makes them worth more attention than their symptoms usually attract.

**High**

- *No subscriptions.* Nothing consumes the topic, so published messages are discarded
  on arrival. This looks identical to a working integration from the producer's side.
- *Non-persistent topic has subscribers.* Messages are not retained across a broker
  restart. Subscribers lose whatever was in flight, with no error anywhere.
- *Backlog with no consumer.* Messages are queuing and nothing is draining them. This
  is the shape a stalled integration takes before anyone notices it stopped.
- *Orphaned operation.* A process references a topic that does not exist in this
  environment — it will fail at runtime. Nearly always a promotion where the process
  moved and the topic did not.

**Medium**

- *Subscriptions exist but no process consumes them.* Either a consuming process is
  missing, or consumption happens outside this account over the REST API. Worth
  confirming which, because the two have very different implications.

**Low**

- *No process publishes to this topic.* May be produced to externally, or left over
  from work that has moved on.
- *Single subscription carries all consumption.* Often deliberate. Flagged so it is a
  decision rather than an accident.

## Interpreting the map for the person asking

The raw table answers the literal question, but the useful answer is usually one step
further on. Some patterns worth naming when you see them:

- A topic with producers in one process and consumers in several is a fan-out. Renaming
  or repartitioning it affects every consumer, so impact assessments should list them.
- A topic that appears in the map but not in the topic list is the orphaned case above,
  and it means someone promoted a process without promoting its topic — check whether
  other topics from the same promotion are also missing.
- Topics with no map entries at all, in an account that clearly uses Event Streams,
  usually mean the scan was limited or the connector identifier is unusual. If you
  suspect the latter, `BOOMI_ES_CONNECTOR_TYPES` accepts extra comma-separated hints
  to match against.

## Fixing what it finds

This skill only reads. Creating a missing topic in the target environment is the
`es-migrate` skill's job, and orphaned operations are usually best fixed by running a
migration plan from the environment where the topic does exist.
