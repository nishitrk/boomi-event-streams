---
name: es-migrate
description: Migrate Boomi Event Streams topics, subscriptions, and tokens from one environment to another — plan the change, review it, apply it, then verify the result. Use this whenever someone wants to promote Event Streams configuration between environments (dev to QA, QA to production), copy topics or subscriptions across, set up a new environment to match an existing one, recreate a topic that is missing after a deployment, or check whether two environments actually match. Also reach for the read-only plan step whenever someone asks what differs between two environments, since planning compares them without changing anything.
---

# Event Streams migration

Promotes Event Streams configuration between environments in three separate steps:
**plan**, **apply**, **verify**.

They are separate commands rather than one, so that the thing a person reviews is
exactly the thing that executes. Nothing is re-derived between the decision and the
action.

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

## The workflow

### 1. Plan — read-only, safe to repeat

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_migrate.py" plan --source Dev --target Test
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_migrate.py" plan --source Dev --target Test --topics orders,invoices
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_migrate.py" plan --source Dev --target Test --subscriptions orders/audit
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_migrate.py" plan --source Dev --target Test --tokens SO_Producer
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_migrate.py" plan --source Dev --target Test --no-tokens
```

The three filters are independent, so any subset can be moved. `--subscriptions`
accepts either a bare name or `topic/name` — the qualified form matters when the same
subscription name appears under several topics, which is common.

Compares the two environments and writes `es-migration-plan.json` (use `--out` for a different path — useful when planning several migrations before applying any). Anything already
present in the target is listed as skipped rather than created, so re-running a
migration is safe and a partially-completed one can simply be resumed.

Show the person the plan and let them read it before going further. Point out
anything surprising — a large number of topics, tokens included when they may not
want them, subscriptions on topics they did not mention.

### 2. Apply — the only step that writes

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_migrate.py" apply --plan es-migration-plan.json            # dry run
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_migrate.py" apply --plan es-migration-plan.json --confirm  # writes
```

Without `--confirm` it re-prints the plan and changes nothing. **Do not pass
`--confirm` on the person's behalf unless they have seen the plan and said to go
ahead.** The flag exists to make approval an explicit act; supplying it automatically
removes the only checkpoint in the process.

Topics are created before subscriptions, since a subscription cannot exist without
its topic. If a topic fails, its subscriptions fail too — that is correct, and the
summary at the end makes the chain visible rather than burying it.

### 3. Verify

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_migrate.py" verify --source Dev --target Test
```

Compares topics, subscriptions, persistence, and partition counts. Exit code 2 means
differences remain. Run this after every migration — the manual alternative has no
verification step at all, which is why missed entities normally surface later as a
failure rather than immediately as a diff.

`reference/limitations.md` in the plugin covers all three of the behaviours below in
more depth, including why they cannot be worked around.

## Three behaviours to explain rather than treat as bugs

**Subscription type shows `NONE` after migration.** The API does not accept a type at
creation; the broker assigns EXCLUSIVE, SHARED, FAILOVER, or KEY_SHARED when a
consumer attaches. A freshly migrated subscription reporting `NONE` is correct.
Verification deliberately does not compare type for this reason.

**New tokens have new JWT values.** Token values cannot be copied between
environments. After migrating tokens, any connection component in the target that
referenced the old value must be updated by hand. Say this out loud when a plan
includes tokens — it is the single most common reason a migration looks complete but
the integration still fails.

**Partition counts carry across.** The source partition count is read and applied to
the new topic. This matters: partitions cannot be changed after creation, and a topic
that silently defaults to one partition behaves differently under load in ways that
are hard to trace back to the migration.

## What this cannot do

Migration is additive only. It creates what the target is missing and skips what is
already there; it never removes anything, and the client it imports has no delete
method to call. If someone asks this skill to clean up or remove entities, that is
`es-admin` — a different skill, reached deliberately, with its own confirmation gate.

That separation is the design. A rule saying "do not delete" can be reasoned around;
a module that was never imported cannot be called. Migration keeps the property even
though the plugin as a whole can now delete.
