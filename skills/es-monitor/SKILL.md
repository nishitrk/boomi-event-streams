---
name: es-monitor
description: Show live Boomi Event Streams health — dead letter queues, active consumer counts, backlogs, throughput rates — and inspect the actual messages sitting on a subscription. Use this whenever someone asks whether Event Streams is actually working rather than how it is configured — why messages are not arriving, whether anything is consuming a topic, what is stuck in a queue, whether a backlog is growing, what went to the dead letter queue, or what a particular message actually contains. Also reach for it on prompts like "is this topic alive", "why did that message never arrive", "is anything consuming orders", "what failed", or any troubleshooting question about message flow rather than setup.
---

# Live Event Streams health

Discovery answers "what exists". This answers "is it working", which is usually the
question behind the question.

Two things here are not visible anywhere else in Boomi:

**Dead letter queues.** Where messages go when delivery keeps failing. Nothing in the
platform surfaces them, so they accumulate unseen until someone thinks to look. Worth
checking on a schedule rather than only when a problem is suspected.

**Active consumer counts.** `activeConsumerCount` says whether anything is attached to
a subscription right now. Everything else in this plugin has to infer that from a
component scan, and an inference is not a fact.

Together they make a backlog diagnosable: **backlog with no active consumer is a
stalled integration; backlog with a consumer attached is a slow one.** Those need
completely different responses, and the count is what tells them apart.

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

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_monitor.py" --environment Test
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_monitor.py" --environment Test --dlq
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_monitor.py" --environment Test --peek orders
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_monitor.py" --environment Test --peek orders --payload
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_monitor.py" --environment Test --json
```

- `--dlq` — dead letter contents for every subscription that has any. Start here when
  something is reported missing.
- `--peek TOPIC` — the messages currently queued. Messages belong to a subscription
  rather than a topic, so this shows each subscription on the matched topic.
- `--payload` — include message bodies. **These are customer data**, so they are
  opt-in: a routine depth check cannot pull payloads into a conversation by accident.
  Think before using it on a production topic, and do not paste bodies into a shared
  document without checking what is in them.
- `--limit N` — how many messages to fetch. Default 10.
- `--subscription NAME` — narrow `--peek` or `--dlq` to one subscription.

## Reading the output

**Redelivery count is the first thing to look at on a stuck message.** Zero means
nothing ever tried to consume it — the consumer was never attached. A high count
means something is trying and failing repeatedly, which is a different problem with a
different fix.

**Publish time tells you how long it has been stuck.** A message queued months ago
with zero redeliveries is an integration that was never finished, not one that broke.

**Idle subscriptions are collapsed into one line**, deliberately. In a quiet
environment every subscription has no consumer and no backlog, and listing each one
buries the findings that matter.

## es-monitor or es-topology?

Both answer health questions and the overlap is real, so the distinction matters:

- **es-monitor reads live counters.** `activeConsumerCount` is a fact about right now.
  Fast, and the only way to see dead letter queues or actual message contents.
- **es-topology reconstructs the process map** from component XML and *infers*
  consumers from it. Slower, and the only way to answer "which process" or to find
  orphaned operations.

For "why isn't this arriving", start here — it is faster and dead letters are the most
common answer. Move to `es-topology` when the question becomes *which process* should
have been consuming, or when nothing is attached and you need to know what was supposed
to be.

## What this cannot do

It reads. There is no consume, no acknowledge, no requeue, and no clear-backlog here —
clearing a backlog discards messages permanently and lives in `es-admin`, behind an
explicit confirmation, precisely so it cannot be reached from a troubleshooting
session by accident.
