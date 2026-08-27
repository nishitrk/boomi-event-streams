#!/usr/bin/env python3
"""
Store Boomi credentials once, use them from any directory.

    python es_setup.py --check                     # where do credentials come from?
    python es_setup.py --save --account-id ... --username ... --token ...
    python es_setup.py --test                      # do they actually work?
    python es_setup.py --show                      # what is stored (token masked)
    python es_setup.py --clear                     # remove the stored credentials

The problem this solves: a project-local `.env` is only found when you happen to be
in that project. Open a conversation somewhere else and every command fails with
"missing required environment variable", which reads like a broken tool rather than
a missing file.

Credentials are written with 0600 permissions to the most durable location
available, which --save names when it writes. That is not one fixed path: a home
directory is ephemeral on some hosts, so a connected folder is preferred where one
exists. --save refuses outright rather than writing somewhere that will not survive.

Resolution order, most specific first:

  1. ./.env in the current directory        -- per-engagement override
  2. $BOOMI_ES_CONFIG, if set               -- an explicit choice of store
  3. .boomi-event-streams/env here or above -- project or parent directory
  4. the same file in a connected folder    -- where --save writes by default
  5. ~/.boomi-event-streams/env             -- only when nothing better exists
  6. the shell environment                  -- fallback

A caveat that matters on hosts which mount folders per conversation: a connected
folder has to be attached to the conversation before step 4 can see it. Credentials
that "disappear" in a new session are almost always this, not a lost file.

Files beat the shell deliberately. A stale `export` silently overriding the file
someone just edited produces a 401 with no visible cause, and that is a genuinely
nasty hour. When two sources disagree, the one that won is printed.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from boomi_auth import (  # noqa: E402
    BoomiAPIError,
    BoomiAuthError,
    Config,
    build_client,
    credential_sources,
    describe_sources,
    preferred_store,
)

FIELDS = [
    ("BOOMI_ACCOUNT_ID", "Account ID", True,
     "Settings -> Account Information. Looks like yourcompany-A1B2C3. Not an email."),
    ("BOOMI_USERNAME", "Username", True,
     "The email you sign in to Boomi with. Plain — the BOOMI_TOKEN. prefix is added for you."),
    ("BOOMI_API_TOKEN", "API token", True,
     "Settings -> Platform API Tokens -> New Token. Shown once, so copy it immediately."),
    ("BOOMI_PROTECTED_ENVIRONMENTS", "Protected environments", False,
     "Comma-separated. Refused for every write. Matching is exact, so list each one."),
    ("BOOMI_API_URL", "API base URL", False,
     "Only for UK/EU accounts: https://api.platform.gb.boomi.com"),
]


def validate(values: dict[str, str]) -> list[str]:
    """Catch the two mistakes that otherwise surface as an unexplained 401."""
    problems = []
    account = values.get("BOOMI_ACCOUNT_ID", "")
    username = values.get("BOOMI_USERNAME", "")

    if "@" in account and "@" not in username:
        problems.append(
            "BOOMI_ACCOUNT_ID and BOOMI_USERNAME look swapped — the account ID is the "
            "'company-A1B2C3' string, the username is your email."
        )
    elif "@" in account:
        problems.append("BOOMI_ACCOUNT_ID is an email address; it should be the account ID.")
    elif "@" not in username:
        problems.append("BOOMI_USERNAME does not look like an email address.")

    if username.startswith("BOOMI_TOKEN."):
        problems.append(
            "BOOMI_USERNAME has the BOOMI_TOKEN. prefix. Remove it — it is added "
            "automatically, and doubling it causes a 401."
        )
    for key, label, required, _ in FIELDS:
        if required and not values.get(key):
            problems.append(f"{label} is required.")
    return problems


def store_path() -> tuple[Path, bool]:
    path, persistent = preferred_store()
    return Path(path), persistent


def save(values: dict[str, str]) -> tuple[Path, bool]:
    path, persistent = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = read_stored()
    existing.update({k: v for k, v in values.items() if v})

    lines = [
        "# Boomi Event Streams credentials.",
        "# Written by es_setup.py. Found from any working directory.",
        "# A ./.env in the directory you run from overrides these.",
        "",
    ]
    for key, _, _, _ in FIELDS:
        if existing.get(key):
            lines.append(f"{key}={existing[key]}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # The file holds a live credential. 0600 before anyone else can read it.
    # Some hosts refuse chmod on mounted folders; the write still succeeded, and
    # failing the whole save over file permissions would be the wrong trade.
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return path, persistent


def read_stored() -> dict[str, str]:
    """Read the first existing store, in the same order resolution uses."""
    for candidate, _ in credential_sources():
        path = Path(candidate)
        if not path.exists() or path.name == ".env":
            continue
        values = {}
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip()
        if values:
            return values
    return {}


def mask(value: str) -> str:
    if not value:
        return "(not set)"
    return value if len(value) <= 8 else f"{value[:4]}…{value[-4:]}"


def plugin_version() -> str:
    """Read the version from the manifest, so it is stated in one place only."""
    import json
    manifest = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".claude-plugin", "plugin.json",
    )
    try:
        with open(manifest, encoding="utf-8") as handle:
            return json.load(handle).get("version", "unknown")
    except OSError:
        return "unknown"


def cmd_check() -> int:
    print(f"# Boomi credential check  (plugin {plugin_version()})\n")
    for line in describe_sources():
        print(f"  {line}")

    config = Config()
    try:
        config.validate()
        print("\n  Credentials are present and well-formed.")
        print("  Run `es_setup.py --test` to confirm Boomi accepts them.")
        return 0
    except BoomiAuthError as exc:
        print(f"\n  Not usable yet:\n    {str(exc).splitlines()[0]}")
        if not any("connected folder" in label for _, label in credential_sources()):
            print(
                "\n  No folder from this machine is mounted in this conversation.\n"
                "  Stored credentials live in such a folder, and mounts do not carry\n"
                "  over between conversations -- so this is more likely an unmounted\n"
                "  folder than missing setup. Connect the folder and re-run --check\n"
                "  before setting anything up again."
            )
        print("\n  Fix with: es_setup.py --save --account-id ... --username ... --token ...")
        return 1


def cmd_test() -> int:
    try:
        client = build_client()
        from es_client import EventStreamsClient

        es = EventStreamsClient(client)
        environments = es.environments()
    except BoomiAuthError as exc:
        # Deliberately says nothing about whether anything is stored -- this runs both
        # before a save (nothing written yet) and standalone via --test (already
        # written). The caller knows which; this function does not.
        print(f"error: {exc}", file=sys.stderr)
        print("\nBoomi rejected these credentials. Check the token has not been "
              "revoked, and that the account ID matches the account the token "
              "belongs to.", file=sys.stderr)
        return 1
    except BoomiAPIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    provisioned = [e for e in environments if e.get("eventStreams")]
    print(f"Connected. {len(environments)} environment(s) visible, "
          f"{len(provisioned)} with Event Streams provisioned.\n")
    for env in environments:
        mark = "yes" if env.get("eventStreams") else "not provisioned"
        print(f"  {str(env.get('name')):<24} {mark}")

    protected = Config().protected
    print()
    if protected:
        print(f"  Protected from writes: {', '.join(protected)}")
        unlisted = [str(e.get("name")) for e in environments
                    if not Config().is_protected(e)]
        if unlisted:
            print(f"  NOT protected: {', '.join(unlisted)}")
    else:
        print("  No environments are protected. Every one of them can be written to "
              "and deleted from.")
        print("  Set protected environments with:")
        print("    es_setup.py --save --protected 'Production,Prod-EU'")
    return 0


def cmd_show() -> int:
    stored = read_stored()
    if not stored:
        print("Nothing stored. Looked in:")
        for candidate, label in credential_sources():
            print(f"  {candidate}  ({label})")
        return 1
    location = next((c for c, _ in credential_sources()
                     if Path(c).exists() and Path(c).name != ".env"), "unknown")
    print(f"# Stored at {location}\n")
    for key, label, _, _ in FIELDS:
        value = stored.get(key, "")
        shown = mask(value) if key == "BOOMI_API_TOKEN" else (value or "(not set)")
        print(f"  {label:<24} {shown}")
    return 0


def cmd_clear() -> int:
    removed = []
    for candidate, _ in credential_sources():
        path = Path(candidate)
        if not path.exists() or path.name == ".env":
            continue
        try:
            path.unlink()
        except OSError:
            # Some hosts block deletion inside mounted folders. Blanking the file
            # achieves the same thing -- credentials gone -- without failing.
            path.write_text("# cleared\n", encoding="utf-8")
        removed.append(str(path))
    if not removed:
        print("Nothing stored.")
        return 0
    for path in removed:
        print(f"Cleared {path}")
    print("Commands will now fail until credentials are set again, unless a project "
          "`.env` or shell variables supply them.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Store Boomi credentials once, for any directory.")
    p.add_argument("--check", action="store_true", help="Show where credentials come from.")
    p.add_argument("--test", action="store_true", help="Verify them against Boomi.")
    p.add_argument("--show", action="store_true", help="Show what is stored (token masked).")
    p.add_argument("--clear", action="store_true", help="Delete the stored credentials.")
    p.add_argument("--save", action="store_true", help="Write credentials to the store.")
    p.add_argument("--account-id")
    p.add_argument("--username")
    p.add_argument("--token")
    p.add_argument("--protected", help="Comma-separated environments refused for writes.")
    p.add_argument("--api-url", help="UK/EU accounts only.")
    p.add_argument("--force", action="store_true",
                   help="Save even if Boomi rejects the credentials. For when the "
                        "values are known good but Boomi is unreachable from here.")
    args = p.parse_args()

    if args.clear:
        return cmd_clear()
    if args.show:
        return cmd_show()

    if args.save:
        values = {
            "BOOMI_ACCOUNT_ID": (args.account_id or "").strip(),
            "BOOMI_USERNAME": (args.username or "").strip(),
            "BOOMI_API_TOKEN": (args.token or "").strip(),
            "BOOMI_PROTECTED_ENVIRONMENTS": (args.protected or "").strip(),
            "BOOMI_API_URL": (args.api_url or "").strip(),
        }
        merged = {**read_stored(), **{k: v for k, v in values.items() if v}}
        problems = validate(merged)
        if problems:
            print("Not saved — these need fixing first:\n", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1

        for key, label, _, _ in FIELDS:
            value = merged.get(key, "")
            shown = mask(value) if key == "BOOMI_API_TOKEN" else (value or "(not set)")
            print(f"  {label:<24} {shown}")

        # Refuse an ephemeral store rather than warning about one.
        #
        # This used to save anyway and print a warning. That produced the worst
        # outcome available: setup reported success, the credentials worked for the
        # rest of that conversation, and were gone by the next one -- so the failure
        # surfaced later, somewhere else, looking like the tool was broken rather
        # than like the warning nobody reads. A refusal here costs one minute; the
        # warning cost an afternoon.
        target, will_persist = store_path()
        if not will_persist and not args.force:
            print(
                f"\nRefusing to save. The only writable location here is {target},\n"
                "which is wiped when this conversation ends -- you would be asked to\n"
                "set up again next time, with nothing explaining why.\n",
                file=sys.stderr,
            )
            print(
                "Connect a folder from your machine first: it mounts at the same real\n"
                "path in every conversation, and setup will prefer it automatically.\n"
                "Or set BOOMI_ES_CONFIG to a path that survives.\n\n"
                "--force saves to the ephemeral location anyway, which is reasonable\n"
                "only for a one-off command you are running right now.",
                file=sys.stderr,
            )
            return 1

        # Verify BEFORE writing anything.
        #
        # The obvious order is save-then-check, and it is wrong: a mistyped token gets
        # persisted, every later command fails against it, and worse, a bad value
        # overwrites credentials that were previously working. Checking first means a
        # failed setup leaves whatever was already there untouched.
        print("\nChecking these against Boomi before saving anything...\n")
        sys.stdout.flush()

        for key, value in merged.items():
            if value:
                os.environ[key] = value

        if cmd_test() != 0:
            sys.stdout.flush()
            print(
                "\nNothing was saved. Boomi rejected these credentials, so writing "
                "them would only make every later command fail the same way.",
                file=sys.stderr,
            )
            if read_stored():
                print("Your existing stored credentials are untouched.", file=sys.stderr)
            print(
                "\nUsual causes: the token was revoked, or it belongs to a different "
                "account than the account ID given. Re-run --save with a corrected "
                "value.\nIf you are certain these are right and Boomi is simply "
                "unreachable from here, add --force to save without checking.",
                file=sys.stderr,
            )
            if not args.force:
                return 1
            print("\n--force given: saving unverified credentials anyway.", file=sys.stderr)

        path, persistent = save(values)
        print(f"\nSaved to {path}")
        if persistent:
            print("That location survives between conversations, so every future "
                  "chat will find these without asking again.")
        else:
            # Saying this plainly matters. The alternative is the user discovering
            # it tomorrow, when the tool asks them to set up all over again.
            print("\n  WARNING: this location does not survive between sessions, so "
                  "you will be asked to set up again next time.")
            print("  To keep them, connect a folder from your machine and re-run "
                  "setup, or set BOOMI_ES_CONFIG to a path that persists.")
        if not merged.get("BOOMI_PROTECTED_ENVIRONMENTS"):
            print("\nNo environments are protected. Nothing stops a delete against "
                  "production until you set that:")
            print("  es_setup.py --save --protected 'Production'")
        return 0

    if args.test:
        return cmd_test()
    return cmd_check()


if __name__ == "__main__":
    raise SystemExit(main())
