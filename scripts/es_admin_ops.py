"""
Mutating Event Streams operations: update, delete, and clear-backlog.

**This module is deliberately separate from es_client.py.**

Everything else in this plugin imports `EventStreamsClient`, which can read and can
additively create, and has no update or delete method at all. That is not an
oversight — it is the safety property the read-side skills rest on: a capability that
does not exist in the imported object cannot be reached by a persuasive prompt, a
misread instruction, or a confused chain of reasoning.

Destructive capability lives here instead, in a class nothing imports unless the user
explicitly ran the admin tool and asked for it. Someone who wants to delete a topic
has to reach for it deliberately. Someone asking "what topics do we have" cannot end
up here by accident.

Three operations in here permanently destroy data:

  delete_topic            removes the topic and everything queued on it
  delete_subscription     removes the subscription and its backlog
  clear_backlog           discards every unconsumed message on a subscription

None of them is recoverable. Each requires explicit confirmation at the CLI layer and
each refuses environments named in BOOMI_PROTECTED_ENVIRONMENTS.
"""

from __future__ import annotations

from typing import Any

from boomi_auth import BoomiClient
from es_client import EventStreamsClient


class ProtectedEnvironmentError(RuntimeError):
    """The target environment is on the operator's denylist."""


class EventStreamsAdmin:
    """Update and delete Event Streams entities.

    Wraps an EventStreamsClient for reads and schema adaptation rather than
    duplicating them, but the mutating methods exist only on this class.
    """

    def __init__(self, client: BoomiClient) -> None:
        self.client = client
        self.es = EventStreamsClient(client)

    # -- safety -----------------------------------------------------------------

    def guard(self, environment: dict[str, Any], operation: str) -> None:
        """Refuse any change to a protected environment.

        Applied to updates as well as deletes. Renaming a production topic is not
        destructive in the way a delete is, but it breaks every producer and consumer
        pointing at the old name, which is worse in practice.
        """
        if self.client.config.is_protected(environment):
            raise ProtectedEnvironmentError(
                f"'{environment.get('name')}' is listed in "
                f"BOOMI_PROTECTED_ENVIRONMENTS, so {operation} is refused.\n"
                "If this is genuinely intended, remove it from that list "
                "deliberately and re-run — the list exists so the decision is a "
                "separate, conscious act rather than a flag on a command line."
            )

    def resolve_environment(self, wanted: str) -> dict[str, Any]:
        for env in self.es.environments():
            if env.get("id") == wanted or str(env.get("name", "")).lower() == wanted.lower():
                return env
        available = ", ".join(str(e.get("name")) for e in self.es.environments())
        raise SystemExit(f"No environment '{wanted}'. Available: {available}")

    # -- create (delegated; additive, so it lives on the read client) ------------

    def create_topic(self, *args, **kwargs):
        return self.es.create_topic(*args, **kwargs)

    def create_subscription(self, *args, **kwargs):
        return self.es.create_subscription(*args, **kwargs)

    def create_token(self, *args, **kwargs):
        return self.es.create_token(*args, **kwargs)

    # -- update -----------------------------------------------------------------

    def update_topic(
        self,
        environment_id: str,
        name: str,
        *,
        description: str | None = None,
        persistent: bool | None = None,
        partitions: int | None = None,
    ) -> dict[str, Any]:
        """Update a topic.

        The API reuses EventStreamsTopicCreateInput for updates, so unsupplied fields
        may be reset to defaults rather than left alone. Callers should read the
        current values and pass them back — `es_admin.py` does this.
        """
        payload: dict[str, Any] = {"environmentId": environment_id, "name": name}
        accepted = self.es.input_fields_for("eventStreamsTopicUpdate")
        for key, value in (
            ("description", description),
            ("persistent", persistent),
            ("partitions", partitions),
        ):
            if value is None or (accepted and key not in accepted):
                continue
            payload[key] = value

        def build(rejected: set[str]) -> str:
            fields = self.es._selection(
                self.es.TOPICS, ["name"], ["description", "persistent", "partitions"],
                "eventStreamsTopicUpdate/", rejected,
            )
            return (
                "mutation UpdateTopic($input: EventStreamsTopicCreateInput!) {\n"
                "  eventStreamsTopicUpdate(input: $input) { " + " ".join(fields) + " }\n}"
            )

        data = self.es._execute_pruning(build, {"input": payload})
        return data.get("eventStreamsTopicUpdate") or {}

    def update_subscription(
        self, environment_id: str, topic_name: str, name: str,
        *, description: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "environmentId": environment_id, "topicName": topic_name, "name": name,
        }
        if description is not None:
            payload["description"] = description
        data = self.client.graphql(
            "mutation UpdateSubscription($input: EventStreamsSubscriptionCreateInput!) {\n"
            "  eventStreamsSubscriptionUpdate(input: $input) { name }\n}",
            {"input": payload},
        )
        return data.get("eventStreamsSubscriptionUpdate") or {}

    def update_token(
        self, token_id: str, *, name: str | None = None,
        allow_produce: bool | None = None, allow_consume: bool | None = None,
        expiration_time: str | None = None, description: str | None = None,
    ) -> dict[str, Any]:
        """Update a token in place.

        Unlike migration, this does not mint a new JWT — the token value is
        unchanged, so connection components keep working. That makes this the right
        way to extend an expiry, rather than creating a replacement token.
        """
        payload: dict[str, Any] = {"id": token_id}
        for key, value in (
            ("name", name), ("allowProduce", allow_produce),
            ("allowConsume", allow_consume), ("expirationTime", expiration_time),
            ("description", description),
        ):
            if value is not None:
                payload[key] = value
        data = self.client.graphql(
            "mutation UpdateToken($input: EventStreamsEnvironmentTokenUpdateInput!) {\n"
            "  eventStreamsTokenUpdate(input: $input) {\n"
            "    id name allowProduce allowConsume expirationTime\n  }\n}",
            {"input": payload},
        )
        return data.get("eventStreamsTokenUpdate") or {}

    # -- destroy ----------------------------------------------------------------

    def delete_topic(self, environment_id: str, name: str) -> bool:
        """Delete a topic. Takes its subscriptions and queued messages with it."""
        self.client.graphql(
            "mutation DeleteTopic($topic: EventStreamsTopicKey!) {\n"
            "  eventStreamsTopicDelete(topic: $topic)\n}",
            {"topic": {"environmentId": environment_id, "name": name}},
        )
        return True

    def delete_subscription(self, environment_id: str, topic_name: str, name: str) -> bool:
        """Delete a subscription, discarding whatever is queued on it."""
        self.client.graphql(
            "mutation DeleteSubscription($subscription: EventStreamsSubscriptionKey!) {\n"
            "  eventStreamsSubscriptionDelete(subscription: $subscription)\n}",
            {"subscription": {
                "topic": {"environmentId": environment_id, "name": topic_name},
                "name": name,
            }},
        )
        return True

    def delete_token(self, token_id: str) -> bool:
        """Delete a token. Anything authenticating with it starts failing at once."""
        self.client.graphql(
            "mutation DeleteToken($id: ID!) {\n  eventStreamsTokenDelete(id: $id)\n}",
            {"id": token_id},
        )
        return True

    def clear_backlog(self, environment_id: str, topic_name: str, name: str) -> bool:
        """Discard every unconsumed message on a subscription.

        The most quietly dangerous operation here: the subscription survives, so
        nothing looks broken afterwards, and the messages are simply gone.
        """
        self.client.graphql(
            "mutation ClearBacklog($subscription: EventStreamsSubscriptionKey!) {\n"
            "  eventStreamsSubscriptionClearBacklog(subscription: $subscription)\n}",
            {"subscription": {
                "topic": {"environmentId": environment_id, "name": topic_name},
                "name": name,
            }},
        )
        return True
