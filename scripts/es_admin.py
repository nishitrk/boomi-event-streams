#!/usr/bin/env python3
"""
Create, update, and delete Event Streams topics, subscriptions, and tokens.

    # create
    python es_admin.py create-topic        --environment Test --name orders
    python es_admin.py create-subscription --environment Test --topic orders --name orders-sub
    python es_admin.py create-token        --environment Test --name producer --produce

    # update
    python es_admin.py update-topic --environment Test --name orders --description "..."
    python es_admin.py update-token --environment Test --name SO_Failover_Consumer \\
                                    --expires 2027-06-01T00:00:00Z

    # destroy -- each needs --confirm, and each is permanent
    python es_admin.py delete-topic        --environment Test --name orders --confirm
    python es_admin.py delete-subscription --environment Test --topic orders --name sub --confirm
    python es_admin.py delete-token        --environment Test --name old-token --confirm
    python es_admin.py clear-backlog       --environment Test --topic orders --name sub --confirm

Without `--confirm`, destructive commands describe exactly what they would remove and
change nothing. That dry run is the point: it tells you how many messages are about to
be discarded, which is the number people most often turn out not to have known.

Environments in BOOMI_PROTECTED_ENVIRONMENTS are refused for every write here,
updates included. Renaming a production topic is not destructive the way a delete is,
but it silently breaks every producer and consumer pointing at the old name.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from boomi_auth import BoomiAPIError, BoomiAuthError, build_client
from es_admin_ops import EventStreamsAdmin, ProtectedEnvironmentError


def find_topic(admin: EventStreamsAdmin, env_id: str, name: str) -> dict[str, Any] | None:
    return next(
        (t for t in admin.es.topics(env_id) if str(t.get("name")) == name), None
    )


def find_subscription(admin, env_id, topic_name, sub_name) -> dict[str, Any] | None:
    topic = find_topic(admin, env_id, topic_name)
    if not topic:
        return None
    return next(
        (s for s in topic.get("subscriptions") or [] if str(s.get("name")) == sub_name), None
    )


def find_token(admin: EventStreamsAdmin, env_id: str, name: str) -> dict[str, Any] | None:
    matches = [t for t in admin.es.tokens(env_id) if str(t.get("name")) == name]
    if len(matches) > 1:
        raise SystemExit(
            f"{len(matches)} tokens in this environment are named '{name}'. "
            "Names are not unique, so this is ambiguous — use --token-id and pick one "
            "from `es_discover.py --json`."
        )
    return matches[0] if matches else None


def describe_deletion(admin: EventStreamsAdmin, args, env_id: str) -> list[str]:
    """Spell out what would be destroyed, in counts rather than adjectives."""
    lines: list[str] = []
    if args.command == "delete-topic":
        topic = find_topic(admin, env_id, args.name)
        if not topic:
            raise SystemExit(f"No topic '{args.name}' in {args.environment}.")
        subs = topic.get("subscriptions") or []
        backlog = sum(s.get("backlogCount") or 0 for s in subs)
        lines.append(f"Topic `{args.name}`")
        lines.append(f"  {len(subs)} subscription(s) will be removed with it"
                     + (": " + ", ".join(str(s.get("name")) for s in subs) if subs else ""))
        lines.append(f"  {backlog} queued message(s) will be discarded")
    elif args.command in ("delete-subscription", "clear-backlog"):
        sub = find_subscription(admin, env_id, args.topic, args.name)
        if not sub:
            raise SystemExit(f"No subscription '{args.topic}/{args.name}' in {args.environment}.")
        backlog = sub.get("backlogCount") or 0
        verb = "removed" if args.command == "delete-subscription" else "kept, but emptied"
        lines.append(f"Subscription `{args.topic}/{args.name}` will be {verb}")
        lines.append(f"  {backlog} queued message(s) will be discarded")
        if args.command == "clear-backlog":
            lines.append("  the subscription itself survives, so nothing will look "
                         "broken afterwards — the messages are simply gone")
    elif args.command == "delete-token":
        token = find_token(admin, env_id, args.name) if args.name else None
        label = args.token_id or (token or {}).get("id") or args.name
        lines.append(f"Token `{args.name or label}`")
        lines.append("  anything authenticating with it starts failing immediately, "
                     "and the value cannot be recovered")
    return lines


def main() -> int:
    p = argparse.ArgumentParser(description="Create, update, and delete Event Streams entities.")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, topic=False, name=True):
        sp.add_argument("--environment", required=True)
        if topic:
            sp.add_argument("--topic", required=True)
        if name:
            sp.add_argument("--name", required=True)

    c = sub.add_parser("create-topic")
    common(c)
    c.add_argument("--description")
    c.add_argument("--partitions", type=int)
    c.add_argument("--persistent", action="store_true", default=None)

    c = sub.add_parser("create-subscription"); common(c, topic=True)
    c.add_argument("--description")

    c = sub.add_parser("create-token"); common(c)
    c.add_argument("--produce", action="store_true")
    c.add_argument("--consume", action="store_true")
    c.add_argument("--expires", help="ISO-8601, e.g. 2027-06-01T00:00:00Z")
    c.add_argument("--description")

    u = sub.add_parser("update-topic"); common(u)
    u.add_argument("--description")
    u.add_argument("--partitions", type=int)

    u = sub.add_parser("update-subscription"); common(u, topic=True)
    u.add_argument("--description")

    u = sub.add_parser("update-token"); common(u)
    u.add_argument("--token-id", help="Use when several tokens share a name.")
    u.add_argument("--rename")
    u.add_argument("--expires")
    u.add_argument("--produce", dest="produce", action="store_true", default=None)
    u.add_argument("--no-produce", dest="produce", action="store_false")
    u.add_argument("--consume", dest="consume", action="store_true", default=None)
    u.add_argument("--no-consume", dest="consume", action="store_false")
    u.add_argument("--description")

    for cmd, needs_topic in (("delete-topic", False), ("delete-subscription", True),
                             ("clear-backlog", True), ("delete-token", False)):
        d = sub.add_parser(cmd)
        common(d, topic=needs_topic)
        if cmd == "delete-token":
            d.add_argument("--token-id")
        d.add_argument("--confirm", action="store_true",
                       help="Actually do it. Without this, describes and exits.")

    args = p.parse_args()

    try:
        admin = EventStreamsAdmin(build_client())
        env = admin.resolve_environment(args.environment)
        env_id = env["id"]
        destructive = args.command.startswith("delete") or args.command == "clear-backlog"
        admin.guard(env, args.command)

        if destructive:
            for line in describe_deletion(admin, args, env_id):
                print(line)
            if not getattr(args, "confirm", False):
                print("\nNothing was changed. Re-run with --confirm to proceed.")
                print("This cannot be undone — there is no restore for Event Streams "
                      "entities or queued messages.")
                return 0

        if args.command == "create-topic":
            r = admin.create_topic(env_id, args.name, persistent=args.persistent,
                                   partitions=args.partitions, description=args.description)
            print(f"Created topic `{args.name}` in {env['name']}. {r}")
            print("A topic with no subscription discards everything published to it, "
                  "so create the subscription next.")

        elif args.command == "create-subscription":
            admin.create_subscription(env_id, args.topic, args.name,
                                      description=args.description)
            print(f"Created subscription `{args.topic}/{args.name}` in {env['name']}.")
            print("It will report type NONE until a consumer attaches — the broker "
                  "assigns the type at that point, not at creation.")

        elif args.command == "create-token":
            if not (args.produce or args.consume):
                raise SystemExit("A token needs --produce, --consume, or both.")
            r = admin.create_token(env_id, args.name, allow_consume=args.consume,
                                   allow_produce=args.produce,
                                   expiration_time=args.expires,
                                   description=args.description)
            print(f"Created token `{args.name}` in {env['name']} (id {r.get('id')}).")
            print("Read its value from the platform UI — it is a credential and is "
                  "deliberately not printed here.")

        elif args.command == "update-topic":
            current = find_topic(admin, env_id, args.name)
            if not current:
                raise SystemExit(f"No topic '{args.name}' in {env['name']}.")
            # The API reuses the create input for updates, so anything not sent may be
            # reset. Carry the current values forward for whatever was not specified.
            admin.update_topic(
                env_id, args.name,
                description=args.description if args.description is not None
                else current.get("description"),
                persistent=current.get("persistent"),
                partitions=args.partitions if args.partitions is not None
                else current.get("partitions"),
            )
            print(f"Updated topic `{args.name}` in {env['name']}.")

        elif args.command == "update-subscription":
            admin.update_subscription(env_id, args.topic, args.name,
                                      description=args.description)
            print(f"Updated subscription `{args.topic}/{args.name}` in {env['name']}.")

        elif args.command == "update-token":
            token = ({"id": args.token_id} if args.token_id
                     else find_token(admin, env_id, args.name))
            if not token:
                raise SystemExit(f"No token '{args.name}' in {env['name']}.")
            admin.update_token(str(token["id"]), name=args.rename,
                               allow_produce=args.produce, allow_consume=args.consume,
                               expiration_time=args.expires,
                               description=args.description)
            print(f"Updated token `{args.name}` in {env['name']}.")
            if args.expires:
                print("The token value is unchanged, so connection components keep "
                      "working — unlike migrating a token, which mints a new JWT.")

        elif args.command == "delete-topic":
            admin.delete_topic(env_id, args.name)
            print(f"\nDeleted topic `{args.name}` from {env['name']}.")

        elif args.command == "delete-subscription":
            admin.delete_subscription(env_id, args.topic, args.name)
            print(f"\nDeleted subscription `{args.topic}/{args.name}` from {env['name']}.")

        elif args.command == "delete-token":
            token = ({"id": args.token_id} if args.token_id
                     else find_token(admin, env_id, args.name))
            if not token:
                raise SystemExit(f"No token '{args.name}' in {env['name']}.")
            admin.delete_token(str(token["id"]))
            print(f"\nDeleted token `{args.name}` from {env['name']}.")

        elif args.command == "clear-backlog":
            admin.clear_backlog(env_id, args.topic, args.name)
            print(f"\nCleared the backlog on `{args.topic}/{args.name}` in {env['name']}.")

    except ProtectedEnvironmentError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    except (BoomiAuthError, BoomiAPIError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
