#!/usr/bin/env python3
"""
Find a topic, subscription, or token by name across every environment.

    python es_find.py orders                     # substring, any kind
    python es_find.py SO_Producer --kind token
    python es_find.py --exact 01_SalesForce_Orders
    python es_find.py orders --json

The useful part is not "where does this exist" but "where doesn't it". A topic present
in Test and absent from Production is the shape of a promotion that never finished, so
every result lists both sides.
"""

from __future__ import annotations

import argparse
import json
import sys

from boomi_auth import BoomiAPIError, BoomiAuthError, build_client
from es_client import EventStreamsClient
from es_discover import expiry_state


def matches(needle: str, name: str, exact: bool) -> bool:
    return needle.lower() == name.lower() if exact else needle.lower() in name.lower()


def search(es: EventStreamsClient, needle: str, kind: str, exact: bool) -> dict:
    inventory = es.inventory()
    hits = {"topics": [], "subscriptions": [], "tokens": []}
    all_envs = [e["name"] for e in inventory["environments"]]

    for env in inventory["environments"]:
        env_name = env["name"]

        if kind in ("any", "topic"):
            for topic in env.get("topics") or []:
                name = str(topic.get("name"))
                if matches(needle, name, exact):
                    hits["topics"].append({
                        "name": name,
                        "environment": env_name,
                        "description": topic.get("description"),
                        "subscriptionCount": len(topic.get("subscriptions") or []),
                    })

        if kind in ("any", "subscription"):
            for topic in env.get("topics") or []:
                for sub in topic.get("subscriptions") or []:
                    name = str(sub.get("name"))
                    if matches(needle, name, exact):
                        hits["subscriptions"].append({
                            "name": name,
                            "topic": str(topic.get("name")),
                            "environment": env_name,
                            "type": sub.get("type"),
                            "backlog": sub.get("backlogCount"),
                        })

        if kind in ("any", "token"):
            for token in env.get("tokens") or []:
                name = str(token.get("name"))
                if matches(needle, name, exact):
                    state, label = expiry_state(token.get("expirationTime"))
                    hits["tokens"].append({
                        "name": name,
                        "environment": env_name,
                        "produce": bool(token.get("allowProduce")),
                        "consume": bool(token.get("allowConsume")),
                        "expiry": label,
                        "expiryState": state,
                    })

    return {"query": needle, "environments": all_envs, "hits": hits}


def presence_line(name: str, found_in: list[str], all_envs: list[str]) -> str:
    missing = [e for e in all_envs if e not in found_in]
    text = f"present in {', '.join(found_in)}"
    if missing:
        text += f" — **absent from {', '.join(missing)}**"
    return text


def render(result: dict) -> str:
    needle = result["query"]
    all_envs = result["environments"]
    hits = result["hits"]
    total = sum(len(v) for v in hits.values())

    lines = [f"# Search: `{needle}`", ""]
    if not total:
        lines.append(
            f"No topic, subscription, or token matching `{needle}` in any of "
            f"{len(all_envs)} environment(s): {', '.join(all_envs)}."
        )
        return "\n".join(lines)

    if hits["topics"]:
        lines += [f"## Topics ({len(hits['topics'])})", ""]
        by_name: dict[str, list[dict]] = {}
        for hit in hits["topics"]:
            by_name.setdefault(hit["name"], []).append(hit)
        for name, group in sorted(by_name.items()):
            envs = [g["environment"] for g in group]
            lines.append(f"### `{name}`")
            lines.append("")
            lines.append(f"- {presence_line(name, envs, all_envs)}")
            for entry in sorted(group, key=lambda g: g["environment"]):
                lines.append(
                    f"- {entry['environment']}: {entry['subscriptionCount']} subscription(s)"
                    + (f" — {entry['description']}" if entry.get("description") else "")
                )
            lines.append("")

    if hits["subscriptions"]:
        lines += [f"## Subscriptions ({len(hits['subscriptions'])})", "",
                  "| Subscription | Topic | Environment | Type | Backlog |",
                  "| --- | --- | --- | --- | --- |"]
        for hit in sorted(hits["subscriptions"], key=lambda h: (h["name"], h["environment"])):
            lines.append(
                f"| `{hit['name']}` | `{hit['topic']}` | {hit['environment']} "
                f"| {hit.get('type') or '—'} | {hit.get('backlog', 0)} |"
            )
        lines.append("")

    if hits["tokens"]:
        lines += [f"## Tokens ({len(hits['tokens'])})", "",
                  "| Token | Environment | Produce | Consume | Expires |",
                  "| --- | --- | --- | --- | --- |"]
        for hit in sorted(hits["tokens"], key=lambda h: (h["name"], h["environment"])):
            lines.append(
                f"| `{hit['name']}` | {hit['environment']} "
                f"| {'yes' if hit['produce'] else 'no'} "
                f"| {'yes' if hit['consume'] else 'no'} | {hit['expiry']} |"
            )
        lines.append("")
        expired = [h["name"] for h in hits["tokens"] if h["expiryState"] == "expired"]
        if expired:
            lines.append(
                "**Expired:** " + ", ".join(f"`{n}`" for n in sorted(set(expired)))
                + ". Anything still using these is failing authentication."
            )
            lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find an Event Streams topic, subscription, or token by name."
    )
    parser.add_argument("needle", help="Name or part of a name to search for.")
    parser.add_argument("--kind", choices=["any", "topic", "subscription", "token"],
                        default="any", help="Restrict the search. Default: any.")
    parser.add_argument("--exact", action="store_true", help="Require a full name match.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        es = EventStreamsClient(build_client())
        result = search(es, args.needle, args.kind, args.exact)
    except (BoomiAuthError, BoomiAPIError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
