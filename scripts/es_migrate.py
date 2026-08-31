#!/usr/bin/env python3
"""
Migrate Event Streams entities between environments: plan, apply, verify.

    python es_migrate.py plan   --source Dev --target Test
    python es_migrate.py plan   --source Dev --target Test --topics orders,invoices
    python es_migrate.py apply  --plan es-migration-plan.json --confirm
    python es_migrate.py verify --source Dev --target Test

The three steps are separate commands on purpose. Planning is read-only and safe to
run repeatedly; applying is the only thing that writes, and it will not run without
both a plan file and an explicit --confirm. That separation means the thing a person
reviews is exactly the thing that executes -- no re-derivation between the decision
and the action.

Two safety properties are structural rather than advisory:

  * There is no delete path. This script can create topics, subscriptions and tokens.
    It cannot remove them, and neither can anything it imports.

  * Environments named in BOOMI_PROTECTED_ENVIRONMENTS are refused as a target, in
    code, before any write. A deny rule expressed as a phrase to avoid can be
    reworded around; a refusal in the write path cannot.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Any

from boomi_auth import BoomiAPIError, BoomiAuthError, build_client
from es_client import EventStreamsClient

DEFAULT_PLAN_PATH = "es-migration-plan.json"


class ProtectedEnvironmentError(RuntimeError):
    """The requested target is on the operator's denylist."""


