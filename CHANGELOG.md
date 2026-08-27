# Changelog

## 1.5.0

Documentation, and one diagnostic that was quietly useless.

- **`SETUP.md`** — a real setup guide. Its step 3 is the one that catches everyone:
  a connected folder must be **selected in each new conversation**. Connecting it once
  makes it available, not automatic, so a new chat reports "no credentials" while the
  file sits untouched on disk. The guide says plainly that the fix is to select the
  folder, and that re-running credential setup is the wrong response — it means
  handling a live API token that did not need touching.
- **`es_topology --diagnose --skip-processes` produced no census at all.** The census
  was only ever built as a side effect of the scan that `--skip-processes` skips, so
  the flag combination that should have been the fast path returned nothing. The
  census is one cheap metadata query and is now taken directly: 17 connector types in
  25 seconds, where `--diagnose` alone needs several minutes for the same list.
- README updated for eight skills, corrected test counts, and the Cowork upload route.
- `es-env-setup` now leads with the folder check rather than the setup form.

## 1.4.1

- **All eight skill descriptions normalised to a single plain YAML scalar.** Two of
  them (`es-monitor`, `es-report`) contained a colon-space, which strict YAML reads as
  a nested key — their frontmatter had never actually parsed. A third used a folded
  `>-` block, a construct none of the previously-working skills used. Whether or not
  this was what Cowork rejected, it removes every frontmatter variable between this
  build and the last one that installed.

## 1.4.0

The credential problem, actually fixed.

- **Every skill now recovers a lost folder mount by itself.** Stored credentials live
  on the user's machine, but the sandbox only sees folders mounted into the *current*
  conversation, and mounts do not carry over. A new chat therefore reported "no
  credentials" even though the file was sitting on disk, unchanged — and the tool's
  advice was to set up again, which was both wrong and an invitation to paste a live
  API token into a transcript. Skills now mount the folder and retry before concluding
  anything is missing.
- **`--check` names the mount as the likely cause** when nothing from the machine is
  mounted, instead of jumping straight to "run --save".
- Skills will not ask for an API token in conversation. Genuine first-time setup
  writes it to the credentials file directly.

## 1.3.1

- **Setup refuses to save to a location that will not survive the conversation**,
  where it previously saved and printed a warning. The old behaviour reported success,
  worked for that session, and was gone by the next — so the failure surfaced later,
  somewhere unrelated, looking like a broken tool. `--force` keeps the old behaviour.
- **`/es-env-setup` moved from `commands/` to `skills/`**, clearing the "legacy
  commands/ format" notice on install.
- Removed a real environment ID that was serving as test fixture data, and 79KB of
  Python bytecode that was riding along in the package.

## 1.3.0

Credential setup, and making it survive.

- **`/es-env-setup`** — one-time credential setup through a form, rather than editing
  `.env` by hand in every project.
- **Credentials now persist across conversations.** The store is chosen at save time
  as the most durable location available: a connected folder is preferred over a home
  directory, because some hosts run with an ephemeral home where saved credentials
  are gone by the next session. Setup reports where it saved and warns if that
  location will not survive.
- **Setup verifies before it writes.** Nothing is persisted unless Boomi accepts the
  credentials, so a mistyped token never lands on disk and a failed attempt leaves
  existing working credentials untouched. `--force` overrides, for genuinely
  unreachable networks.
- `BOOMI_ES_CONFIG` sets the store location explicitly.
- Resolution order: `./.env` → `$BOOMI_ES_CONFIG` → `.boomi-event-streams/env` in this
  directory or any above → a connected folder → home → the shell.

Packaging fixes, all of which blocked installation:

- The archive now has `.claude-plugin/` at its **root**, not nested inside a folder.
- `marketplace.json` is excluded from the uploaded package — it was the only file
  referencing a remote URL, and the likely cause of "Failed to fetch content: 403".
- No manifest references anything remote.

## 1.2.0

Live health and full CRUD.

- **`es-monitor`** — dead letter queues, active consumer counts, throughput, and
  message inspection. Distinguishes a stalled subscription (backlog, no consumer)
  from a slow one (backlog, consumer attached); they need different fixes.
- **`es-admin`** — create, update and delete topics, subscriptions and tokens, plus
  clear-backlog.
- **Destructive capability is isolated.** `EventStreamsClient`, which every read skill
  imports, has no delete or update method at all. Mutations live in `es_admin_ops.py`,
  which nothing imports unless the admin tool was explicitly run.
- Destructive commands dry-run by default and report the cost in counts — how many
  subscriptions go with a topic, how many queued messages get discarded.
- `BOOMI_PROTECTED_ENVIRONMENTS` now refuses **every** write, updates included.
  Matching is exact: `Production` does not cover `Production US`.
- Truncated-list detection: inventories cross-check against the account's own counts,
  since the GraphQL API has no pagination and no way to signal a capped result.

## 1.1.0

Lookup, reporting, and a topology rewrite.

- **`es-find`** — locate one topic, subscription or token across every environment,
  showing where it is *absent* as well as where it exists.
- **`es-report`** — one document: inventory, cross-environment drift, token health,
  topology, prioritised findings.
- Topology now identifies operations by the topic they declare rather than by
  connector name, which is what makes orphaned operations findable at all.
- Process mapping uses the component dependency graph instead of reading every
  process. On a real account: 1,726 component fetches replaced by a handful of
  queries, and operation scanning cut from 794 reads to 112.
- `--topic` filter, and `--diagnose` for when a scan finds nothing.

## 1.0.0

First working version: `es-discover`, `es-find`, `es-topology`, `es-report`,
`es-migrate`. A port of the Event Streams Assistant Agent Studio agent, with the six
deployed Boomi processes replaced by direct API calls.
