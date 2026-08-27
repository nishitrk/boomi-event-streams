#!/usr/bin/env python3
"""
Inventory Event Streams entities across environments.

    python es_discover.py                       # every environment
    python es_discover.py --environment Test    # one, by name or ID
    python es_discover.py --json                # machine-readable

Environments without Event Streams provisioned are listed too, marked as such.
That absence is usually the answer to "why can't I see my topics" and hiding it
just moves the confusion further down the line.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from boomi_auth import BoomiAPIError, BoomiAuthError, build_client
from es_client import EventStreamsClient

# Warn this far ahead of expiry. A month is long enough to renew a token through
# whatever change process the environment requires, without crying wolf.
EXPIRY_WARNING_DAYS = 30


def expiry_state(expiration: str | None) -> tuple[str, str]:
    """Classify a token's expiry as (state, human label).

    An expired token in a live environment is silent until something tries to use
    it, at which point it looks like a broken integration rather than a lapsed
    credential. Surfacing it in a routine inventory is the cheapest place to catch
    it, so this is computed on every listing rather than only on request.
    """
    if not expiration:
        return ("ok", "never")
    try:
        cleaned = expiration.replace("Z", "+00:00")
        expires = datetime.fromisoformat(cleaned)
    except ValueError:
        return ("unknown", str(expiration))

    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    date_text = expires.date().isoformat()

    if expires <= now:
        days = (now - expires).days
        return ("expired", f"**EXPIRED** {date_text} ({days}d ago)")
    if expires - now <= timedelta(days=EXPIRY_WARNING_DAYS):
        return ("expiring", f"{date_text} (in {(expires - now).days}d)")
    return ("ok", date_text)


def resolve_environment(es: EventStreamsClient, wanted: str | None) -> str | None:
    """Accept an environment name or ID; return the ID."""
    if not wanted:
        return None
    environments = es.environments()
    for env in environments:
        if env.get("id") == wanted:
            return wanted
    matches = [e for e in environments if str(e.get("name", "")).lower() == wanted.lower()]
    if len(matches) == 1:
        return matches[0]["id"]
    if len(matches) > 1:
        names = ", ".join(f"{m['name']} ({m['id']})" for m in matches)
        raise SystemExit(f"'{wanted}' matches more than one environment: {names}")
    available = ", ".join(str(e.get("name")) for e in environments) or "(none found)"
    raise SystemExit(f"No environment named or numbered '{wanted}'. Available: {available}")


def render(inventory: dict) -> str:
    lines: list[str] = ["# Event Streams inventory", ""]

    # Surfaced at the top rather than in a footnote: if the list is incomplete,
    # everything below it is suspect and the reader needs to know before reading it.
    all_warnings = [
        w for env in inventory["environments"] for w in env.get("completenessWarnings") or []
    ]
    if all_warnings:
        lines.append("> **This inventory may be incomplete.**")
        lines += [f"> - {w}" for w in all_warnings]
        lines.append("")

    for env in inventory["environments"]:
        lines.append(f"## {env['name']}")
        lines.append("")
        lines.append(f"- Environment ID: `{env['id']}`")

        if not env["eventStreamsProvisioned"]:
            lines.append("- **Event Streams is not provisioned in this environment.**")
            lines.append("")
            continue

        lines.append(f"- Region: {env.get('region') or 'unknown'}")
        lines.append("")

        topics = env.get("topics") or []
        if topics:
            # Columns are built from the fields the account's schema actually
            # returned rather than a fixed list. Not every Boomi account defines
            # persistent or partitions on the topic type, and printing an empty
            # column for a concept that does not exist there reads as missing data
            # rather than as inapplicable.
            present = {key for topic in topics for key in topic if key != "subscriptions"}
            optional_columns = [
                ("persistent", "Persistent", lambda t: "yes" if t.get("persistent") else "no"),
                ("partitions", "Partitions", lambda t: str(t.get("partitions", "—"))),
                ("description", "Description", lambda t: str(t.get("description") or "—")),
            ]
            active = [c for c in optional_columns if c[0] in present]

            header = ["Topic"] + [label for _, label, _ in active] + ["Subscriptions"]
            lines.append(f"### Topics ({len(topics)})")
            lines.append("")
            lines.append("| " + " | ".join(header) + " |")
            lines.append("| " + " | ".join(["---"] * len(header)) + " |")
            for topic in sorted(topics, key=lambda t: str(t.get("name", ""))):
                subs = topic.get("subscriptions") or []
                sub_names = ", ".join(str(s.get("name")) for s in subs) or "—"
                row = (
                    [f"`{topic.get('name')}`"]
                    + [render_cell(topic) for _, _, render_cell in active]
                    + [sub_names]
                )
                lines.append("| " + " | ".join(row) + " |")
            lines.append("")

            subscription_rows = [
                (topic.get("name"), sub)
                for topic in topics
                for sub in (topic.get("subscriptions") or [])
            ]
            if subscription_rows:
                lines.append(f"### Subscriptions ({len(subscription_rows)})")
                lines.append("")
                lines.append("| Topic | Subscription | Type | Durable | Backlog |")
                lines.append("| --- | --- | --- | --- | --- |")
                for topic_name, sub in sorted(
                    subscription_rows, key=lambda r: (str(r[0]), str(r[1].get("name")))
                ):
                    lines.append(
                        f"| `{topic_name}` "
                        f"| `{sub.get('name')}` "
                        f"| {sub.get('type') or 'NONE'} "
                        f"| {'yes' if sub.get('durable') else 'no'} "
                        f"| {sub.get('backlogCount', 0)} |"
                    )
                lines.append("")
        else:
            lines.append("_No topics._")
            lines.append("")

        tokens = env.get("tokens") or []
        if tokens:
            lines.append(f"### Tokens ({len(tokens)})")
            lines.append("")
            lines.append("| Token | Produce | Consume | Expires |")
            lines.append("| --- | --- | --- | --- |")
            expired: list[str] = []
            expiring: list[str] = []
            for token in sorted(tokens, key=lambda t: str(t.get("name", ""))):
                state, label = expiry_state(token.get("expirationTime"))
                name = str(token.get("name"))
                if state == "expired":
                    expired.append(name)
                elif state == "expiring":
                    expiring.append(name)
                lines.append(
                    f"| `{name}` "
                    f"| {'yes' if token.get('allowProduce') else 'no'} "
                    f"| {'yes' if token.get('allowConsume') else 'no'} "
                    f"| {label} |"
                )
            lines.append("")

            if expired:
                lines.append(
                    f"**{len(expired)} expired token(s):** "
                    + ", ".join(f"`{n}`" for n in expired)
                    + ". Anything still referencing these is failing authentication "
                    "now — an expired token produces a connection failure, not a "
                    "warning, so this stays invisible until something breaks."
                )
                lines.append("")
            if expiring:
                lines.append(
                    f"**{len(expiring)} token(s) expiring within {EXPIRY_WARNING_DAYS} days:** "
                    + ", ".join(f"`{n}`" for n in expiring)
                    + "."
                )
                lines.append("")

            # Separate token records can share a name. That is legal but worth
            # surfacing: it makes the platform UI ambiguous, and any tooling that
            # identifies tokens by name -- including migration planning -- will
            # treat several distinct credentials as one.
            duplicates = [n for n, count in Counter(
                str(t.get("name")) for t in tokens
            ).items() if count > 1]
            if duplicates:
                lines.append(
                    "**Duplicate token names:** "
                    + ", ".join(f"`{n}`" for n in sorted(duplicates))
                    + ". These are distinct tokens sharing a name. Worth tidying — "
                    "it makes it impossible to tell from a connection component "
                    "which credential is actually in use."
                )
                lines.append("")

            # The JWT itself is available on the token object but is a live credential.
            # It is never printed -- anyone who needs it can read it in the platform UI.
            lines.append("_Token values are credentials and are not printed._")
            lines.append("")
        else:
            lines.append("_No tokens._")
            lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Inventory Boomi Event Streams entities.")
    parser.add_argument("--environment", help="Environment name or ID. Omit for all.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of markdown.")
    args = parser.parse_args()

    try:
        es = EventStreamsClient(build_client())
        environment_id = resolve_environment(es, args.environment)
        inventory = es.inventory(environment_id)
    except (BoomiAuthError, BoomiAPIError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(inventory, indent=2) if args.json else render(inventory))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
