---
name: es-report
description: Produce one combined Boomi Event Streams report covering every environment — inventory, cross-environment drift, token health, process-to-topic topology, and prioritised findings — as a single shareable document. Use this whenever someone wants a full picture rather than one answer — an Event Streams audit, a health check, a handover document, a status report for a customer or delivery lead, a pre-go-live review, or a written record of how Event Streams is configured. Also reach for it on prompts like "give me an overview of our Event Streams setup", "document this for the customer", "what state is this account in", or any request for a report, audit, or summary spanning more than one environment.
---

# Combined Event Streams report

One document: summary, cross-environment drift, token health, per-environment
inventory, and prioritised findings. Written to be handed to someone who was not
involved in building the integration.

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
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_report.py"                                  # all environments
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_report.py" --environment Test               # adds topology for Test
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_report.py" --environment Test --limit 50    # bound the scan
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_report.py" --out event-streams-report.md
```

**What `--environment` does, and does not, change.** The inventory, drift matrix and
token health always cover *every* environment — that part is not narrowed. What
`--environment` adds is the process-to-topic map and the orphaned-operation check for
the one environment you name, because those need a component scan and a map is only
meaningful against a single environment's topics.

So "a full write-up" usually means: pass `--environment` for whichever environment
matters most, typically production or the one being handed over. There is no form that
produces a topology map for all environments at once; run the report once per
environment if you need that. Say what the scan will cost before starting it.

## Reading the report

**Cross-environment drift is the section to read first.** It puts every topic against
every environment in one grid, so a promotion that stopped halfway is visible at a
glance rather than inferred.

Resist reporting drift as failure. POC, test, and student-data topics belong in lower
environments and their absence from production is correct. What matters is a topic
missing from an environment where a process already references it — which appears as
an orphaned operation in the findings, and only when `--environment` was used.

**Findings are ordered by how quietly the problem fails**, not by how dramatic they
sound. A topic with no subscriptions and an expired token both produce no error
anywhere until something downstream breaks, which is exactly why they are ranked
above a single-subscription topic that is probably fine.

## Using it as a deliverable

The output is markdown, so it goes straight into a document, a wiki page, or an email.
Two things worth doing before sending it to a customer:

- Read the findings yourself and drop the ones that are deliberate for this account.
  A report that flags six non-issues teaches the reader to ignore all of it.
- Note the date. The report is a snapshot, and backlog counts and token expiry in
  particular move.

If someone wants it regularly, this is a good candidate for a scheduled task — the
drift matrix and token expiry are exactly the things that degrade silently between
reviews.

## When to reach for something else

One environment in detail is `es-discover`. One named entity is `es-find`. Topology
and health for a single environment, with `--diagnose` for when the scan finds
nothing, is `es-topology`. Fixing drift is `es-migrate`.