def resolve(es: EventStreamsClient, wanted: str) -> dict[str, Any]:
    environments = es.environments()
    for env in environments:
        if env.get("id") == wanted:
            return env
    matches = [e for e in environments if str(e.get("name", "")).lower() == wanted.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(f"{m['name']} ({m['id']})" for m in matches)
        raise SystemExit(f"'{wanted}' matches more than one environment: {names}")
    available = ", ".join(str(e.get("name")) for e in environments) or "(none)"
    raise SystemExit(f"No environment named or numbered '{wanted}'. Available: {available}")


def guard_target(es: EventStreamsClient, target: dict[str, Any]) -> None:
    if es.client.config.is_protected(target):
        raise ProtectedEnvironmentError(
            f"'{target.get('name')}' is listed in BOOMI_PROTECTED_ENVIRONMENTS and "
            "cannot be used as a migration target.\n"
            "If this is genuinely intended, remove it from that list deliberately "
            "and re-run. It is set apart precisely so the decision is a separate, "
            "conscious act rather than a flag on a long command line."
        )


def wanted(names: list[str] | None) -> set[str] | None:
    """Normalise a comma-separated selection into a lowercase set, or None for all."""
    if not names:
        return None
    return {n.strip().lower() for n in names if n.strip()}


def build_plan(
    es: EventStreamsClient,
    source: dict[str, Any],
    target: dict[str, Any],
    only_topics: list[str] | None = None,
    include_tokens: bool = True,
    only_subscriptions: list[str] | None = None,
    only_tokens: list[str] | None = None,
) -> dict[str, Any]:
    """Work out what would need creating in the target, without changing anything.

    The three selection filters are independent so any subset can be moved. A
    subscription filter accepts either a bare name or `topic/name`, because the same
    subscription name legitimately appears under several topics and the bare form is
    what someone reading a report will have to hand.
    """
    sub_filter = wanted(only_subscriptions)
    token_filter = wanted(only_tokens)
    source_topics = es.topics(source["id"])
    target_topics = {str(t.get("name")) for t in es.topics(target["id"])}

    if only_topics:
        topic_filter = wanted(only_topics) or set()
        found = {str(t.get("name")).lower() for t in source_topics}
        missing = topic_filter - found
        if missing:
            raise SystemExit(
                f"These topics are not in {source['name']}: {', '.join(sorted(missing))}"
            )
        source_topics = [t for t in source_topics if str(t.get("name")).lower() in topic_filter]

    target_subs: dict[str, set[str]] = {}
    for topic in es.topics(target["id"]):
        target_subs[str(topic.get("name"))] = {
            str(s.get("name")) for s in (topic.get("subscriptions") or [])
        }

    topics_to_create: list[dict[str, Any]] = []
    subscriptions_to_create: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for topic in source_topics:
        name = str(topic.get("name"))
        if name in target_topics:
            skipped.append({"kind": "topic", "name": name, "reason": "already exists in target"})
        else:
            topics_to_create.append(
                {
                    "name": name,
                    "persistent": topic.get("persistent"),
                    # Carrying the source partition count is the whole reason this
                    # field is read. Omitting it silently creates single-partition
                    # topics, which changes ordering and throughput behaviour in
                    # ways that surface much later and are hard to trace back here.
                    "partitions": topic.get("partitions"),
                    "description": topic.get("description"),
                }
            )

        for sub in topic.get("subscriptions") or []:
            sub_name = str(sub.get("name"))
            if sub_filter is not None and not (
                sub_name.lower() in sub_filter
                or f"{name}/{sub_name}".lower() in sub_filter
            ):
                continue
            if sub_name in target_subs.get(name, set()):
                skipped.append(
                    {
                        "kind": "subscription",
                        "name": f"{name}/{sub_name}",
                        "reason": "already exists in target",
                    }
                )
                continue
            subscriptions_to_create.append(
                {
                    "topicName": name,
                    "name": sub_name,
                    "description": sub.get("description"),
                    "sourceType": sub.get("type"),
                }
            )

    tokens_to_create: list[dict[str, Any]] = []
    duplicate_source_tokens: list[str] = []
    if include_tokens:
        target_token_names = {str(t.get("name")) for t in es.tokens(target["id"])}
        source_tokens = es.tokens(source["id"])

        # Tokens are matched by name because that is the only thing meaningful
        # across environments -- IDs and JWT values are per-environment. But token
        # names are not unique, and several distinct credentials can share one.
        # Collapsing those silently would under-migrate, so count them and say so.
        name_counts = Counter(str(t.get("name")) for t in source_tokens)
        duplicate_source_tokens = sorted(n for n, c in name_counts.items() if c > 1)

        seen: set[str] = set()
        for token in source_tokens:
            name = str(token.get("name"))
            if token_filter is not None and name.lower() not in token_filter:
                continue
            if name in target_token_names:
                skipped.append({"kind": "token", "name": name, "reason": "already exists in target"})
                continue
            if name in seen:
                skipped.append(
                    {
                        "kind": "token",
                        "name": name,
                        "reason": (
                            f"duplicate name in source ({name_counts[name]} tokens share it); "
                            "only one will be created"
                        ),
                    }
                )
                continue
            seen.add(name)
            tokens_to_create.append(
                {
                    "name": name,
                    "allowProduce": bool(token.get("allowProduce")),
                    "allowConsume": bool(token.get("allowConsume")),
                    "expirationTime": token.get("expirationTime"),
                    "description": token.get("description"),
                }
            )

    return {
        "source": {"id": source["id"], "name": source["name"]},
        "target": {"id": target["id"], "name": target["name"]},
        "topics": topics_to_create,
        "subscriptions": subscriptions_to_create,
        "tokens": tokens_to_create,
        "skipped": skipped,
        "duplicateSourceTokenNames": duplicate_source_tokens,
    }


def plan_rows(plan: dict[str, Any]) -> list[list[Any]]:
    """One row per planned item: creates first, then what is already there.

    Detail carries the per-kind fields that do not deserve their own column —
    a topic's persistence and partition count, a token's permissions, and the
    reason a skipped item was skipped.
    """
    import es_table as T

    rows: list[list[Any]] = []
    for topic in plan["topics"]:
        rows.append([
            "topic", topic["name"], None, "create",
            f"persistent: {T.yes_no(topic.get('persistent'))}, partitions: "
            + (str(topic["partitions"]) if topic.get("partitions") is not None
               else "default"),
        ])
    for sub in plan["subscriptions"]:
        rows.append(["subscription", sub["name"], sub["topicName"], "create", None])
    for token in plan["tokens"]:
        rows.append([
            "token", token["name"], None, "create",
            f"produce: {T.yes_no(token['allowProduce'])}, "
            f"consume: {T.yes_no(token['allowConsume'])}",
        ])
    for item in plan["skipped"]:
        name = str(item["name"])
        parent = None
        if item["kind"] == "subscription" and "/" in name:
            parent, name = name.split("/", 1)
        rows.append([item["kind"], name, parent, "skip", item["reason"]])
    return rows


def render_plan(plan: dict[str, Any]) -> str:
    import es_table as T

    to_create = len(plan["topics"]) + len(plan["subscriptions"]) + len(plan["tokens"])
    lines = [
        f"# Migration plan — {plan['source']['name']} → {plan['target']['name']}",
        "",
        T.section("Summary", T.table(
            ["Metric", "Value"],
            [
                ["Topics to create", len(plan["topics"])],
                ["Subscriptions to create", len(plan["subscriptions"])],
                ["Tokens to create", len(plan["tokens"])],
                ["Already present, skipped", len(plan["skipped"])],
                ["Total to create", to_create],
            ],
        )),
        T.section(
            "Plan",
            T.numbered(
                ["Type", "Name", "Parent topic", "Action", "Detail"],
                plan_rows(plan),
                empty="_Nothing to create._",
            ),
        ),
        "_Action `skip` means the item already exists in the target and will not be "
        "touched. Nothing in this script deletes or overwrites._",
        "",
        f"_Parent topic is {T.DASH} for topics and tokens, which have none — it is "
        "not a missing value._",
        "",
    ]

    if plan["subscriptions"]:
        lines += [
            "> **On subscription type.** Subscriptions are created without a type and "
            "will report `NONE` until a consumer attaches; the broker assigns "
            "EXCLUSIVE / SHARED / FAILOVER / KEY_SHARED at that point. This is how the "
            "API behaves, not a partial migration.",
            "",
        ]
    if plan["tokens"]:
        lines += [
            "> **On tokens.** New tokens carry new JWT values. Connection components in "
            f"{plan['target']['name']} that referenced the old tokens must be updated "
            "by hand afterwards — the value cannot be copied across.",
            "",
        ]
    if plan.get("duplicateSourceTokenNames"):
        lines += [
            "> **Duplicate token names in the source.** "
            + ", ".join(f"`{n}`" for n in plan["duplicateSourceTokenNames"])
            + ". Tokens are matched across environments by name, since IDs and JWT "
            "values are per-environment — so only one token per name is created and "
            "the rest are listed as skipped above. If those duplicates are genuinely "
            "different credentials, create the extras by hand.",
            "",
        ]

    return "\n".join(lines)


def cmd_plan(args: argparse.Namespace, es: EventStreamsClient | None = None) -> int:
    # The client is injectable so these commands can be exercised without a live
    # account. Building it inside the function made the whole command layer
    # untestable, which is how the apply path went unverified longest.
    es = es or EventStreamsClient(build_client())
    source = resolve(es, args.source)
    target = resolve(es, args.target)

    if source["id"] == target["id"]:
        raise SystemExit("Source and target are the same environment.")

    # Checked at plan time as well as apply time so the refusal arrives before
    # anyone has invested attention in reviewing a plan that could never run.
    guard_target(es, target)

    plan = build_plan(
        es,
        source,
        target,
        only_topics=args.topics.split(",") if args.topics else None,
        include_tokens=not args.no_tokens,
        only_subscriptions=args.subscriptions.split(",") if args.subscriptions else None,
        only_tokens=args.tokens.split(",") if args.tokens else None,
    )

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(plan, handle, indent=2)

    print(render_plan(plan))
    total = len(plan["topics"]) + len(plan["subscriptions"]) + len(plan["tokens"])
    print(f"\nPlan written to {args.out} ({total} items to create).")
    if total:
        print(f"Review it, then: python es_migrate.py apply --plan {args.out} --confirm")
    return 0


def cmd_apply(args: argparse.Namespace, es: EventStreamsClient | None = None) -> int:
    import es_table as T

    with open(args.plan, "r", encoding="utf-8") as handle:
        plan = json.load(handle)

    es = es or EventStreamsClient(build_client())
    target = plan["target"]

    # Re-checked here against the live environment record rather than trusting the
    # plan file, so editing a plan by hand cannot route a write into a protected
    # environment.
    guard_target(es, resolve(es, target["id"]))

    if not args.confirm:
        print(render_plan(plan))
        print("\nNothing was changed. Re-run with --confirm to apply this plan.")
        return 0

    created: list[str] = []
    failed: list[str] = []
    # Rows for the summary table. Held alongside created/failed rather than derived
    # from them: those two carry free text, and re-parsing a sentence back into
    # columns is exactly the kind of step that quietly mangles a name with a slash
    # in it.
    results: list[list[Any]] = []

    for topic in plan["topics"]:
        try:
            es.create_topic(
                target["id"],
                topic["name"],
                persistent=topic.get("persistent"),
                partitions=topic.get("partitions"),
                description=topic.get("description"),
            )
            created.append(f"topic {topic['name']}")
            results.append(["topic", topic["name"], None, "created", None])
            print(f"  created topic {topic['name']}")
        except (BoomiAPIError, BoomiAuthError) as exc:
            failed.append(f"topic {topic['name']}: {exc}")
            results.append(["topic", topic["name"], None, "FAILED", str(exc)])
            print(f"  FAILED topic {topic['name']}: {exc}", file=sys.stderr)

    # Subscriptions are created after topics because a subscription cannot exist
    # without its topic. If a topic failed above, its subscriptions will fail too --
    # that is the correct outcome, and the summary makes the chain visible.
    for sub in plan["subscriptions"]:
        try:
            es.create_subscription(
                target["id"],
                sub["topicName"],
                sub["name"],
                description=sub.get("description"),
            )
            created.append(f"subscription {sub['topicName']}/{sub['name']}")
            results.append(
                ["subscription", sub["name"], sub["topicName"], "created", None]
            )
            print(f"  created subscription {sub['topicName']}/{sub['name']}")
        except (BoomiAPIError, BoomiAuthError) as exc:
            failed.append(f"subscription {sub['topicName']}/{sub['name']}: {exc}")
            results.append(
                ["subscription", sub["name"], sub["topicName"], "FAILED", str(exc)]
            )
            print(f"  FAILED subscription {sub['topicName']}/{sub['name']}: {exc}", file=sys.stderr)

    for token in plan["tokens"]:
        try:
            es.create_token(
                target["id"],
                token["name"],
                allow_consume=token["allowConsume"],
                allow_produce=token["allowProduce"],
                expiration_time=token.get("expirationTime"),
                description=token.get("description"),
            )
            created.append(f"token {token['name']}")
            results.append(["token", token["name"], None, "created", None])
            print(f"  created token {token['name']}")
        except (BoomiAPIError, BoomiAuthError) as exc:
            failed.append(f"token {token['name']}: {exc}")
            results.append(["token", token["name"], None, "FAILED", str(exc)])
            print(f"  FAILED token {token['name']}: {exc}", file=sys.stderr)

    # Read the target back. A create call returning 200 is not the same as the
    # entity existing afterwards -- on a real migration a token reported created
    # and then could not be found in the target, and nothing in the apply output
    # said so. Confirming here means the run reports what is true, not what was
    # requested.
    present = _read_back(es, target["id"])
    for row in results:
        kind, name, parent = row[0], row[1], row[2]
        if row[3] == "FAILED":
            row.insert(4, "—")
            continue
        key = (kind, f"{parent}/{name}" if kind == "subscription" else name)
        if present is None:
            row.insert(4, "unchecked")
        elif key in present:
            row.insert(4, "found")
        else:
            row.insert(4, "NOT FOUND")
            if row[3] == "created":
                row[3] = "created"
                failed.append(
                    f"{kind} {name}: create reported success but it is not in "
                    f"{target['name']}"
                )

    print()
    print(T.section(
        f"Applied — {plan['source']['name']} → {target['name']}",
        T.numbered(
            ["Type", "Name", "Parent topic", "Created", "Verified", "Detail"],
            results,
            empty="_The plan contained nothing to create._",
        ),
    ))
    print(
        "_Created is the result of the create call. Verified is a fresh read of the "
        "target afterwards — the two can disagree, and when they do the read is the "
        "one to believe._"
    )
    print(f"\nCreated {len(created)} item(s); {len(failed)} failed.")
    if plan["tokens"] and not failed:
        print(
            "Reminder: new tokens have new JWT values. Update any connection "
            f"components in {target['name']} that used the old ones."
        )
    print(
        f"\nVerify with: python es_migrate.py verify "
        f"--source {plan['source']['name']} --target {target['name']}"
    )
    return 1 if failed else 0


def _read_back(es: EventStreamsClient, environment_id: str) -> set | None:
    """What the target actually holds now, as {(kind, name)}.

    Returns None if the read itself fails, which renders as "unchecked" rather
    than "NOT FOUND" -- a failed verification query is not evidence of a missing
    entity, and reporting it as one would send someone hunting a phantom.
    """
    try:
        found = {("topic", str(t.get("name"))) for t in es.topics(environment_id)}
        for sub in es.subscriptions(environment_id):
            found.add(("subscription", f"{sub.get('topicName')}/{sub.get('name')}"))
        found |= {("token", str(t.get("name"))) for t in es.tokens(environment_id)}
        return found
    except (BoomiAPIError, BoomiAuthError):
        return None


def cmd_verify(args: argparse.Namespace, es: EventStreamsClient | None = None) -> int:
    import es_table as T

    es = es or EventStreamsClient(build_client())
    source = resolve(es, args.source)
    target = resolve(es, args.target)

    source_topics = {str(t.get("name")): t for t in es.topics(source["id"])}
    target_topics = {str(t.get("name")): t for t in es.topics(target["id"])}

    missing_topics = sorted(set(source_topics) - set(target_topics))
    # Held as (topic, field, source value, target value) rather than a formatted
    # sentence so the table can put each half in its own column. The comparisons
    # themselves are unchanged, and so is what counts as a difference.
    mismatched: list[tuple[str, str, Any, Any]] = []
    missing_subs: list[tuple[str, str]] = []

    for name, source_topic in source_topics.items():
        target_topic = target_topics.get(name)
        if not target_topic:
            continue
        if source_topic.get("partitions") != target_topic.get("partitions"):
            mismatched.append(
                (name, "partitions",
                 source_topic.get("partitions"), target_topic.get("partitions"))
            )
        if bool(source_topic.get("persistent")) != bool(target_topic.get("persistent")):
            mismatched.append(
                (name, "persistent",
                 source_topic.get("persistent"), target_topic.get("persistent"))
            )
        source_subs = {str(s.get("name")) for s in source_topic.get("subscriptions") or []}
        target_subs = {str(s.get("name")) for s in target_topic.get("subscriptions") or []}
        missing_subs.extend((name, s) for s in sorted(source_subs - target_subs))

    # Only claim to have checked what this account's schema actually exposes.
    # Saying "partition counts match" when the field does not exist here would be
    # a false assurance, and verification output is only useful if it is trusted.
    compared = [
        label
        for field, label in (("persistent", "persistence"), ("partitions", "partition counts"))
        # The selection prefix matters: it is how supports() consults the record of
        # what the executor actually refused, rather than only what introspection
        # advertised. Without it a rejected field would still be reported as compared.
        if es.supports(es.TOPICS, field, es.TOPIC_SEL)
    ]

    lines = [f"# Verification — {source['name']} → {target['name']}", ""]
    ok = not (missing_topics or missing_subs or mismatched)

    lines += [
        T.section("Summary", T.table(
            ["Metric", "Value"],
            [
                ["Topics in source", len(source_topics)],
                ["Missing topics", len(missing_topics)],
                ["Missing subscriptions", len(missing_subs)],
                ["Configuration differences", len(mismatched)],
                ["Fields compared", ", ".join(compared) if compared else None],
            ],
        )),
    ]

    if ok:
        detail = f" with matching {' and '.join(compared)}" if compared else ""
        lines.append(
            f"Every topic and subscription in {source['name']} is present in "
            f"{target['name']}{detail}."
        )
        lines.append("")
    else:
        rows: list[list[Any]] = []
        for name in missing_topics:
            rows.append(["topic", name, None,
                         f"missing from {target['name']}", "present", None])
        for topic_name, sub_name in missing_subs:
            rows.append(["subscription", sub_name, topic_name,
                         f"missing from {target['name']}", "present", None])
        for topic_name, field, source_value, target_value in mismatched:
            rows.append(["topic", topic_name, None, f"{field} differs",
                         source_value, target_value])
        lines += [
            T.section("Differences", T.numbered(
                ["Type", "Name", "Parent topic", "Difference", "Source", "Target"],
                rows,
            )),
            f"_A {T.DASH} in the Target column means the entity is not there at all, "
            "which is different from it being there with a different setting._",
            "",
        ]

    lines += [
        "",
        "_Subscription type is not compared: it is assigned by the broker when a "
        "consumer attaches, so it legitimately differs between a live source and a "
        "freshly migrated target._",
    ]
    if not compared:
        lines.append(
            "\n_This account's schema does not expose `persistent` or `partitions` "
            "on topics, so only presence was verified, not configuration. Run "
            "`es_schema.py` to see what is available here._"
        )
    print("\n".join(lines))
    return 0 if ok else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate Event Streams entities between environments.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="Compare environments and write a migration plan.")
    p.add_argument("--source", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--topics", help="Comma-separated topic names. Omit for all.")
    p.add_argument("--subscriptions",
                   help="Comma-separated subscription names, as 'name' or 'topic/name'.")
    p.add_argument("--tokens", help="Comma-separated token names.")
    p.add_argument("--no-tokens", action="store_true", help="Exclude tokens entirely.")
    p.add_argument("--out", default=DEFAULT_PLAN_PATH)
    p.set_defaults(func=cmd_plan)

    a = sub.add_parser("apply", help="Execute a reviewed plan. Requires --confirm.")
    a.add_argument("--plan", default=DEFAULT_PLAN_PATH)
    a.add_argument("--confirm", action="store_true", help="Actually write. Without it, dry run.")
    a.set_defaults(func=cmd_apply)

    v = sub.add_parser("verify", help="Compare two environments after a migration.")
    v.add_argument("--source", required=True)
    v.add_argument("--target", required=True)
    v.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    try:
        return args.func(args)
    except ProtectedEnvironmentError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    except (BoomiAuthError, BoomiAPIError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
