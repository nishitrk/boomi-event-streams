---
name: es-env-setup
description: Set up, check, or change the Boomi credentials this plugin uses — account ID, username, API token, and which environments are protected from writes. Use when someone is setting the plugin up for the first time, when any Event Streams command fails with a missing-credential or 401 error, when they ask where credentials are stored or want to point at a different Boomi account, or when they are asked to set up again in a new conversation despite having done it already — that last one is a mount problem this skill diagnoses, not a setup one.
---

Set up, check, or change the user's Boomi credentials. Once saved they are found from
any directory in any future conversation, so this is a one-time step per machine.

## Before anything else: is this actually a setup problem?

Most "no credentials" reports are not. Credentials live in a file inside a folder on
the user's machine, and **that folder has to be selected in each new conversation** —
connecting it once makes it available, not automatic. A conversation that cannot see
the folder cannot see the file, and reports exactly what a never-configured install
reports.

So before treating this as setup:

1. Call `request_cowork_directory` for the user's workspace folder — the path is in
   your own system prompt.
2. Re-run `es_setup.py --check`.

If credentials appear, say so plainly — the folder was not selected, nothing was lost,
and no setup is needed. Only if they still do not appear is this a real first-time
setup.

**Never resolve this by re-running setup.** It means handling a live API token that
did not need touching, and the usual way users supply one is by pasting it into the
chat, where it stays.

## First, look before asking

Run this and read the result before asking the user anything:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_setup.py" --check
```

It reports where credentials currently resolve from and whether they are usable.
Three outcomes:

- **Already working** — say so, show which account and which environments are
  protected, and ask whether they want to change anything rather than re-running
  setup they do not need.
- **Present but malformed** — the check names the specific problem, usually the
  account ID and username being swapped. Offer to fix just that.
- **Nothing found** — go on to collect them.

## Collecting the values

Use the **AskUserQuestion tool** so they get a form rather than a series of chat
messages. Ask for all of these in one go:

| Field | What it is | Where to find it |
|---|---|---|
| **Account ID** | `yourcompany-A1B2C3` | Settings → Account Information |
| **Username** | The email they sign in with | — |
| **API token** | The platform API token | Settings → Platform API Tokens → New Token |
| **Protected environments** | Comma-separated, refused for every write | Their production environment names |
| **Region** | UK/EU accounts need a different API URL | Only if their account is UK/EU hosted |

Four things to tell them while they fill it in, because each prevents a specific
failure:

- **The username is the plain email.** Not the `BOOMI_TOKEN.`-prefixed form — that
  prefix is added automatically, and including it causes a 401 that says nothing
  about the cause.
- **The account ID is not an email.** These two get swapped constantly. The setup
  script refuses to save a swapped pair rather than letting Boomi reject it later.
- **The API token is shown once.** If they no longer have it, they need a new one;
  there is no way to read an existing token back.
- **Protected environments matter more than it sounds.** Anything not listed can be
  written to and deleted from. Matching is **exact** — `Production` does not cover
  `Production US`, so each one has to be listed. Ask directly which environments are
  production; do not guess from names.

## Saving

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_setup.py" --save \
  --account-id "..." --username "..." --token "..." \
  --protected "Production,Prod-EU"
```

Add `--api-url https://api.platform.gb.boomi.com` for UK/EU accounts.

**Order matters here, and it is deliberate: nothing is written until Boomi accepts
the credentials.** The script checks the format, then makes a real call to Boomi, and
only writes the file if that succeeds. A mistyped token is never persisted, and if the
user already had working credentials stored, a failed attempt leaves them untouched.

On success it reports **where** it saved and whether that location persists, then
lists the environments it can see including which are **not** protected. Show the user
that output in full.

**If it warns that the location is not persistent, do not gloss over it.** Some hosts
run with an ephemeral home directory, and credentials written there are gone by the
next conversation — the user would be asked to set up again every time without ever
being told why. When that warning appears, tell them to connect a folder from their
machine, or set `BOOMI_ES_CONFIG` to a path that survives, and re-run.

**Never echo the API token back** into the conversation, and do not put it in a
summary. Pass it straight to the command.

## When it fails

If Boomi rejects the credentials, **nothing is saved** — say that plainly, because
"it failed" otherwise sounds like the file is now half-written. Anything previously
stored is still there and still working.

The usual causes are a revoked token, or a token belonging to a different account than
the account ID given. Offer to re-run with a corrected value rather than debugging
further.

`--force` saves without checking. Only suggest it when the user is confident the
values are right and Boomi is genuinely unreachable — from a restricted network, say.
Do not reach for it to get past a rejection.

## Other things this command can do

```bash
es_setup.py --check    # where do credentials resolve from, are they usable
es_setup.py --test     # verify against Boomi, list visible environments
es_setup.py --show     # what is stored, token masked
es_setup.py --clear    # remove the stored credentials
```

## How resolution works, if they ask

Most specific first:

1. `./.env` in the directory they are working in — a per-engagement override
2. `$BOOMI_ES_CONFIG`, if set — an explicit choice of store
3. `.boomi-event-streams/env` in this directory or any directory above it
4. The same file inside a connected folder — where this command writes by default
5. `~/.boomi-event-streams/env` — used only when nothing better exists

The store is not one fixed path. It is chosen at save time as the best location that
will still exist tomorrow, which is why the command reports where it went.

Files beat the shell deliberately. A stale `export` silently overriding a file
someone just edited produces a 401 with no visible cause. When sources disagree,
`--check` says which one won.
