---
name: es-find
description: Look up a specific Boomi Event Streams topic, subscription, or token by name across every environment at once, showing which environments have it and — more usefully — which do not. Use this whenever someone asks where a particular topic or token lives, whether something exists in production yet, why a topic works in one environment but not another, which environments a subscription is missing from, or asks to trace a single named entity. Also reach for it on questions like "is orders set up in prod", "did that token get promoted", or "where is SO_Producer used" — anything about one named thing rather than a full inventory.
---

# Find one thing across environments

Answers "where is this, and where isn't it" for a single topic, subscription, or
token.

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
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_find.py" orders                        # substring, any kind
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_find.py" SO_Producer --kind token
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_find.py" 01_SalesForce_Orders --exact
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_find.py" orders --json
```

Substring matching is the default because people rarely remember the exact name, and
`--exact` is there for when a partial match would be ambiguous.

## What makes the output useful

Every result names the environments where the thing is **absent** as well as where it
is present. That asymmetry is the point: a topic in Test but not Production is the
signature of a promotion that stopped halfway, and it is the condition that produces
orphaned operations and runtime failures later.

When you report a result, lead with the gap rather than the inventory. "Present in
Local Test Atm and Test, missing from Production" is the answer; the list of places it
does exist is supporting detail.

Expired tokens are flagged inline, so a lookup doubles as a credential check.

## Interpreting a gap

Not every difference is a problem, and saying so saves the person a pointless
investigation:

- POC, test, and student-data topics legitimately do not belong in production
- A topic missing from one environment matters if a process there references it —
  that is the orphaned-operation case, and `es-topology` is what confirms it
- A token missing from the target is expected before a migration and a problem after
  one

If someone asks "should this be everywhere?", the honest answer usually depends on
whether a process needs it, which means checking topology rather than guessing.

## When to reach for something else

A full picture of one environment is `es-discover`. Everything at once, including
drift across environments, is `es-report`. Closing a gap is `es-migrate`.
