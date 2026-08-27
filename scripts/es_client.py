"""
Event Streams operations over the Boomi GraphQL API.

Three shapes of the API are worth knowing before reading this, because each one
forces a decision in the code below:

  1. There is no top-level subscription query. Subscriptions only exist nested
     under a topic, so "list the subscriptions" means "list topics and flatten".

  2. There is no token query at all. Tokens hang off the platform environment tree
     at environments { eventStreams { tokens } }, which is a different query root
     from everything else here.

  3. Subscription type is read-only. EventStreamsSubscriptionCreateInput has no
     type field -- Pulsar assigns EXCLUSIVE / SHARED / FAILOVER / KEY_SHARED when a
     consumer actually attaches. A freshly created subscription reporting NONE is
     correct and expected, not a failed migration. See reference/limitations.md.

This module intentionally contains no delete or update-in-place operations. Every
write here is additive. The Event Streams API does expose delete mutations; leaving
them unimplemented is the point, because a function that does not exist cannot be
reached by a persuasive prompt.
"""

from __future__ import annotations

from typing import Any

import os
import sys

from boomi_auth import BoomiAuthError, BoomiClient, BoomiFieldError

# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

# The Event Streams schema is not identical across accounts. Asking for a field the
# account's schema does not define is a hard validation error that fails the whole
# query -- not a null in the response -- so a fixed field list makes the plugin
# work on the account it was written against and nowhere else.
#
# Instead we introspect the type once per run and request the intersection of what
# we want and what exists. Fields listed here are optional by definition; anything
# genuinely required belongs in REQUIRED_TOPIC_FIELDS.

REQUIRED_TOPIC_FIELDS = ["name"]
OPTIONAL_TOPIC_FIELDS = [
    "description",
    "persistent",
    "partitions",
    "restProduceUrl",
    "restProduceSingleMsgUrl",
]

REQUIRED_SUBSCRIPTION_FIELDS = ["name"]
OPTIONAL_SUBSCRIPTION_FIELDS = ["description", "type", "durable", "backlogCount"]

TYPE_FIELDS_QUERY = """
query TypeFields($name: String!) {
  __type(name: $name) { name kind fields { name } inputFields { name } }
}
"""

# Resolving a type by guessing its name is unreliable here: a Boomi schema can define
# more than one type called EventStreamsTopic (an admin one and the one this query
# actually returns), and __type picks whichever it finds. The result is introspection
# cheerfully reporting fields that the query validator then rejects.
#
# So never name a type. Ask the schema what a given field returns and unwrap the
# NonNull/List wrappers to reach the concrete type. Five levels is deeper than any
# real signature needs and costs nothing.
MUTATION_ARGS_QUERY = """
query MutationArgs($parent: String!) {
  __type(name: $parent) {
    fields {
      name
      args {
        name
        type { kind name
          ofType { kind name
            ofType { kind name
              ofType { kind name } } } }
      }
    }
  }
}
"""

FIELD_TYPES_QUERY = """
query FieldTypes($parent: String!) {
  __type(name: $parent) {
    name
    fields {
      name
      type { kind name
        ofType { kind name
          ofType { kind name
            ofType { kind name
              ofType { kind name } } } } }
    }
  }
}
"""

REQUIRED_ES_ENV_FIELDS = ["region"]
# topicCount and subscriptionCount are the server's own tally, independent of the
# list it returns. That makes them a truncation detector -- see check_completeness().
OPTIONAL_ES_ENV_FIELDS = ["restProduceBaseUrl", "topicCount", "subscriptionCount"]

REQUIRED_TOKEN_FIELDS = ["id", "name"]
OPTIONAL_TOKEN_FIELDS = [
    "allowConsume",
    "allowProduce",
    "expirationTime",
    "expirationEditable",
    "createdTime",
    "description",
]

# ---------------------------------------------------------------------------
# Mutations -- additive only
# ---------------------------------------------------------------------------

TOPIC_CREATE = """
mutation CreateTopic($input: EventStreamsTopicCreateInput!) {
  eventStreamsTopicCreate(input: $input) { name persistent partitions }
}
"""

SUBSCRIPTION_CREATE = """
mutation CreateSubscription($input: EventStreamsSubscriptionCreateInput!) {
  eventStreamsSubscriptionCreate(input: $input) { name type }
}
"""

TOKEN_CREATE = """
mutation CreateToken($input: EventStreamsEnvironmentTokenCreateInput!) {
  eventStreamsTokenCreate(input: $input) {
    id
    name
    allowConsume
    allowProduce
    expirationTime
  }
}
"""


