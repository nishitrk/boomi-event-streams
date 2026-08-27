#!/usr/bin/env python3
"""
Show what this account's Event Streams schema actually supports.

    python es_schema.py
    python es_schema.py --type EventStreamsTopic     # look up one type by name

Types are resolved by asking the schema what each field returns, not by guessing a
type name. That distinction is the whole point of this script: a Boomi schema can
define more than one type sharing a name, so `__type(name: "EventStreamsTopic")` may
return a type that has nothing to do with what `eventStreamsTopics` actually gives
you. When that happens, introspection reports fields the query validator rejects.

The RESOLVED line under each heading tells you which type is genuinely in play. If it
differs from what you expected, that mismatch is your bug.
"""

from __future__ import annotations

import argparse
import sys

from boomi_auth import BoomiAPIError, BoomiAuthError, build_client
from es_client import (
    OPTIONAL_ES_ENV_FIELDS,
    OPTIONAL_SUBSCRIPTION_FIELDS,
    OPTIONAL_TOKEN_FIELDS,
    OPTIONAL_TOPIC_FIELDS,
    REQUIRED_ES_ENV_FIELDS,
    REQUIRED_SUBSCRIPTION_FIELDS,
    REQUIRED_TOKEN_FIELDS,
    REQUIRED_TOPIC_FIELDS,
    EventStreamsClient,
)

PATHS = [
    ("Topics", EventStreamsClient.TOPICS, REQUIRED_TOPIC_FIELDS + OPTIONAL_TOPIC_FIELDS),
    ("Subscriptions", EventStreamsClient.SUBSCRIPTIONS,
     REQUIRED_SUBSCRIPTION_FIELDS + OPTIONAL_SUBSCRIPTION_FIELDS),
    ("Event Streams environment", EventStreamsClient.ES_ENVIRONMENT,
     REQUIRED_ES_ENV_FIELDS + OPTIONAL_ES_ENV_FIELDS),
    ("Tokens", EventStreamsClient.TOKENS, REQUIRED_TOKEN_FIELDS + OPTIONAL_TOKEN_FIELDS),
]

MUTATIONS = [
    ("eventStreamsTopicCreate", ["environmentId", "name", "persistent", "partitions", "description"]),
    ("eventStreamsSubscriptionCreate", ["environmentId", "topicName", "name", "description"]),
    ("eventStreamsTokenCreate",
     ["environmentId", "name", "allowConsume", "allowProduce", "expirationTime", "description"]),
]


def report(label: str, resolved: str | None, available: set[str], expected: list[str]) -> None:
    print(f"\n## {label}")
    if not resolved:
        print("  RESOLVED: could not resolve — this field may not exist on this account")
        return
    print(f"  RESOLVED TYPE: {resolved}")
    if not available:
        print("  (no fields readable)")
        return
    for name in expected:
        print(f"  {'yes' if name in available else 'NO ':<4} {name}")
    extra = sorted(available - set(expected))
    if extra:
        print(f"  -- also available: {', '.join(extra)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Show this account's Event Streams schema.")
    parser.add_argument("--type", help="Look up a single type by name instead.")
    args = parser.parse_args()

    try:
        es = EventStreamsClient(build_client())
        return _report(es, args)
    except BoomiAuthError as exc:
        # This script exists to answer "what does my account support", so an auth
        # failure must never be presented as an absent capability. Reporting a bad
        # token as a missing field is the most misleading answer available here.
        print(f"error: {exc}", file=sys.stderr)
        print(
            "\nThis is a credentials problem, not a schema one. Nothing above should "
            "be read as a statement about what your account supports.",
            file=sys.stderr,
        )
        return 1
    except BoomiAPIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _report(es: EventStreamsClient, args: argparse.Namespace) -> int:
    if args.type:
        fields = es.type_fields(args.type)
        print(f"\n## {args.type} (looked up by name)")
        for name in sorted(fields) or ["  (not defined, or no fields)"]:
            print(f"  {name}")
        return 0

    print("# Event Streams schema — as this account actually defines it")

    for label, path, expected in PATHS:
        report(label, es.resolve_type(path), es.fields_at(path), expected)

    print("\n# Mutation inputs")
    for mutation, expected in MUTATIONS:
        available = es.input_fields_for(mutation)
        print(f"\n## {mutation}")
        if not available:
            print("  could not resolve — mutation may not exist on this account")
            continue
        for name in expected:
            print(f"  {'yes' if name in available else 'NO ':<4} {name}")
        extra = sorted(available - set(expected))
        if extra:
            print(f"  -- also accepts: {', '.join(extra)}")

    print(
        "\nFields marked NO are not available here. The client omits them from every "
        "query and mutation automatically, so calls succeed — that data is simply "
        "not part of this account's schema."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
