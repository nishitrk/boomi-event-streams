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
    import es_table as T

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

    lines += [
        T.section("Summary", T.table(
            ["Metric", "Value"],
            [
                ["Environments searched", len(all_envs)],
                ["Topics matched", len(hits["topics"])],
                ["Subscriptions matched", len(hits["subscriptions"])],
                ["Tokens matched", len(hits["tokens"])],
            ],
        )),
    ]

    # One row per environment for every distinct name found, present or not. The
    # rows saying "No" are the point of this tool: a topic in Test and absent from
    # Production is the shape of a promotion that never finished, and a table that
    # only listed the hits would hide exactly that.
    found_in: dict[tuple[str, str], list[str]] = {}
    for kind, entries in (("topic", hits["topics"]),
                          ("subscription", hits["subscriptions"]),
                          ("token", hits["tokens"])):
        for entry in entries:
            found_in.setdefault((kind, str(entry["name"])), []).append(entry["environment"])

    presence_rows = []
    for (kind, name) in sorted(found_in):
        for env in all_envs:
            presence_rows.append([env, kind, name, env in found_in[(kind, name)]])

    lines += [
        T.section("Presence by environment", T.table(
            ["Environment", "Kind", "Name", "Present"], presence_rows,
        )),
        "_A `No` here is the useful signal, not a gap in the search: it means the "
        "search ran against that environment and the entity is genuinely not there._",
        "",
    ]

    absent_anywhere = [
        (kind, name) for (kind, name), envs in sorted(found_in.items())
        if len(set(envs)) < len(all_envs)
    ]
    if absent_anywhere:
        for kind, name in absent_anywhere:
            envs = sorted(set(found_in[(kind, name)]), key=all_envs.index)
            lines.append(f"- `{name}` ({kind}) — {presence_line(name, envs, all_envs)}")
        lines.append("")
    else:
        lines += ["_Every match is present in every environment._", ""]

    if hits["topics"]:
        lines += [
            T.section(f"Topics ({len(hits['topics'])})", T.table(
                ["Topic", "Environment", "Subscriptions", "Description"],
                [
                    [h["name"], h["environment"], h["subscriptionCount"],
                     h.get("description")]
                    for h in sorted(hits["topics"],
                                    key=lambda h: (h["name"], h["environment"]))
                ],
            )),
        ]

    if hits["subscriptions"]:
        lines += [
            T.section(f"Subscriptions ({len(hits['subscriptions'])})", T.table(
                ["Subscription", "Topic", "Environment", "Type", "Backlog"],
                [
                    [h["name"], h["topic"], h["environment"], h.get("type"),
                     h.get("backlog")]
                    for h in sorted(hits["subscriptions"],
                                    key=lambda h: (h["name"], h["environment"]))
                ],
            )),
        ]

    if hits["tokens"]:
        lines += [
            T.section(f"Tokens ({len(hits['tokens'])})", T.table(
                ["Token", "Environment", "Produce", "Consume", "Expires"],
                [
                    [h["name"], h["environment"], T.yes_no(h["produce"]),
                     T.yes_no(h["consume"]), h["expiry"]]
                    for h in sorted(hits["tokens"],
                                    key=lambda h: (h["name"], h["environment"]))
                ],
            )),
        ]
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
