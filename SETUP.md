# Setup

Four steps, once. Step 3 is the one people miss.

---

## 1. Install the plugin

Upload `boomi-event-streams.plugin` (or the `.zip`) through **Upload plugin** in
Cowork. It should report installed and enabled.

If a previous version is already installed, **remove it first**. Two registrations of
the same plugin name can leave the new one disabled with no way to enable it.

## 2. Connect a folder

The plugin stores your Boomi credentials in a file on your own machine. It needs a
folder to put that file in, so connect one — `Documents` is fine. Any folder works
except a system location.

Ask for it in plain language: *"connect my Documents folder"*.

## 3. Select that folder in every new chat

**This is the step that catches everyone, and it is not optional.**

Connecting a folder in step 2 makes it available. It does **not** make it appear
automatically in your next conversation. Each new Cowork chat starts with no folders
attached, and a chat that cannot see your folder cannot see your credentials — so it
reports "no credentials found" even though the file is sitting on your disk, exactly
where you left it.

**So: at the start of a new chat, select the folder you set up in step 2.**

If you forget, nothing is lost and nothing is broken. Say *"connect my Documents
folder"* and re-run whatever you were doing. From 1.4.0 the skills also attempt this
themselves before concluding anything is missing — but selecting the folder yourself
is faster and always works.

> **Never re-run credential setup to fix this.** A missing-credentials message after a
> successful setup almost always means an unselected folder, not lost credentials.
> Re-running setup means re-entering a live API token, usually by pasting it into the
> chat, which puts it in the transcript permanently.

## 4. Add your credentials

Create `.boomi-event-streams/env` inside the folder you connected:

```
BOOMI_ACCOUNT_ID=yourcompany-A1B2C3
BOOMI_USERNAME=you@yourcompany.com
BOOMI_API_TOKEN=your-platform-api-token
BOOMI_PROTECTED_ENVIRONMENTS=Production
```

| Field | Where it comes from |
|---|---|
| `BOOMI_ACCOUNT_ID` | Settings → Account Information. The `company-A1B2C3` string, **not** an email. |
| `BOOMI_USERNAME` | The email you sign in with. **Plain** — the `BOOMI_TOKEN.` prefix is added for you. |
| `BOOMI_API_TOKEN` | Settings → Platform API Tokens → New Token. Shown once. |
| `BOOMI_PROTECTED_ENVIRONMENTS` | Environments refused for every write. **Exact** match — `Production` does not cover `Production US`, so list each one. |

UK/EU accounts also need `BOOMI_API_URL=https://api.platform.gb.boomi.com`.

Three failure modes, all of which surface as a bare `401`:

- The account ID and username swapped. The scripts detect this one and refuse to run.
- `BOOMI_TOKEN.` included in the username, which double-prefixes it.
- A revoked token, or a token belonging to a different account than the ID given.

**Write the token into the file yourself rather than pasting it into chat.** Anything
typed into a conversation stays in that conversation's history.

## 5. Confirm it works

Ask: *"check my Boomi credentials"*, or run:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_setup.py" --check   # where they resolve from
python3 "$CLAUDE_PLUGIN_ROOT/scripts/es_setup.py" --test    # does Boomi accept them
```

`--test` lists your environments and, importantly, **which are not protected**. Read
that line. Anything listed there can be written to and deleted from.

---

## Where credentials are looked for

Most specific first. The first file found wins, and files always beat shell variables
— a stale `export` silently overriding a file you just edited is a genuinely nasty
hour, so `--check` prints which source won.

1. `./.env` in the directory you are working in — a per-engagement override
2. `$BOOMI_ES_CONFIG`, if set — an explicit choice
3. `.boomi-event-streams/env` here, or in any directory above
4. The same file inside a **connected folder** — where setup writes by default
5. `~/.boomi-event-streams/env` — only when nothing better exists
6. Shell environment variables

## If something is wrong

| Symptom | What it actually means |
|---|---|
| "No credentials" in a new chat | The folder is not selected. Step 3. |
| "No credentials" after setup said it saved | Same. Do not re-run setup. |
| Bare `401` | Swapped ID/username, a `BOOMI_TOKEN.` prefix, or a revoked token. |
| Plugin disabled, will not enable | An older copy is still installed. Remove all copies, install once. |
| `Field X is undefined` | Run `es_schema.py` — some accounts reject fields introspection advertises. |
| Topology scan finds nothing | `es_topology.py --environment X --diagnose --skip-processes` lists every connector type in the account. |

## Safety worth knowing before you start

- **`BOOMI_PROTECTED_ENVIRONMENTS` is empty by default.** Nothing is protected until
  you set it. Set it before your first write.
- **Read skills cannot destroy anything.** The client class they import has no delete
  or update method; mutations live in a separate module nothing else imports.
- **Destructive commands dry-run by default** and report the cost in counts — how many
  subscriptions go with a topic, how many queued messages get discarded.
- **`--confirm` should be typed by a person.** It protects absolutely against accident,
  but a script or an agent can type it too.
