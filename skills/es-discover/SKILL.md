---
name: es-discover
description: Inventory Boomi Event Streams entities — topics, subscriptions, and tokens — across one environment or every environment in an account. Use this whenever someone asks what topics or subscriptions exist, wants to see their Event Streams setup, asks which environments have Event Streams provisioned, or asks about tokens and their produce/consume permissions. Reach for it even when the request is phrased loosely — "what's in our Event Streams", "show me the queues", "do we have a topic for orders yet", "what's set up in QA" — since these all resolve to the same inventory question.
---

# Event Streams discovery

Answers "what exists?" for Boomi Event Streams: topics with their partition counts and
persistence, subscriptions nested under them, and the tokens that grant produce and
consume access.

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
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_discover.py"                     # every environment
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_discover.py" --environment Test  # one, by name or ID
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_discover.py" --json              # for further processing
```

The default output is markdown tables. Use `--json` when you need to compute
something from the result rather than show it. For a document meant to be handed
to a customer or a delivery lead, use `es-report` — it covers every environment
and adds drift and health, which a single inventory does not.

## Reading the output

Environments where Event Streams is not provisioned appear in the list, marked as
such. That is deliberate — "no topics found" and "Event Streams was never turned on
here" are very different answers to the same question, and the second one is usually
what the person actually needed to know.

Two fields tend to prompt follow-up questions:

**Subscription type showing `NONE`.** Type is assigned by the broker when a consumer
attaches, not when the subscription is created. `NONE` means nothing is currently
consuming, which is worth mentioning if the person is troubleshooting. It is not a
misconfiguration.

**Partitions.** Partition count affects ordering and throughput and cannot be changed
after creation, so it is the field most worth checking when two environments behave
differently under load.

Token values are credentials. The API returns them, and the script deliberately does
not print them. If someone needs a token value, point them at the platform UI rather
than fetching it into the conversation.

## Comparing environments

To answer "what's different between Dev and Test", run the inventory for each and
diff them — or use the migration skill's `plan` command, which does exactly this
comparison and is read-only:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_migrate.py" plan --source Dev --target Test
```

Planning writes a file and changes nothing in either environment, so it is a safe way
to get a precise difference even when no migration is intended.

## When discovery is not enough

If the question is "which processes use this topic" or "is anything wrong here",
that is the `es-topology` skill — it reconstructs the process-to-topic map from
component XML, which this skill does not do.
