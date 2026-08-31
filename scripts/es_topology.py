#!/usr/bin/env python3
"""
Map which processes talk to which Event Streams topics, and check health.

    python es_topology.py --environment Test
    python es_topology.py --environment Test --skip-processes   # fast, topics only
    python es_topology.py --environment Test --limit 50         # bound the scan
    python es_topology.py --environment Test --diagnose --skip-processes  # why found nothing
    python es_topology.py --environment Test --json

The detection logic lives in es_inspect.py. Read the note at the top of that file for
why topic names, not connector types, are the primary signal.

Cost: building the map reads component XML for every operation and process in the
account, which on a large account is a lot of calls. Results cache under .es-cache/
keyed by component ID and version, so a second run is fast and changed components are
re-read. Use --skip-processes when the question is about topic health rather than
which processes are involved.
"""

from __future__ import annotations

import argparse
import json
import sys

from boomi_auth import BoomiAPIError, BoomiAuthError, build_client
from es_client import EventStreamsClient
from es_inspect import connector_census, analyse, find_operations, map_processes


def render(
    env_name: str,
    topics: list[dict],
    usages: list[dict],
    findings: list[dict],
    census: dict[str, int] | None,
    diagnose: bool,
) -> str:
    import es_table as T

    lines = [f"# Event Streams topology — {env_name}", ""]

    if usages:
        deployed = [u for u in usages if u.get("deployed")]
        orphaned = [u for u in usages if not u.get("processName")]
        lines += [
            T.section("Summary", T.table(
                ["Metric", "Value"],
                [
                    ["Topics referenced", len({u.get("topic") for u in usages if u.get("topic")})],
                    ["Operations", len(usages)],
                    ["Deployed", f"{len(deployed)} of {len(usages)}"],
                    ["Orphaned operations", len(orphaned)],
                ],
            )),
            T.section("Operations by action",
                      T.counts("Action", T.summarise(usages, "action"))),
        ]

        # Grouped by topic, because the question people arrive with is almost always
        # "what touches this topic", not "what does this process do".
        by_topic: dict[str, list[dict]] = {}
        for usage in usages:
            by_topic.setdefault(str(usage.get("topic") or "(dynamic)"), []).append(usage)

        lines += ["## Topic to process map", ""]
        for topic in sorted(by_topic):
            rows = sorted(by_topic[topic], key=lambda u: (u["action"], str(u.get("processName"))))
            live = sum(1 for r in rows if r.get("deployed"))
            state = ("DEPLOYED" if live == len(rows)
                     else "NOT DEPLOYED" if live == 0 else "PARTIALLY DEPLOYED")
            body = []
            for r in rows:
                dep, envs = T.status(r.get("deployed"), r.get("environments"))
                body.append([
                    r.get("subscription"),
                    r.get("subscriptionType"),
                    r["action"],
                    r.get("processName") or "⚠️ no parent process",
                    T.yes_no(r.get("transacted")),
                    T.yes_no(r.get("fromDeadLetter")),
                    dep,
                    envs,
                ])
            lines += [
                f"### `{topic}` — {state}",
                "",
                T.table(
                    ["Subscription", "Type", "Operation", "Process",
                     "Transacted", "From DL", "Deployed", "Environment"],
                    body,
                ),
                "",
            ]

        lines += [
            "_Type and Subscription are what the **operation declares**; "
            "`es_discover` reports what the **broker** has, which stays `NONE` until a "
            "consumer attaches. The two disagreeing is normal, and the gap is "
            "informative: it separates intent from what has actually connected._",
            "",
            "_Deployed is a property of the process, not the operation. An operation "
            "with no parent process can never be deployed._",
            "",
        ]
    else:
        lines += [
            "## Topic to process map",
            "",
            "_No process references found._",
            "",
            "Either no process in this account uses Event Streams, the scan was "
            "limited or skipped, or the operations were not recognised. Run with "
            "`--diagnose --skip-processes` to see which connector types this account "
            "actually has.",
            "",
        ]

    if diagnose and census:
        total = sum(census.values())
        lines += [
            f"## Connector types in this account ({len(census)} types, "
            f"{total} operations)",
            "",
            "_Complete — this census covers every connector operation in the account, "
            "not just the ones read during the scan._",
            "",
            T.table(["subType", "Operations"],
                    sorted(census.items(), key=lambda kv: -kv[1])),
            "",
        ]

        from es_inspect import connector_hints

        looks_like_es = [s for s in census if any(h in s.lower() for h in connector_hints())]
        if looks_like_es:
            lines += [
                "Connector type(s) that look like Event Streams: "
                + ", ".join(f"`{s}`" for s in looks_like_es)
                + ". If nothing matched despite that, the operations may set their "
                "topic at runtime, or the scan was limited before reaching them.",
                "",
            ]
        else:
            lines += [
                "**No connector type here looks like Event Streams.** That is the "
                "likely answer: the processes in this account may reach Event Streams "
                "some other way, or the operations live in an account this token "
                "cannot see. If you know the connector's subType, add it to "
                "`BOOMI_ES_CONNECTOR_TYPES` in `.env` and re-run.",
                "",
            ]

    lines += [
        f"## Health findings ({len(findings)})",
        "",
        T.severity_summary(findings) if findings else "No issues found.",
        "",
    ]
    if findings:
        lines += [T.wrap_findings(findings), ""]

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Map Event Streams topology and check health.")
    parser.add_argument("--environment", required=True,
                        help="Environment name or ID. Required — the map is only "
                             "meaningful against one environment's topics.")
    parser.add_argument("--topic",
                        help="Show only this topic (substring match). The scan cost is "
                             "the same; this filters the output.")
    parser.add_argument("--limit", type=int, help="Cap how many components are scanned.")
    parser.add_argument("--skip-processes", action="store_true",
                        help="Skip the process scan. Much faster; no topic-to-process map.")
    parser.add_argument("--diagnose", action="store_true",
                        help="Also list every connector type in the account. Pair with --skip-processes: the census is one cheap query and does not need the scan.")
    parser.add_argument("--exhaustive", action="store_true",
                        help="Read every connector operation, not just likely ones. "
                             "Slow; only needed if a targeted scan finds nothing.")
    parser.add_argument("--no-reference-api", action="store_true",
                        help="Skip the dependency-graph lookup and scan process XML.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    try:
        client = build_client()
        es = EventStreamsClient(client)

        from es_discover import resolve_environment

        environment_id = resolve_environment(es, args.environment)
        env_name = next(
            (e["name"] for e in es.environments() if e["id"] == environment_id),
            args.environment,
        )
        topics = es.topics(environment_id)
        tokens = es.tokens(environment_id)
        known = {str(t.get("name")) for t in topics}

        usages: list[dict] = []
        census: dict[str, int] = {}
        if not args.skip_processes:
            if not args.quiet:
                print("Finding Event Streams operations...", file=sys.stderr)
            operations, census = find_operations(
                client, known, args.limit, verbose=not args.quiet,
                exhaustive=args.exhaustive,
            )
            if not args.quiet:
                print(f"  matched {len(operations)} operation(s)", file=sys.stderr)
            usages = map_processes(
                client, operations, args.limit, verbose=not args.quiet,
                use_reference_api=not args.no_reference_api,
                debug=args.diagnose,
            )
        elif args.diagnose:
            # --diagnose used to yield nothing at all here, because the census was
            # only built as a side effect of the scan that --skip-processes skips.
            # The census does not need that scan, so take it directly.
            if not args.quiet:
                print("Taking the connector census...", file=sys.stderr)
            census = connector_census(client)

        findings = analyse(topics, usages, tokens)

        # Filtering happens after analysis, not before: health findings are computed
        # against the whole environment so a filtered view still reflects reality,
        # and only the presentation narrows.
        if args.topic:
            needle = args.topic.lower()
            matched_topics = [t for t in topics if needle in str(t.get("name", "")).lower()]
            if not matched_topics and not any(
                needle in str(u.get("topic", "")).lower() for u in usages
            ):
                available = ", ".join(sorted(str(t.get("name")) for t in topics)) or "(none)"
                print(
                    f"No topic matching '{args.topic}' in {env_name}.\n"
                    f"Topics here: {available}",
                    file=sys.stderr,
                )
                return 1
            usages = [u for u in usages if needle in str(u.get("topic", "")).lower()]
            findings = [f for f in findings if needle in str(f.get("subject", "")).lower()]
            topics = matched_topics
            env_name = f"{env_name} — topic '{args.topic}'"
    except (BoomiAuthError, BoomiAPIError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({
            "environment": env_name, "topics": topics, "usages": usages,
            "findings": findings, "connectorCensus": census,
        }, indent=2))
    else:
        print(render(env_name, topics, usages, findings, census, args.diagnose))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