class EventStreamsClient:
    """Read and additively write Event Streams entities."""

    def __init__(self, client: BoomiClient) -> None:
        self.client = client
        self._type_fields: dict[str, set[str]] = {}
        self._field_types: dict[str, dict[str, str]] = {}
        self._resolved: dict[str, str | None] = {}
        # Selection paths the executor has rejected this run, e.g.
        # "eventStreamsTopics/persistent". Introspection is not a reliable guide to
        # what will execute, so this is populated from actual rejections and is the
        # authoritative record of what this account supports.
        self._rejected: set[str] = set()

    # -- executing against an untrustworthy schema ------------------------------

    def _execute_pruning(
        self,
        build_query,
        variables: dict[str, Any] | None = None,
        max_attempts: int = 5,
    ) -> dict[str, Any]:
        """Run a query, dropping any field the executor rejects, then retry.

        Introspection here advertises fields the query validator refuses, so the
        only dependable description of the API is the API's own rejection. Each
        rejected path is remembered for the rest of the run, so the cost is a small
        number of extra round trips on the first call and none afterwards.

        build_query receives the set of paths to omit and returns the query string.
        """
        for _ in range(max_attempts):
            query = build_query(self._rejected)
            try:
                return self.client.graphql(query, variables)
            except BoomiFieldError as exc:
                new = exc.field_paths - self._rejected
                if not new:
                    raise  # Pruning is not converging; surface the real error.
                self._rejected |= new
                if os.environ.get("BOOMI_ES_VERBOSE"):
                    print(
                        "note: this account does not support "
                        + ", ".join(sorted(new))
                        + " — retrying without it.",
                        file=sys.stderr,
                    )
        raise BoomiFieldError(
            "Could not build a query this account accepts after "
            f"{max_attempts} attempts. Rejected so far: "
            + ", ".join(sorted(self._rejected)),
            self._rejected,
        )

    def rejected_fields(self) -> set[str]:
        """Selection paths this account refused during this run."""
        return set(self._rejected)

    # -- detecting a silently truncated list -------------------------------------

    def check_completeness(self, environment: dict[str, Any],
                           topics: list[dict[str, Any]]) -> list[str]:
        """Compare returned list lengths against the server's own counts.

        The Event Streams GraphQL fields take no pagination arguments at all --
        no first/after, no limit/offset, no Relay connections -- so a list is
        returned whole or not at all. There is also no hasNextPage or totalCount in
        the response, which means that if the server ever did cap a large result set,
        nothing in the reply would say so: the output would look complete and simply
        be wrong. Under-reporting an inventory is the kind of error that gets acted on
        rather than noticed.

        The environment carries its own topicCount and subscriptionCount, computed
        independently of the list. Comparing the two costs nothing and turns an
        undetectable failure into a loud one. Returns a list of warnings, empty when
        the counts agree or the account does not expose them.
        """
        warnings: list[str] = []
        event_streams = environment.get("eventStreams") or {}

        declared_topics = event_streams.get("topicCount")
        if isinstance(declared_topics, int) and declared_topics != len(topics):
            warnings.append(
                f"{environment.get('name')}: the account reports {declared_topics} "
                f"topic(s) but the query returned {len(topics)}. The result is "
                "incomplete — treat any inventory, drift, or migration plan built "
                "from it as unreliable until this is understood."
            )

        declared_subs = event_streams.get("subscriptionCount")
        actual_subs = sum(len(t.get("subscriptions") or []) for t in topics)
        if isinstance(declared_subs, int) and declared_subs != actual_subs:
            warnings.append(
                f"{environment.get('name')}: the account reports {declared_subs} "
                f"subscription(s) but the query returned {actual_subs}."
            )

        for topic in topics:
            declared = topic.get("subscriptionCount")
            actual = len(topic.get("subscriptions") or [])
            if isinstance(declared, int) and declared != actual:
                warnings.append(
                    f"{environment.get('name')}/{topic.get('name')}: the account "
                    f"reports {declared} subscription(s), the query returned {actual}."
                )
        return warnings

    # -- schema adaptation ------------------------------------------------------

    @staticmethod
    def _unwrap(type_ref: dict[str, Any] | None) -> str | None:
        """Strip NON_NULL and LIST wrappers to reach the underlying named type."""
        while type_ref:
            if type_ref.get("name") and type_ref.get("kind") not in ("NON_NULL", "LIST"):
                return type_ref["name"]
            type_ref = type_ref.get("ofType")
        return None

    def _field_type_map(self, parent_type: str) -> dict[str, str]:
        """{field name -> concrete type name} for one type, cached per run."""
        if parent_type in self._field_types:
            return self._field_types[parent_type]
        mapping: dict[str, str] = {}
        try:
            data = self.client.graphql(FIELD_TYPES_QUERY, {"parent": parent_type})
            for field in ((data.get("__type") or {}).get("fields") or []):
                resolved = self._unwrap(field.get("type"))
                if resolved:
                    mapping[field["name"]] = resolved
        except BoomiAuthError:
            # Never swallow this. To the caller an auth failure is indistinguishable
            # from "this account's schema lacks these fields" -- and that is a
            # confident diagnosis which is the exact opposite of the truth. Bad
            # credentials must surface as bad credentials.
            raise
        except Exception:
            mapping = {}
        self._field_types[parent_type] = mapping
        return mapping

    def resolve_type(self, path: str) -> str | None:
        """Resolve the concrete type reached by a field path, e.g. 'Query.eventStreamsTopics'.

        This is how every type name in this client is obtained. Looking a type up by
        its expected name is what produced the original failure: introspection found
        an `EventStreamsTopic` that advertised `persistent` and `partitions`, while
        the type `eventStreamsTopics` actually returns has neither, so every query
        built from that introspection was rejected by the validator.
        """
        if path in self._resolved:
            return self._resolved[path]
        parts = path.split(".")
        current: str | None = parts[0]
        for field_name in parts[1:]:
            if not current:
                break
            current = self._field_type_map(current).get(field_name)
        self._resolved[path] = current
        return current

    def type_fields(self, type_name: str | None) -> set[str]:
        """Field names on a type, cached per run. Handles object and input types.

        An empty set means "could not determine", and callers treat that as "request
        only the required fields" -- a smaller result rather than a failed query.
        """
        if not type_name:
            return set()
        if type_name in self._type_fields:
            return self._type_fields[type_name]
        try:
            data = self.client.graphql(TYPE_FIELDS_QUERY, {"name": type_name})
            info = data.get("__type") or {}
            fields = {
                f["name"] for f in ((info.get("fields") or []) + (info.get("inputFields") or []))
            }
        except BoomiAuthError:
            raise  # see type_fields note above -- an auth failure is not an absent field
        except BoomiAuthError:
            # Never swallow this. To the caller an auth failure is indistinguishable
            # from "this account's schema lacks these fields" -- and that is a
            # confident diagnosis which is the exact opposite of the truth. Bad
            # credentials must surface as bad credentials.
            raise
        except Exception:
            fields = set()
        self._type_fields[type_name] = fields
        return fields

    def fields_at(self, path: str) -> set[str]:
        """Fields available on whatever type the given field path returns."""
        return self.type_fields(self.resolve_type(path))

    def input_fields_for(self, mutation: str, arg: str = "input") -> set[str]:
        """Fields accepted by a mutation's input argument.

        Resolved from the mutation's actual argument type for the same reason the
        output types are: an input type looked up by its expected name may not be
        the one this mutation accepts, and sending an undefined input field fails
        the whole mutation rather than being ignored.
        """
        key = f"Mutation.{mutation}({arg})"
        if key in self._resolved:
            return self.type_fields(self._resolved[key])
        resolved: str | None = None
        try:
            data = self.client.graphql(MUTATION_ARGS_QUERY, {"parent": "Mutation"})
            for field in ((data.get("__type") or {}).get("fields") or []):
                if field.get("name") != mutation:
                    continue
                for argument in field.get("args") or []:
                    if argument.get("name") == arg:
                        resolved = self._unwrap(argument.get("type"))
        except BoomiAuthError:
            # Never swallow this. To the caller an auth failure is indistinguishable
            # from "this account's schema lacks these fields" -- and that is a
            # confident diagnosis which is the exact opposite of the truth. Bad
            # credentials must surface as bad credentials.
            raise
        except Exception:
            resolved = None
        self._resolved[key] = resolved
        return self.type_fields(resolved)

    def _selection(
        self,
        path: str,
        required: list[str],
        optional: list[str],
        selection_prefix: str = "",
        rejected: set[str] | None = None,
    ) -> list[str]:
        """Fields to request: required, plus optional ones not known to be unusable.

        Two filters apply. Introspection narrows the list optimistically, and the
        rejected set removes anything the executor has actually refused. The second
        overrides the first, because only one of them has been tested.
        """
        rejected = rejected if rejected is not None else self._rejected
        available = self.fields_at(path)
        chosen = list(optional) if not available else [f for f in optional if f in available]
        chosen = [f for f in chosen if f"{selection_prefix}{f}" not in rejected]
        return list(required) + chosen

    def supports(self, path: str, field: str, selection_prefix: str = "") -> bool:
        """Whether this account can actually return a field.

        A field the executor rejected counts as unsupported regardless of what
        introspection claims. Callers use this to tell "the value is absent" apart
        from "the concept does not exist here", which matters when reporting a
        migration as verified.
        """
        if f"{selection_prefix}{field}" in self._rejected:
            return False
        return field in self.fields_at(path)

    # Field paths, resolved against the live schema rather than assumed by name.
    TOPICS = "Query.eventStreamsTopics"
    SUBSCRIPTIONS = "Query.eventStreamsTopics.subscriptions"
    ENVIRONMENTS = "Query.environments"
    ES_ENVIRONMENT = "Query.environments.eventStreams"
    TOKENS = "Query.environments.eventStreams.tokens"

    TOPIC_SEL = "eventStreamsTopics/"
    SUB_SEL = "eventStreamsTopics/subscriptions/"

    def _topics_query(self, rejected: set[str]) -> str:
        topic_fields = self._selection(
            self.TOPICS, REQUIRED_TOPIC_FIELDS, OPTIONAL_TOPIC_FIELDS, self.TOPIC_SEL, rejected
        )
        subscriptions_block = ""
        if f"{self.TOPIC_SEL}subscriptions" not in rejected:
            sub_fields = self._selection(
                self.SUBSCRIPTIONS,
                REQUIRED_SUBSCRIPTION_FIELDS,
                OPTIONAL_SUBSCRIPTION_FIELDS,
                self.SUB_SEL,
                rejected,
            )
            subscriptions_block = (
                "\n    subscriptions {\n      " + "\n      ".join(sub_fields) + "\n    }"
            )
        return (
            "query Topics($environmentId: ID) {\n"
            "  eventStreamsTopics(environmentId: $environmentId) {\n    "
            + "\n    ".join(topic_fields)
            + subscriptions_block
            + "\n  }\n}"
        )

    # -- reads ------------------------------------------------------------------

    def topics(self, environment_id: str | None = None) -> list[dict[str, Any]]:
        data = self._execute_pruning(self._topics_query, {"environmentId": environment_id})
        return data.get("eventStreamsTopics") or []

    def subscriptions(self, environment_id: str | None = None) -> list[dict[str, Any]]:
        """Flatten subscriptions out of the topic tree, tagging each with its topic."""
        flattened: list[dict[str, Any]] = []
        for topic in self.topics(environment_id):
            for subscription in topic.get("subscriptions") or []:
                entry = dict(subscription)
                entry["topicName"] = topic.get("name")
                flattened.append(entry)
        return flattened

    ES_ENV_SEL = "environments/eventStreams/"
    TOKEN_SEL = "environments/eventStreams/tokens/"

    def _environments_query(self, rejected: set[str]) -> str:
        es_fields = self._selection(
            self.ES_ENVIRONMENT,
            REQUIRED_ES_ENV_FIELDS,
            OPTIONAL_ES_ENV_FIELDS,
            self.ES_ENV_SEL,
            rejected,
        )
        token_fields = self._selection(
            self.TOKENS, REQUIRED_TOKEN_FIELDS, OPTIONAL_TOKEN_FIELDS, self.TOKEN_SEL, rejected
        )
        return (
            "query EnvironmentsWithEventStreams {\n"
            "  environments {\n    id\n    name\n    eventStreams {\n      "
            + "\n      ".join(es_fields)
            + "\n      tokens {\n        "
            + "\n        ".join(token_fields)
            + "\n      }\n    }\n  }\n}"
        )

    def environments(self) -> list[dict[str, Any]]:
        """Every environment, with its Event Streams block where one is provisioned.

        Environments without Event Streams come back with eventStreams = null. That
        is a normal state, not an error -- an account typically has several.
        """
        data = self._execute_pruning(self._environments_query)
        return data.get("environments") or []

    def tokens(self, environment_id: str | None = None) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        for env in self.environments():
            if environment_id and env.get("id") != environment_id:
                continue
            event_streams = env.get("eventStreams") or {}
            for token in event_streams.get("tokens") or []:
                entry = dict(token)
                entry["environmentId"] = env.get("id")
                entry["environmentName"] = env.get("name")
                collected.append(entry)
        return collected

    def inventory(self, environment_id: str | None = None) -> dict[str, Any]:
        """Everything about one environment, or all of them, in a single shape.

        Callers get topics, subscriptions and tokens together because almost every
        question -- compare two environments, report on one, plan a migration --
        needs all three, and fetching them separately triples the round trips.
        """
        environments = self.environments()
        if environment_id:
            environments = [e for e in environments if e.get("id") == environment_id]

        result: dict[str, Any] = {"environments": []}
        for env in environments:
            env_id = env.get("id")
            event_streams = env.get("eventStreams")
            if not event_streams:
                result["environments"].append(
                    {
                        "id": env_id,
                        "name": env.get("name"),
                        "eventStreamsProvisioned": False,
                        "topics": [],
                        "tokens": [],
                    }
                )
                continue

            topics = self.topics(env_id)
            result["environments"].append(
                {
                    "id": env_id,
                    "name": env.get("name"),
                    "eventStreamsProvisioned": True,
                    "region": event_streams.get("region"),
                    "restProduceBaseUrl": event_streams.get("restProduceBaseUrl"),
                    "topics": topics,
                    "tokens": event_streams.get("tokens") or [],
                    # Carried on the result so every caller can surface it without
                    # having to remember to ask. A truncated inventory that looks
                    # complete is worse than an error.
                    "completenessWarnings": self.check_completeness(env, topics),
                }
            )
        return result

    # -- live health: what is actually happening right now -----------------------

    # Static configuration answers "what exists". These answer "is it working", which
    # is a different question and usually the one being asked. activeConsumerCount in
    # particular replaces an inference drawn from an expensive component scan with a
    # fact: whether anything is attached to this subscription at this moment.
    LIVE_TOPIC_FIELDS = [
        "backlogCount", "backlogSize", "messageRateIn", "messageRateOut",
        "producerCount", "subscriptionCount", "createdBy", "createdTime",
    ]
    LIVE_SUBSCRIPTION_FIELDS = [
        "activeConsumerCount", "deadLetterBacklogCount", "retryBacklogCount",
        "messageRateOut", "createdBy", "createdTime",
    ]

    def _live_query(self, rejected: set[str]) -> str:
        topic_fields = self._selection(
            self.TOPICS, ["name"], self.LIVE_TOPIC_FIELDS, self.TOPIC_SEL, rejected
        )
        sub_fields = self._selection(
            self.SUBSCRIPTIONS,
            ["name", "backlogCount"],
            self.LIVE_SUBSCRIPTION_FIELDS,
            self.SUB_SEL,
            rejected,
        )
        return (
            "query LiveTopics($environmentId: ID) {\n"
            "  eventStreamsTopics(environmentId: $environmentId) {\n    "
            + "\n    ".join(topic_fields)
            + "\n    subscriptions {\n      "
            + "\n      ".join(sub_fields)
            + "\n    }\n  }\n}"
        )

    def live_topics(self, environment_id: str | None = None) -> list[dict[str, Any]]:
        """Topics with throughput, backlog, and live producer/consumer counts."""
        data = self._execute_pruning(self._live_query, {"environmentId": environment_id})
        return data.get("eventStreamsTopics") or []

    MESSAGE_FIELDS = [
        "messageId", "publishTime", "producer", "redeliveryCount",
        "size", "topicName", "subscriptionName",
    ]

    def _messages_query(self, field: str, include_payload: bool) -> str:
        fields = list(self.MESSAGE_FIELDS)
        if include_payload:
            # Payload is real customer data. It is only ever fetched when explicitly
            # asked for, so an ordinary DLQ depth check cannot pull message bodies
            # into a conversation as a side effect.
            fields += ["payload", "metaData"]
        return (
            f"query Messages($input: EventStreamsMessagesInput!) {{\n"
            f"  {field}(input: $input) {{ " + " ".join(fields) + " }\n}"
        )

    # Message indices are ONE-based. startIndex=0 is rejected outright with
    # "Invalid indices provided to query subscription backlog messages", which reads
    # like a range problem rather than an off-by-one and sends you looking in the
    # wrong place. Verified against a live account: 0 always fails, 1 always works.
    FIRST_MESSAGE_INDEX = 1

    def _fetch_messages(
        self,
        field: str,
        environment_id: str,
        topic: str,
        subscription: str,
        start: int,
        end: int,
        include_payload: bool,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "environmentId": environment_id,
            "topicName": topic,
            # subscriptionName is ID! -- required, not optional. Messages live on a
            # subscription, not on the topic, which follows from Pulsar semantics:
            # each subscription has its own cursor over the same stream.
            "subscriptionName": subscription,
            "startIndex": max(start, self.FIRST_MESSAGE_INDEX),
            "endIndex": max(end, self.FIRST_MESSAGE_INDEX),
        }
        data = self.client.graphql(
            self._messages_query(field, include_payload), {"input": payload}
        )
        return data.get(field) or []

    def messages(self, environment_id, topic, subscription, start=1, end=10,
                 include_payload=False) -> list[dict[str, Any]]:
        """Peek at queued messages without consuming them."""
        return self._fetch_messages(
            "eventStreamsMessages", environment_id, topic, subscription,
            start, end, include_payload,
        )

    def dead_letter_messages(self, environment_id, topic, subscription, start=1,
                             end=10, include_payload=False) -> list[dict[str, Any]]:
        """Read the dead letter queue for one subscription.

        This is where messages go when delivery keeps failing. Nothing in Boomi
        surfaces it, so it accumulates unseen -- which is precisely why it is worth
        reading on a schedule rather than only when someone suspects a problem.
        """
        return self._fetch_messages(
            "eventStreamsDeadLetterQueueMessages", environment_id, topic,
            subscription, start, end, include_payload,
        )

    # -- additive writes --------------------------------------------------------

    def create_topic(
        self,
        environment_id: str,
        name: str,
        *,
        persistent: bool | None = None,
        partitions: int | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"environmentId": environment_id, "name": name}
        # Only send fields this account's schema actually defines. Sending an
        # undefined input field is a hard validation error that fails the mutation,
        # so a value carried over from a richer source schema would otherwise break
        # the migration rather than being quietly dropped.
        create_input_fields = self.input_fields_for("eventStreamsTopicCreate")
        for key, value in (
            ("persistent", persistent),
            ("partitions", partitions),
            ("description", description),
        ):
            if value is None:
                continue
            if create_input_fields and key not in create_input_fields:
                continue
            payload[key] = value

        # The return selection is pruned the same way as queries. Asking the
        # mutation to echo a field this account cannot return would fail the
        # creation itself, which is a bad way to lose a migration halfway through.
        def build(rejected: set[str]) -> str:
            fields = self._selection(
                self.TOPICS,
                REQUIRED_TOPIC_FIELDS,
                ["persistent", "partitions"],
                "eventStreamsTopicCreate/",
                rejected,
            )
            return (
                "mutation CreateTopic($input: EventStreamsTopicCreateInput!) {\n"
                "  eventStreamsTopicCreate(input: $input) { " + " ".join(fields) + " }\n}"
            )

        data = self._execute_pruning(build, {"input": payload})
        return data.get("eventStreamsTopicCreate") or {}

    def create_subscription(
        self,
        environment_id: str,
        topic_name: str,
        name: str,
        *,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create a subscription.

        There is deliberately no `type` argument. The API does not accept one at
        creation time; the type is assigned by the broker when a consumer connects.
        Offering the parameter would imply a guarantee this cannot make good on.
        """
        payload: dict[str, Any] = {
            "environmentId": environment_id,
            "topicName": topic_name,
            "name": name,
        }
        if description is not None:
            payload["description"] = description
        data = self.client.graphql(SUBSCRIPTION_CREATE, {"input": payload})
        return data.get("eventStreamsSubscriptionCreate") or {}

    def create_token(
        self,
        environment_id: str,
        name: str,
        *,
        allow_consume: bool,
        allow_produce: bool,
        expiration_time: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create a token.

        The returned token carries a new JWT. Connection components in the target
        environment that referenced the old token value must be updated by hand --
        the value cannot be copied across. reference/limitations.md explains why.
        """
        payload: dict[str, Any] = {
            "environmentId": environment_id,
            "name": name,
            "allowConsume": allow_consume,
            "allowProduce": allow_produce,
        }
        if expiration_time is not None:
            payload["expirationTime"] = expiration_time
        if description is not None:
            payload["description"] = description
        data = self.client.graphql(TOKEN_CREATE, {"input": payload})
        return data.get("eventStreamsTokenCreate") or {}
