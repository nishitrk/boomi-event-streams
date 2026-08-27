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
    provisioned = [e for e in inventory["environments"] if e["eventStreamsProvisioned"]]
    if len(provisioned) < 2:
        return ["_Fewer than two provisioned environments; nothing to compare._", ""]

    env_names = [e["name"] for e in provisioned]
    topics_by_env = {
        e["name"]: {str(t.get("name")) for t in (e.get("topics") or [])} for e in provisioned
    }
    all_topics = sorted(set().union(*topics_by_env.values())) if topics_by_env else []

    lines = ["| Topic | " + " | ".join(env_names) + " |",
             "| --- | " + " | ".join(["---"] * len(env_names)) + " |"]
    incomplete: list[str] = []
    for topic in all_topics:
        present = [topic in topics_by_env[name] for name in env_names]
        if any(present) and not all(present):
            incomplete.append(topic)
        lines.append(
            f"| `{topic}` | " + " | ".join("yes" if p else "—" for p in present) + " |"
        )
    lines.append("")

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
    lines = ["| Environment | Tokens | Expired | Expiring soon | Duplicate names |",
             "| --- | --- | --- | --- | --- |"]
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
        lines.append(
            f"| {env['name']} | {len(tokens)} | {expired or '—'} "
            f"| {expiring or '—'} | {duplicates or '—'} |"
        )
    lines.append("")
    if not any_problem:
        lines += ["No expired tokens, none expiring within 30 days, no duplicate names.", ""]
    return lines


def build(es: EventStreamsClient, client, environment: str | None,
          limit: int | None, quiet: bool) -> str:
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

    lines += [
        "## Summary",
        "",
        f"- Environments with Event Streams: **{len(provisioned)}**"
        + (f" (plus {len(not_provisioned)} without)" if not_provisioned else ""),
        f"- Topics: **{topic_total}**",
        f"- Subscriptions: **{sub_total}**",
        f"- Tokens: **{token_total}**",
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
        lines.append(f"- Region: {env.get('region') or 'unknown'}")
        lines.append(f"- Environment ID: `{env['id']}`")
        lines.append("")
        topics = env.get("topics") or []
        if topics:
            lines += ["| Topic | Subscriptions | Backlog |", "| --- | --- | --- |"]
            for topic in sorted(topics, key=lambda t: str(t.get("name"))):
                subs = topic.get("subscriptions") or []
                backlog = sum(s.get("backlogCount") or 0 for s in subs)
                names = ", ".join(str(s.get("name")) for s in subs) or "—"
                lines.append(f"| `{topic.get('name')}` | {names} | {backlog or '—'} |")
            lines.append("")
        else:
            lines += ["_No topics._", ""]

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
            lines += ["| Topic | Action | Process | Match |", "| --- | --- | --- | --- |"]
            for u in sorted(usages, key=lambda u: (str(u.get("topic")), str(u["processName"]))):
                lines.append(
                    f"| `{u.get('topic')}` | {u['action']} | {u['processName']} "
                    f"| {u.get('confidence')} |"
                )
            lines.append("")
        else:
            lines += [
                "_No process references found._ Run "
                "`es_topology.py --diagnose` to see which connector types exist here.",
                "",
            ]
            if census:
                lines += [
                    "Connector types present: "
                    + ", ".join(f"`{k}` ({v})" for k, v in sorted(census.items(), key=lambda kv: -kv[1])[:10]),
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
        for severity in ("high", "medium", "low"):
            group = [f for f in findings if f["severity"] == severity]
            if not group:
                continue
            lines += [f"### {severity.title()} ({len(group)})", ""]
            for finding in group:
                lines.append(
                    f"- **`{finding['subject']}` — {finding['finding']}.** {finding['detail']}"
                )
            lines.append("")
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
