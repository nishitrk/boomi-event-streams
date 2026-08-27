---
name: es-admin
description: Create, update, and delete Boomi Event Streams topics, subscriptions, and tokens, and clear a subscription backlog. Use this when someone wants to make a change rather than inspect one — create a topic or subscription, add a token, rename or re-describe something, extend a token's expiry, remove an entity that is no longer needed, or empty a stuck queue. Also reach for it when someone asks to fix a problem another skill found, such as an expired token or a missing topic. This skill performs destructive, unrecoverable operations, so read its safety notes before running anything with --confirm.
---

# Event Streams administration

Creates, updates, and deletes Event Streams entities. **This is the only skill in the
plugin that can destroy anything.**

## Before anything else: what is at stake

Three operations here permanently destroy data, and none of them is recoverable —
Event Streams has no restore, no undo, and no bin:

- **delete-topic** removes the topic, every subscription on it, and everything queued
- **delete-subscription** removes the subscription and discards its backlog
- **clear-backlog** discards every unconsumed message but keeps the subscription, so
  nothing looks broken afterwards. That makes it the quietest of the three.

Deleting a **token** is not data loss, but anything authenticating with it starts
failing immediately, and the value cannot be recovered.

Every destructive command requires `--confirm`. **Never supply `--confirm` on the
person's behalf.** Run it without the flag first, show them exactly what it reports —
including how many messages would be discarded — and let them decide. That count is
the number people most often turn out not to have known.

Environments named in `BOOMI_PROTECTED_ENVIRONMENTS` are refused for *every* write
here, updates included. Renaming a production topic is not destructive the way a
delete is, but it silently breaks every producer and consumer pointing at the old
name, which is worse in practice.

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

**This variable is what stands between a command and a production incident:**

```
BOOMI_PROTECTED_ENVIRONMENTS=Production,Prod-EU
```

Environments named here are refused for **every** write in this skill — creates,
updates, deletes and clear-backlog alike, with or without `--confirm`.

**It is empty by default, so nothing is protected until it is filled in.** Matching is
exact: `Production` does not cover `Production US` or `Production-EU`, so every
environment has to be listed individually. If someone is about to change anything and
this is unset or incomplete, stop and say so before running the command.

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

**Create**

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_admin.py" create-topic --environment Test --name orders
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_admin.py" create-subscription --environment Test --topic orders --name orders-sub
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_admin.py" create-token --environment Test --name producer --produce
```

Two defaults worth setting deliberately rather than accepting:

- **A topic with no subscription discards everything published to it**, so create the
  subscription in the same sitting.
- **`--persistent` is not set by default.** A non-persistent topic loses whatever is
  in flight across a broker restart, with no error raised anywhere — `es-topology`
  rates that a high-severity finding. Ask whether messages need to survive a restart;
  for anything carrying business data the answer is yes.

A token needs `--produce`, `--consume`, or both.

**Update**

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_admin.py" update-topic --environment Test --name orders --description "..."
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_admin.py" update-token --environment Test --name SO_Consumer --expires 2027-06-01T00:00:00Z
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_admin.py" update-subscription --environment Test --topic orders --name orders-sub --description "..."
```

**`update-topic --partitions` will not do what it looks like.** Partition count is
fixed at creation in Event Streams; the flag exists because the API accepts the field,
but the broker will either ignore it or reject it. To change partitioning you create a
new topic and migrate producers and consumers onto it. Say that rather than running
the command and reporting an ambiguous result.

**Extending an expiry is the right fix for an expiring token.** Update keeps the
existing JWT value, so connection components carry on working — unlike migrating a
token, which mints a new value that every connection then has to be pointed at.

Token names are not unique. If several share one, the tool refuses rather than
guessing; use `--token-id` from `es_discover.py --json`.

**Destroy**

All four destructive commands take the same shape: run once without `--confirm` to see
the cost, then again with it only after the person has said yes.

```bash
# Always run this form first — describes and exits, changes nothing
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_admin.py" delete-topic        --environment Test --name orders
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_admin.py" delete-subscription --environment Test --topic orders --name orders-sub
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_admin.py" delete-token        --environment Test --name old-token
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_admin.py" clear-backlog       --environment Test --topic orders --name orders-sub
```

Note that `delete-subscription` and `clear-backlog` need **both** `--topic` and
`--name` — a subscription is identified by its topic as well as its own name.

Only after the person has seen that output and agreed, add `--confirm` to the same
command. There is no separate syntax; it is the identical line plus one flag.

`update-subscription` is shown under **Update** above — it changes only the
description and needs no confirmation.

## When another skill found the problem

Two common handoffs, and both deserve a question first rather than a fix:

- **Expired token** (found by `es-discover` or `es-find`) → `update-token --expires`,
  not delete-and-recreate. Ask what expiry they want; do not invent one.
- **Missing topic causing orphaned operations** (found by `es-topology`) → usually
  `es-migrate` from the environment that has it, which carries the subscriptions too.
  Creating it by hand here loses that.

If someone asks to clear a backlog because messages are stuck, check `es-monitor`
first. A backlog with no active consumer means nothing ever read those messages —
clearing it destroys them and leaves the actual cause untouched.
