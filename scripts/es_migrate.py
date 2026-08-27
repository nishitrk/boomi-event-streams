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


def render_plan(plan: dict[str, Any]) -> str:
    lines = [
        f"# Migration plan — {plan['source']['name']} → {plan['target']['name']}",
        "",
    ]

    def section(title: str, rows: list[str]) -> None:
        lines.append(f"## {title} ({len(rows)})")
        lines.append("")
        lines.extend(rows or ["_Nothing to create._"])
        lines.append("")

    section(
        "Topics to create",
        [
            f"- `{t['name']}` — persistent: {t.get('persistent')}, "
            f"partitions: {t.get('partitions') if t.get('partitions') is not None else 'default'}"
            for t in plan["topics"]
        ],
    )
    section(
        "Subscriptions to create",
        [f"- `{s['topicName']}` / `{s['name']}`" for s in plan["subscriptions"]],
    )
    section(
        "Tokens to create",
        [
            f"- `{t['name']}` — produce: {t['allowProduce']}, consume: {t['allowConsume']}"
            for t in plan["tokens"]
        ],
    )

    if plan["skipped"]:
        lines.append(f"## Already present, will be skipped ({len(plan['skipped'])})")
        lines.append("")
        for item in plan["skipped"]:
            lines.append(f"- {item['kind']}: `{item['name']}` — {item['reason']}")
        lines.append("")

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
            print(f"  created topic {topic['name']}")
        except (BoomiAPIError, BoomiAuthError) as exc:
            failed.append(f"topic {topic['name']}: {exc}")
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
            print(f"  created subscription {sub['topicName']}/{sub['name']}")
        except (BoomiAPIError, BoomiAuthError) as exc:
            failed.append(f"subscription {sub['topicName']}/{sub['name']}: {exc}")
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
            print(f"  created token {token['name']}")
        except (BoomiAPIError, BoomiAuthError) as exc:
            failed.append(f"token {token['name']}: {exc}")
            print(f"  FAILED token {token['name']}: {exc}", file=sys.stderr)

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


def cmd_verify(args: argparse.Namespace, es: EventStreamsClient | None = None) -> int:
    es = es or EventStreamsClient(build_client())
    source = resolve(es, args.source)
    target = resolve(es, args.target)

    source_topics = {str(t.get("name")): t for t in es.topics(source["id"])}
    target_topics = {str(t.get("name")): t for t in es.topics(target["id"])}

    missing_topics = sorted(set(source_topics) - set(target_topics))
    mismatched: list[str] = []
    missing_subs: list[str] = []

    for name, source_topic in source_topics.items():
        target_topic = target_topics.get(name)
        if not target_topic:
            continue
        if source_topic.get("partitions") != target_topic.get("partitions"):
            mismatched.append(
                f"`{name}` partitions: source {source_topic.get('partitions')}, "
                f"target {target_topic.get('partitions')}"
            )
        if bool(source_topic.get("persistent")) != bool(target_topic.get("persistent")):
            mismatched.append(
                f"`{name}` persistent: source {source_topic.get('persistent')}, "
                f"target {target_topic.get('persistent')}"
            )
        source_subs = {str(s.get("name")) for s in source_topic.get("subscriptions") or []}
        target_subs = {str(s.get("name")) for s in target_topic.get("subscriptions") or []}
        missing_subs.extend(f"`{name}` / `{s}`" for s in sorted(source_subs - target_subs))

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

    if ok:
        detail = f" with matching {' and '.join(compared)}" if compared else ""
        lines.append(
            f"Every topic and subscription in {source['name']} is present in "
            f"{target['name']}{detail}."
        )
    else:
        if missing_topics:
            lines += [f"## Missing topics ({len(missing_topics)})", ""]
            lines += [f"- `{name}`" for name in missing_topics] + [""]
        if missing_subs:
            lines += [f"## Missing subscriptions ({len(missing_subs)})", ""]
            lines += [f"- {entry}" for entry in missing_subs] + [""]
        if mismatched:
            lines += [f"## Configuration differences ({len(mismatched)})", ""]
            lines += [f"- {entry}" for entry in mismatched] + [""]

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
