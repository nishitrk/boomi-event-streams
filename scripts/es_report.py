#!/usr/bin/env python3
"""
One combined Event Streams report: inventory, drift, topology, and health.

    python es_report.py                                  # all environments
    python es_report.py --environment Test               # topology for Test too
    python es_report.py --environment Test --limit 50
    python es_report.py --out report.md

The cross-environment drift matrix is the part worth reading first. It puts every
topic against every environment in one grid, which is the fastest way to see a
promotion that stopped halfway — the pattern that produces orphaned operations later.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from boomi_auth import BoomiAPIError, BoomiAuthError, build_client
from es_client import EventStreamsClient
from es_discover import expiry_state
from es_inspect import analyse, find_operations, map_processes


def drift_matrix(inventory: dict) -> list[str]:
    import es_table as T

    provisioned = [e for e in inventory["environments"] if e["eventStreamsProvisioned"]]
    if len(provisioned) < 2:
        return ["_Fewer than two provisioned environments; nothing to compare._", ""]

    env_names = [e["name"] for e in provisioned]
    topics_by_env = {
        e["name"]: {str(t.get("name")) for t in (e.get("topics") or [])} for e in provisioned
    }
    all_topics = sorted(set().union(*topics_by_env.values())) if topics_by_env else []

    rows = []
    incomplete: list[str] = []
    for topic in all_topics:
        present = [topic in topics_by_env[name] for name in env_names]
        if any(present) and not all(present):
            incomplete.append(topic)
        rows.append([topic, *present])

    lines = [
        T.table(["Topic", *env_names], rows,
                empty="_No topics in any provisioned environment._"),
        "",
        "_`No` here is a definite absence, not an unknown: every provisioned "
        "environment was queried._",
        "",
    ]

    if incomplete:
        lines += [
            f"**{len(incomplete)} topic(s) exist in some environments but not others:** "
            + ", ".join(f"`{t}`" for t in incomplete)
            + ".",
            "",
            "Some of this is intentional — POC and test topics rarely belong in "
            "production. The ones worth checking are topics a process already "
            "references in an environment where the topic is missing, which show up "
            "as orphaned operations in the health section below.",
            "",
        ]
    else:
        lines += ["All topics are present in every provisioned environment.", ""]
    return lines


def token_summary(inventory: dict) -> list[str]:
    import es_table as T

    rows = []
    any_problem = False
    for env in inventory["environments"]:
        tokens = env.get("tokens") or []
        if not tokens:
            continue
        states = [expiry_state(t.get("expirationTime"))[0] for t in tokens]
        expired = states.count("expired")
        expiring = states.count("expiring")
        names = [str(t.get("name")) for t in tokens]
        duplicates = len(names) - len(set(names))
        if expired or expiring or duplicates:
            any_problem = True
        rows.append([env["name"], len(tokens), expired, expiring, duplicates])

    lines = [
        T.table(
            ["Environment", "Tokens", "Expired", "Expiring soon", "Duplicate names"],
            rows,
            empty="_No tokens in any environment._",
        ),
        "",
    ]
    if any_problem:
        expired_names = sorted({
            str(t.get("name"))
            for env in inventory["environments"]
            for t in (env.get("tokens") or [])
            if expiry_state(t.get("expirationTime"))[0] == "expired"
        })
        if expired_names:
            lines += [
                "**Expired:** " + ", ".join(f"`{n}`" for n in expired_names)
                + ". An expired token produces a connection failure rather than a "
                "warning, so anything still referencing these is already failing.",
                "",
            ]
    else:
        lines += ["No expired tokens, none expiring within 30 days, no duplicate names.", ""]
    return lines


def build(es: EventStreamsClient, client, environment: str | None,
          limit: int | None, quiet: bool) -> str:
    import es_table as T

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    inventory = es.inventory()

    lines = [
        "# Event Streams report",
        "",
        f"Generated {generated} for account `{es.client.config.account_id}`.",
        "",
    ]

    provisioned = [e for e in inventory["environments"] if e["eventStreamsProvisioned"]]
    not_provisioned = [e for e in inventory["environments"] if not e["eventStreamsProvisioned"]]
    topic_total = sum(len(e.get("topics") or []) for e in provisioned)
    sub_total = sum(
        len(t.get("subscriptions") or []) for e in provisioned for t in (e.get("topics") or [])
    )
    token_total = sum(len(e.get("tokens") or []) for e in provisioned)

    # Surfaced above the tables rather than in them: if any environment's list came
    # back short, every count and every drift row below it is suspect.
    all_warnings = [
        w for env in inventory["environments"] for w in env.get("completenessWarnings") or []
    ]
    if all_warnings:
        lines.append("> **This report may be built on an incomplete inventory.**")
        lines += [f"> - {w}" for w in all_warnings]
        lines.append("")

    lines += [
        "## Summary",
        "",
        f"Environments with Event Streams: **{len(provisioned)}**"
        + (f" (plus {len(not_provisioned)} without)" if not_provisioned else "")
        + ".",
        "",
        T.table(
            ["Environment", "Topics", "Subscriptions", "Tokens"],
            [
                [
                    env["name"],
                    len(env.get("topics") or []),
                    sum(len(t.get("subscriptions") or []) for t in env.get("topics") or []),
                    len(env.get("tokens") or []),
                ]
                if env["eventStreamsProvisioned"]
                # None, not 0: there is no Event Streams here to count, which is a
                # different statement from "there is one and it is empty".
                else [env["name"], None, None, None]
                for env in inventory["environments"]
            ]
            + [["**Total**", topic_total, sub_total, token_total]],
        ),
        "",
        f"_{T.DASH} means Event Streams is not provisioned in that environment; the "
        "total counts only the provisioned ones._",
        "",
    ]

    lines += ["## Cross-environment drift", ""]
    lines += drift_matrix(inventory)

    lines += ["## Tokens", ""]
    lines += token_summary(inventory)

    lines += ["## Inventory by environment", ""]
    for env in inventory["environments"]:
        lines.append(f"### {env['name']}")
        lines.append("")
        if not env["eventStreamsProvisioned"]:
            lines += ["_Event Streams is not provisioned here._", ""]
            continue
        lines.append(
            f"_Environment ID `{env['id']}` {T.DASH} region "
            f"{env.get('region') or 'unknown'}._"
        )
        lines.append("")
        topics = env.get("topics") or []
        lines += [
            T.table(
                ["Topic", "Subscriptions", "Backlog"],
                [
                    [
                        topic.get("name"),
                        [str(s.get("name")) for s in (topic.get("subscriptions") or [])],
                        sum(s.get("backlogCount") or 0
                            for s in (topic.get("subscriptions") or [])) or None,
                    ]
                    for topic in sorted(topics, key=lambda t: str(t.get("name")))
                ],
                empty="_No topics._",
            ),
            "",
        ]

    # Topology is scoped to one environment because the scan is expensive and a
    # process-to-topic map is only meaningful against a specific environment's topics.
    usages: list[dict] = []
    if environment:
        from es_discover import resolve_environment

        env_id = resolve_environment(es, environment)
        env_name = next((e["name"] for e in es.environments() if e["id"] == env_id), environment)
        topics = es.topics(env_id)
        tokens = es.tokens(env_id)

        if not quiet:
            print(f"Scanning components for topology ({env_name})...", file=sys.stderr)
        operations, census = find_operations(
            client, {str(t.get("name")) for t in topics}, limit, verbose=not quiet
        )
        usages = map_processes(client, operations, limit, verbose=not quiet)

        lines += [f"## Topology — {env_name}", ""]
        if usages:
            lines += [
                T.table(
                    ["Topic", "Action", "Process", "Match"],
                    [
                        [u.get("topic"), u["action"], u["processName"], u.get("confidence")]
                        for u in sorted(
                            usages,
                            key=lambda u: (str(u.get("topic")), str(u["processName"])),
                        )
                    ],
                ),
                "",
                "_Match is how the topic was identified in the operation's XML, not a "
                "measure of how healthy the link is. Run `es_topology.py` for the "
                "deployment state and the full per-topic breakdown._",
                "",
            ]
        else:
            lines += [
                "_No process references found._ Run "
                "`es_topology.py --diagnose` to see which connector types exist here.",
                "",
            ]
            if census:
                lines += [
                    T.counts("Connector type", sorted(
                        census.items(), key=lambda kv: -kv[1])[:10]),
                    "",
                ]

        findings = analyse(topics, usages, tokens)
        lines += [f"## Health — {env_name} ({len(findings)} finding(s))", ""]
    else:
        all_topics = [t for e in provisioned for t in (e.get("topics") or [])]
        all_tokens = [t for e in provisioned for t in (e.get("tokens") or [])]
        findings = analyse(all_topics, [], all_tokens)
        lines += [
            f"## Health — all environments ({len(findings)} finding(s))",
            "",
            "_Pass `--environment NAME` to include the process-to-topic map and the "
            "orphaned-operation check, which need a component scan._",
            "",
        ]

    if findings:
        lines += [T.severity_summary(findings), "", T.wrap_findings(findings), ""]
    else:
        lines += ["No issues found.", ""]

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Combined Event Streams report.")
    parser.add_argument("--environment",
                        help="Include the topology and orphan check for this environment.")
    parser.add_argument("--limit", type=int, help="Cap components scanned for topology.")
    parser.add_argument("--out", help="Write to a file instead of stdout.")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    try:
        client = build_client()
        es = EventStreamsClient(client)
        report = build(es, client, args.environment, args.limit, args.quiet)
    except (BoomiAuthError, BoomiAPIError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(report + "\n")
        print(f"Report written to {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
