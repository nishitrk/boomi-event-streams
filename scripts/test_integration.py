#!/usr/bin/env python3
"""
End-to-end tests against a mocked Boomi account. No network, no credentials.

    python test_integration.py

The fixture is modelled on a real account rather than invented, including the parts
that caused trouble: a schema that advertises `persistent` and `partitions` through
introspection and then rejects them at execution, tokens sharing a name, an expired
token in production, and topics that exist in some environments but not others.

Testing against a convenient fixture would have missed every one of those.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.update(
    BOOMI_ACCOUNT_ID="trainingaccount-6HOMIV",
    BOOMI_USERNAME="tester@boomi.com",
    BOOMI_API_TOKEN="not-a-real-token",
    BOOMI_PROTECTED_ENVIRONMENTS="Production",
)

import boomi_auth  # noqa: E402
from boomi_auth import BoomiFieldError, Config  # noqa: E402
import es_inspect  # noqa: E402
import es_migrate  # noqa: E402
from es_client import EventStreamsClient  # noqa: E402


def _no_network(*args, **kwargs):
    """Fail loudly rather than reaching the internet.

    An earlier version of this suite silently made a real request to api.boomi.com
    because one command built its own client instead of accepting an injected one.
    A test that can reach the network will eventually depend on it, so the transport
    is disabled outright and any attempt is an error rather than a slow success.
    """
    raise AssertionError(
        "a test tried to make a real HTTP request — the mock was not wired in"
    )


boomi_auth._request = _no_network

PASSED = 0
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  pass  {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label} {detail}")


def section(title: str) -> None:
    print(f"\n{title}")


# ---------------------------------------------------------------------------
# Fixture — shaped like a real account
# ---------------------------------------------------------------------------

LOCAL, PROD, TEST = "97c5f7f6-local", "70f67aca-prod", "d0d27725-test"

# The executor refuses these even though introspection advertises them.
REFUSED_TOPIC_FIELDS = {"persistent", "partitions", "restProduceSingleMsgUrl"}

def sub(name, type_="NONE", backlog=0):
    return {"name": name, "description": None, "type": type_, "durable": True,
            "backlogCount": backlog}

TOPICS = {
    LOCAL: [
        {"name": "01_SalesForce_Failover_Test", "description": None,
         "subscriptions": [sub("SAP_Warehouse_Failover_Test")]},
        {"name": "01_SalesForce_Orders", "description": "Sales Order Producer",
         "subscriptions": [sub("SAP_Warehouse_01")]},
        {"name": "POC_Topic", "description": "POC topic",
         "subscriptions": [sub("POC_Sub_Shared"), sub("POC_Listen_Exclusive")]},
        {"name": "ZF_Producer", "description": None, "subscriptions": [sub("ZF_Producer_01")]},
    ],
    PROD: [
        {"name": "01_SalesForce_Failover_Test", "description": None,
         "subscriptions": [sub("SAP_Warehouse_Failover_Test")]},
        {"name": "01_SalesForce_Orders", "description": "Sales Order Producer",
         "subscriptions": [sub("SAP_Warehouse_01")]},
        {"name": "boomi.onboarding.customer.registered", "description": None,
         "subscriptions": [sub("registered_customer")]},
    ],
    TEST: [
        {"name": "01_SalesForce_Orders", "description": "Sales Order Producer",
         "subscriptions": [sub("SAP_Warehouse_01")]},
        {"name": "Student_Data_Math", "description": None,
         "subscriptions": [sub("Student_Data_Math", backlog=1)]},
        {"name": "Student_Data_Producer", "description": None,
         "subscriptions": [sub("Producer", "SHARED"), sub("Test", backlog=1)]},
        {"name": "Orphan_Free_Topic", "description": None, "subscriptions": []},
    ],
}

def token(name, produce, consume, expiry):
    return {"id": f"tok-{name}-{expiry}", "name": name, "allowProduce": produce,
            "allowConsume": consume, "expirationTime": expiry, "description": None}

TOKENS = {
    # Four tokens sharing one name, as seen in the real account.
    LOCAL: [token("SO_Producer", True, False, "2027-04-09T18:29:59.000Z") for _ in range(4)]
           + [token("ZF_Consumer", False, True, "2027-04-02T18:29:59.000Z")],
    # An expired token sitting in production.
    PROD: [token("SO_Failover_Consumer", False, True, "2026-04-16T18:29:59.000Z"),
           token("Customer_Onboarding", True, True, "2027-05-13T18:29:59.000Z")],
    TEST: [token("SO_Producer", True, False, "2027-04-09T18:29:59.000Z")],
}

ENVIRONMENTS = [
    {"id": LOCAL, "name": "Local Test Atm",
     "eventStreams": {"region": "usa-east-1", "tokens": TOKENS[LOCAL]}},
    {"id": PROD, "name": "Production",
     "eventStreams": {"region": "usa-east-1", "tokens": TOKENS[PROD]}},
    {"id": TEST, "name": "Test",
     "eventStreams": {"region": "usa-east-1", "tokens": TOKENS[TEST]}},
    {"id": "no-es", "name": "Sandbox", "eventStreams": None},
]

# Component fixtures for the topology scan.
OP_ORDERS_PRODUCE = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
OP_ORDERS_CONSUME = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
OP_MISSING_TOPIC = "cccccccc-3333-4333-8333-cccccccccccc"
OP_DYNAMIC = "dddddddd-4444-4444-8444-dddddddddddd"
OP_UNRELATED = "eeeeeeee-5555-4555-8555-eeeeeeeeeeee"
PROC_MAIN = "11111111-aaaa-4aaa-8aaa-111111111111"
PROC_CONSUMER = "22222222-bbbb-4bbb-8bbb-222222222222"
PROC_BROKEN = "33333333-cccc-4ccc-8ccc-333333333333"

def es_operation_xml(topic: str, direction: str, access: str = "Exclusive") -> str:
    """The real shape of a Boomi Event Streams operation component.

    Copied from a live account rather than invented. Two details here are exactly
    what earlier versions got wrong, so they are load-bearing in this fixture:
    direction lives in `customOperationType`, and `operationType` is always
    "EXECUTE" — a decoy that classifies everything as a producer if matched.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<bns:Component xmlns:bns="http://api.platform.boomi.com/" '
        'type="connector-action" subType="officialboomi-X3979C-events-prod">'
        "<bns:object><Operation><Configuration>"
        f'<GenericOperationConfig customOperationType="{direction}" '
        'operationType="EXECUTE" requestProfileType="binary">'
        f'<field id="topic" type="string" value="{topic}"/>'
        f'<field id="producerAccessMode" type="string" value="{access}"/>'
        '<field id="compressionType" type="string" value="NONE"/>'
        "</GenericOperationConfig></Configuration></Operation></bns:object>"
        "</bns:Component>"
    )


COMPONENT_XML = {
    OP_ORDERS_PRODUCE: es_operation_xml("01_SalesForce_Orders", "PRODUCE"),
    OP_ORDERS_CONSUME: es_operation_xml("01_SalesForce_Orders", "CONSUME", "Shared"),
    # A topic that exists in no environment: the orphaned-operation case.
    OP_MISSING_TOPIC: es_operation_xml("Topic_That_Was_Never_Promoted", "PRODUCE"),
    OP_DYNAMIC: es_operation_xml("DPP_TargetTopic", "LISTEN"),
    OP_UNRELATED: '<operation actionType="QUERY"><field name="objectName">Account</field></operation>',
    # Processes reference operations through attributes that are NOT called
    # componentId -- which is exactly why the original regex matched nothing against
    # a real account. Using realistic attribute names here is what makes this
    # fixture able to catch that class of bug.
    PROC_MAIN: f'<process><shape shapetype="connectoraction"><configuration>'
               f'<connectorAction actionType="SEND" operationId="{OP_ORDERS_PRODUCE}" '
               f'connectionId="99999999-0000-4000-8000-999999999999"/></configuration></shape>'
               f'<shape><connectorAction operationId="{OP_UNRELATED}"/></shape></process>',
    PROC_CONSUMER: f'<process><shape><connectorAction actionType="GET" '
                   f'operationId="{OP_ORDERS_CONSUME}"/></shape></process>',
    PROC_BROKEN: f'<process><shape><connectorAction operationId="{OP_MISSING_TOPIC}"/></shape>'
                 f'<shape><connectorAction operationId="{OP_DYNAMIC}"/></shape></process>',
}

# An intermediate component: a process reaches OP_DYNAMIC through this rather than
# referencing it directly. ComponentReference does not recurse, so finding the process
# requires a second hop.
INTERMEDIATE = "44444444-dddd-4ddd-8ddd-444444444444"

# parentComponentId -> child componentId, as ComponentReference reports it.
COMPONENT_REFERENCES = [
    {"parentComponentId": PROC_MAIN, "componentId": OP_ORDERS_PRODUCE,
     "parentVersion": 3, "type": "DEPENDENT"},
    {"parentComponentId": PROC_MAIN, "componentId": OP_UNRELATED,
     "parentVersion": 3, "type": "DEPENDENT"},
    {"parentComponentId": PROC_CONSUMER, "componentId": OP_ORDERS_CONSUME,
     "parentVersion": 2, "type": "DEPENDENT"},
    {"parentComponentId": PROC_BROKEN, "componentId": OP_MISSING_TOPIC,
     "parentVersion": 1, "type": "DEPENDENT"},
    # Indirect: OP_DYNAMIC <- INTERMEDIATE <- PROC_BROKEN
    {"parentComponentId": INTERMEDIATE, "componentId": OP_DYNAMIC,
     "parentVersion": 1, "type": "DEPENDENT"},
    {"parentComponentId": PROC_BROKEN, "componentId": INTERMEDIATE,
     "parentVersion": 1, "type": "DEPENDENT"},
    # A non-process parent that leads nowhere, which must not appear in the map.
    {"parentComponentId": "77777777-eeee-4eee-8eee-777777777777",
     "componentId": OP_ORDERS_PRODUCE, "parentVersion": 1, "type": "INDEPENDENT"},
]

COMPONENT_META = {
    "connector-action": [
        {"componentId": OP_ORDERS_PRODUCE, "name": "ES Send Orders", "subType": "custom_es_v2", "currentVersion": 1},
        {"componentId": OP_ORDERS_CONSUME, "name": "ES Get Orders", "subType": "custom_es_v2", "currentVersion": 1},
        {"componentId": OP_MISSING_TOPIC, "name": "ES Send Legacy", "subType": "custom_es_v2", "currentVersion": 1},
        {"componentId": OP_DYNAMIC, "name": "eventstreams dynamic send", "subType": "eventstreams", "currentVersion": 1},
        {"componentId": OP_UNRELATED, "name": "SF Query Account", "subType": "salesforce", "currentVersion": 1},
    ],
    "process": [
        {"componentId": PROC_MAIN, "name": "MAIN-Order-Publish", "currentVersion": 3},
        {"componentId": PROC_CONSUMER, "name": "MAIN-Order-Consume", "currentVersion": 2},
        {"componentId": PROC_BROKEN, "name": "MAIN-Legacy-Publish", "currentVersion": 1},
    ],
}


class MockClient:
    """Stands in for BoomiClient, reproducing the account's real misbehaviour."""

    def __init__(self) -> None:
        self.config = Config()
        self.graphql_calls = 0
        self.rest_raw_calls = 0
        self.reference_queries = 0
        self.created: list[tuple[str, dict]] = []

    # -- GraphQL --------------------------------------------------------------

    def graphql(self, query, variables=None):
        self.graphql_calls += 1
        variables = variables or {}

        if "FieldTypes" in query:
            parent = variables["parent"]
            shapes = {
                "Query": {"eventStreamsTopics": "EventStreamsTopic",
                          "environments": "Environment"},
                "EventStreamsTopic": {"subscriptions": "EventStreamsSubscription"},
                "Environment": {"eventStreams": "EventStreamsEnvironment"},
                "EventStreamsEnvironment": {"tokens": "EventStreamsToken"},
            }
            return {"__type": {"fields": [
                {"name": f, "type": {"kind": "LIST", "name": None,
                                     "ofType": {"kind": "OBJECT", "name": t}}}
                for f, t in shapes.get(parent, {}).items()]}}

        if "TypeFields" in query:
            # Introspection over-reports, exactly as the real account does.
            catalogue = {
                "EventStreamsTopic": {"name", "description", "persistent", "partitions",
                                      "restProduceUrl", "restProduceSingleMsgUrl",
                                      "subscriptions"},
                "EventStreamsSubscription": {"name", "description", "type", "durable",
                                             "backlogCount"},
                "EventStreamsEnvironment": {"region", "restProduceBaseUrl", "tokens"},
                "EventStreamsToken": {"id", "name", "allowConsume", "allowProduce",
                                      "expirationTime", "expirationEditable",
                                      "createdTime", "description"},
                "EventStreamsTopicCreateInput": {"environmentId", "name", "persistent",
                                                 "partitions", "description"},
            }
            return {"__type": {"kind": "OBJECT",
                               "fields": [{"name": f} for f in catalogue.get(variables["name"], set())],
                               "inputFields": []}}

        if "MutationArgs" in query:
            return {"__type": {"fields": [
                {"name": "eventStreamsTopicCreate",
                 "args": [{"name": "input", "type": {"kind": "NON_NULL", "name": None,
                           "ofType": {"kind": "INPUT_OBJECT",
                                      "name": "EventStreamsTopicCreateInput"}}}]}]}}

        if "query Topics" in query:
            bad = sorted(
                f"eventStreamsTopics/{f}" for f in REFUSED_TOPIC_FIELDS
                if f"\n    {f}\n" in query
            )
            if bad:
                raise BoomiFieldError(
                    "; ".join(f"Validation error (FieldUndefined@[{p}]) : Field "
                              f"{p.split('/')[-1]} in type EventStreamsTopic is undefined"
                              for p in bad), set(bad))
            env_id = variables.get("environmentId")
            data = TOPICS.get(env_id, []) if env_id else [
                t for group in TOPICS.values() for t in group]
            return {"eventStreamsTopics": json.loads(json.dumps(data))}

        if "EnvironmentsWithEventStreams" in query:
            return {"environments": json.loads(json.dumps(ENVIRONMENTS))}

        if "CreateTopic" in query:
            self.created.append(("topic", variables["input"]))
            return {"eventStreamsTopicCreate": {"name": variables["input"]["name"]}}
        if "CreateSubscription" in query:
            self.created.append(("subscription", variables["input"]))
            return {"eventStreamsSubscriptionCreate": {"name": variables["input"]["name"],
                                                       "type": "NONE"}}
        if "CreateToken" in query:
            self.created.append(("token", variables["input"]))
            return {"eventStreamsTokenCreate": {"id": "new", "name": variables["input"]["name"]}}

        raise AssertionError(f"unexpected GraphQL query: {query[:120]}")

    # -- REST -----------------------------------------------------------------

    reference_api_available = True

    def rest_query_all(self, object_name, query_filter=None, max_results=None):
        expression = (query_filter or {}).get("QueryFilter", {}).get("expression", {})

        if object_name == "ComponentReference":
            if not self.reference_api_available:
                raise RuntimeError("ComponentReference is not queryable on this account")
            self.reference_queries += 1
            wanted_id = (expression.get("argument") or [None])[0]
            matches = [r for r in COMPONENT_REFERENCES if r["componentId"] == wanted_id]
            if not matches:
                return []
            # The real response nests one level deeper than a flat query result:
            # each result[] entry wraps a `references` array. Reading fields off the
            # wrapper finds nothing and looks exactly like "no references exist".
            return [{"@type": "ComponentReference", "references": matches}]

        assert object_name == "ComponentMetadata"
        for clause in expression.get("nestedExpression", []):
            if clause.get("property") == "type":
                rows = list(COMPONENT_META.get(clause["argument"][0], []))
                return rows[:max_results] if max_results is not None else rows
        return []

    def rest_raw(self, path, accept="application/xml"):
        self.rest_raw_calls += 1
        component_id = path.split("/")[-1]
        if component_id not in COMPONENT_XML:
            raise AssertionError(f"unexpected component fetch: {component_id}")
        return COMPONENT_XML[component_id]


# ---------------------------------------------------------------------------

CACHE = tempfile.mkdtemp(prefix="es-test-cache-")
es_inspect.CACHE_DIR = CACHE


def run() -> int:
    client = MockClient()
    es = EventStreamsClient(client)

    # -- schema self-healing --------------------------------------------------
    section("Schema self-healing (introspection over-reports, executor refuses)")
    topics = es.topics(TEST)
    check("recovers and returns data", len(topics) == 4, f"got {len(topics)}")
    check("dropped every refused field",
          es.rejected_fields() == {f"eventStreamsTopics/{f}" for f in REFUSED_TOPIC_FIELDS},
          f"got {es.rejected_fields()}")
    check("kept the fields that work", "description" in topics[0])
    check("supports() reflects the executor, not introspection",
          es.supports(es.TOPICS, "partitions", es.TOPIC_SEL) is False)
    before = client.graphql_calls
    es.topics(PROD)
    check("second query costs one call — pruning is remembered",
          client.graphql_calls - before == 1, f"cost {client.graphql_calls - before}")

    # -- discovery ------------------------------------------------------------
    section("Discovery")
    import es_discover

    inventory = es.inventory()
    check("lists every environment", len(inventory["environments"]) == 4)
    check("marks the unprovisioned one",
          any(not e["eventStreamsProvisioned"] for e in inventory["environments"]))
    rendered = es_discover.render(inventory)
    check("omits columns the account cannot return", "Partitions" not in rendered)
    check("flags the expired production token", "EXPIRED" in rendered and
          "SO_Failover_Consumer" in rendered)
    check("reports duplicate token names", "Duplicate token names" in rendered
          and "SO_Producer" in rendered)
    check("never prints token values", "tok-SO_Producer" not in rendered)

    state, label = es_discover.expiry_state("2026-04-16T18:29:59.000Z")
    check("expiry classified as expired", state == "expired" and "EXPIRED" in label)

    # -- find -----------------------------------------------------------------
    section("Find")
    import es_find

    result = es_find.search(es, "01_SalesForce_Orders", "any", exact=True)
    hit_envs = {h["environment"] for h in result["hits"]["topics"]}
    check("finds the topic in all three environments",
          hit_envs == {"Local Test Atm", "Production", "Test"}, f"got {hit_envs}")
    text = es_find.render(result)
    check("names the environment it is absent from", "absent from Sandbox" in text)

    partial = es_find.search(es, "student", "topic", exact=False)
    check("substring search works",
          {h["name"] for h in partial["hits"]["topics"]} ==
          {"Student_Data_Math", "Student_Data_Producer"})

    tokens_only = es_find.search(es, "SO_Failover_Consumer", "token", exact=True)
    check("kind filter restricts results",
          tokens_only["hits"]["topics"] == [] and len(tokens_only["hits"]["tokens"]) == 1)
    check("find reports expiry", "EXPIRED" in es_find.render(tokens_only))

    missing = es_find.search(es, "no_such_thing_anywhere", "any", exact=True)
    check("empty result is explained, not blank",
          "No topic, subscription, or token" in es_find.render(missing))

    # -- topology -------------------------------------------------------------
    section("Topology (topic names as the primary signal)")
    known = {str(t.get("name")) for t in TOPICS[TEST]} | {"01_SalesForce_Orders"}
    operations, census = es_inspect.find_operations(client, known, verbose=False)
    matched = {op["componentId"] for op in operations}
    check("matches an ES op despite an unconventional subType",
          OP_ORDERS_PRODUCE in matched and OP_ORDERS_CONSUME in matched)
    check("ignores an unrelated connector", OP_UNRELATED not in matched)
    check("still catches a runtime-resolved topic via subType hint",
          OP_DYNAMIC in matched)
    check("catches an operation whose topic exists nowhere (the orphan case)",
          OP_MISSING_TOPIC in matched,
          "topic-name matching alone cannot see this; the learned subType must")
    # Now that the topic field is read directly, an operation naming a topic that
    # exists nowhere is reported as "declared" -- a stronger, more specific answer
    # than the "by-connector" inference that was needed when the topic could only be
    # found by scanning for known names.
    check("labels an operation naming a non-existent topic as declared",
          next(o["confidence"] for o in operations
               if o["componentId"] == OP_MISSING_TOPIC) == "declared")
    check("labels the dynamic one as dynamic",
          next(o["confidence"] for o in operations if o["componentId"] == OP_DYNAMIC) == "dynamic")
    check("labels literal matches as exact",
          next(o["confidence"] for o in operations if o["componentId"] == OP_ORDERS_PRODUCE) == "exact")
    check("census reports every connector type present",
          census.get("salesforce") == 1 and census.get("custom_es_v2") == 3,
          f"got {census}")

    # A limit bounds XML reads, not the census. Capping the census would turn
    # "this account has no Event Streams connector" into a guess.
    capped_ops, capped_census = es_inspect.find_operations(client, known, limit=1, verbose=False)
    check("census stays complete when the scan is limited",
          capped_census == census, f"got {capped_census}")
    check("a limited scan reads the hinted component first",
          len(capped_ops) == 1 and capped_ops[0]["componentId"] == OP_DYNAMIC,
          f"got {[o['componentId'][:8] for o in capped_ops]}")
    # Narrowing to hinted components only would break the unconventional-subType
    # case entirely, so an unlimited scan must still read every component.
    check("an unlimited scan does not skip unhinted components",
          OP_ORDERS_PRODUCE in matched and OP_MISSING_TOPIC in matched)

    section("Process mapping via the dependency graph")
    client.reference_queries = 0
    usages = es_inspect.map_processes(client, operations, verbose=False)
    pairs = {(u["processName"], u["topic"], u["action"]) for u in usages}
    check("maps producer process to topic",
          ("MAIN-Order-Publish", "01_SalesForce_Orders", "produce") in pairs, f"got {pairs}")
    check("maps consumer process to topic",
          ("MAIN-Order-Consume", "01_SalesForce_Orders", "consume") in pairs)
    check("does not invent a link to the unrelated operation",
          not any(u["operationName"] == "SF Query Account" for u in usages))
    check("uses the dependency graph rather than scanning every process",
          client.reference_queries > 0)
    check("ignores a non-process parent",
          not any(u["processId"] == "77777777-eeee-4eee-8eee-777777777777" for u in usages))
    check("reads the nested result[].references[] shape",
          len(usages) >= 3, f"got {len(usages)} usages — a flat read would give 0")
    # ComponentReference does not recurse, so an operation reached through an
    # intermediate component is only found by walking a second level.
    check("follows an indirect reference through an intermediate component",
          any(u["processName"] == "MAIN-Legacy-Publish"
              and u["operationName"] == "eventstreams dynamic send" for u in usages),
          f"got {[(u['processName'], u['operationName']) for u in usages]}")

    section("Flattening the ComponentReference response")
    nested = [{"@type": "ComponentReference", "references": [
        {"parentComponentId": "p1", "componentId": "c1"},
        {"parentComponentId": "p2", "componentId": "c1"}]}]
    check("extracts entries from the nested wrapper",
          len(es_inspect.flatten_references(nested)) == 2)
    check("passes an already-flat row through unchanged",
          es_inspect.flatten_references([{"parentComponentId": "p1"}])
          == [{"parentComponentId": "p1"}])
    check("tolerates an empty response", es_inspect.flatten_references([]) == [])

    section("Process mapping falls back on a zero-yield graph")
    # A graph lookup that returns rows nobody can interpret must not be reported as
    # "nothing references this". This mock returns rows whose parent key is one the
    # code does not know, so resolution yields nothing and the scan must take over.
    original_refs = list(COMPONENT_REFERENCES)
    COMPONENT_REFERENCES[:] = [
        {"someUnknownKey": "not-a-uuid", "componentId": r["componentId"]}
        for r in original_refs
    ]
    try:
        recovered = es_inspect.map_processes(client, operations, verbose=False)
        recovered_pairs = {(u["processName"], u["topic"], u["action"]) for u in recovered}
        check("an uninterpretable graph response triggers the scan, not an empty answer",
              ("MAIN-Order-Publish", "01_SalesForce_Orders", "produce") in recovered_pairs,
              f"got {recovered_pairs}")
    finally:
        COMPONENT_REFERENCES[:] = original_refs

    section("Process mapping fallback (dependency graph unavailable)")
    MockClient.reference_api_available = False
    try:
        before_raw = client.rest_raw_calls
        fallback = es_inspect.map_processes(client, operations, verbose=False)
        fallback_pairs = {(u["processName"], u["topic"], u["action"]) for u in fallback}
        check("falls back to scanning process XML",
              ("MAIN-Order-Publish", "01_SalesForce_Orders", "produce") in fallback_pairs,
              f"got {fallback_pairs}")
        check("the fallback finds the same links as the graph",
              fallback_pairs == pairs,
              f"graph={pairs}\n           scan={fallback_pairs}")
        # The whole point of the UUID fix: these processes reference operations via
        # operationId=, not componentId=, so a regex naming the attribute finds nothing.
        check("matches references regardless of the attribute carrying the UUID",
              len(fallback) == len(usages), f"{len(fallback)} vs {len(usages)}")
    finally:
        MockClient.reference_api_available = True

    before_raw = client.rest_raw_calls
    es_inspect.find_operations(client, known, verbose=False)
    check("component XML is cached across calls",
          client.rest_raw_calls == before_raw, f"{client.rest_raw_calls - before_raw} refetches")

    # -- health ---------------------------------------------------------------
    section("Action classification against real component XML")
    real_produce = es_operation_xml("01_SalesForce_Orders", "PRODUCE")
    real_consume = es_operation_xml("01_SalesForce_Orders", "CONSUME", "Shared")
    real_listen = es_operation_xml("01_SalesForce_Orders", "LISTEN")
    check("reads customOperationType=PRODUCE",
          es_inspect.classify_action(real_produce) == "produce")
    check("reads customOperationType=CONSUME",
          es_inspect.classify_action(real_consume) == "consume")
    check("reads customOperationType=LISTEN",
          es_inspect.classify_action(real_listen) == "listen")
    # operationType is always EXECUTE regardless of direction. A pattern loose enough
    # to match it would classify every operation as a producer.
    check("is not fooled by operationType=EXECUTE",
          es_inspect.classify_action(real_consume) != "produce")
    check("extracts the topic from the topic field",
          es_inspect.extract_topic(real_produce, {"01_SalesForce_Orders"})
          == ("01_SalesForce_Orders", "exact"))
    check("reports a declared topic that exists nowhere as 'declared'",
          es_inspect.extract_topic(
              es_operation_xml("Gone_Topic", "PRODUCE"), {"01_SalesForce_Orders"})
          == ("Gone_Topic", "declared"),
          "this is how orphaned operations are found")
    check("reads the access mode",
          es_inspect.access_mode(real_consume) == "Shared")

    section("Action classification fallbacks")
    check("reads the action from XML when present",
          es_inspect.classify_action('<op actionType="SEND"/>') == "produce")
    check("falls back to the operation name",
          es_inspect.classify_action("<op/>", "Send Sales Orders to Event Broker")
          == "produce")
    check("recognises retrieve as consuming",
          es_inspect.classify_action("<op/>", "Retrieve Messages (Shared) from Event Broker")
          == "consume")
    check("recognises listen",
          es_inspect.classify_action("<op/>", "Retrieve Messages (Listen) from Event Broker")
          == "listen")
    check("XML wins over the name when they disagree",
          es_inspect.classify_action('<op actionType="SEND"/>', "Consume Something")
          == "produce")
    check("admits when it cannot tell",
          es_inspect.classify_action("<op/>", "Op 1") == "unknown")

    section("Findings are suppressed when direction is unknown")
    blind_usages = [
        {"processName": "P1", "topic": "Student_Data_Math", "action": "unknown",
         "operationName": "Op", "confidence": "exact"},
        {"processName": "P2", "topic": "Student_Data_Producer", "action": "unknown",
         "operationName": "Op", "confidence": "exact"},
    ]
    blind = es_inspect.analyse(TOPICS[TEST], blind_usages, [])
    blind_kinds = {f["finding"] for f in blind}
    check("does not claim a topic has no publisher when direction is unreadable",
          "No process publishes to this topic" not in blind_kinds, f"got {blind_kinds}")
    check("says why those checks were skipped",
          any("direction could not be determined" in f["finding"] for f in blind))

    classified = es_inspect.analyse(
        TOPICS[TEST],
        [{**u, "action": "consume"} for u in blind_usages],
        [],
    )
    check("still reports a missing publisher when direction IS known",
          any(f["finding"] == "No process publishes to this topic" for f in classified))

    section("Health analysis")
    findings = es_inspect.analyse(TOPICS[TEST], usages, TOKENS[PROD])
    kinds = {(f["subject"], f["finding"]) for f in findings}
    check("flags the orphaned operation",
          any("Orphaned" in k[1] and "Never_Promoted" in k[0] for k in kinds), f"got {kinds}")
    check("groups orphans by topic rather than one per process",
          len([f for f in findings if "Orphaned" in f["finding"]]) == 1,
          "several processes on one missing topic is one problem, not several")
    check("flags a topic with no subscriptions",
          ("Orphan_Free_Topic", "No subscriptions") in kinds)
    check("flags a backlog",
          any("Backlog of 1" in k[1] for k in kinds))
    check("flags the expired token",
          any("expired" in k[1].lower() and k[0] == "SO_Failover_Consumer" for k in kinds))
    check("does not report the dynamic topic as orphaned",
          not any("DPP_TargetTopic" in k[0] for k in kinds))
    severities = [f["severity"] for f in findings]
    check("high severity findings come first",
          severities == sorted(severities, key=lambda s: {"high": 0, "medium": 1, "low": 2}[s]))

    # -- migration ------------------------------------------------------------
    section("Migration: plan, filters, guardrail, apply, verify")
    src = {"id": TEST, "name": "Test"}
    tgt = {"id": LOCAL, "name": "Local Test Atm"}
    prod = {"id": PROD, "name": "Production"}

    plan = es_migrate.build_plan(es, src, tgt)
    names = {t["name"] for t in plan["topics"]}
    check("plans only what the target lacks",
          names == {"Student_Data_Math", "Student_Data_Producer", "Orphan_Free_Topic"},
          f"got {names}")
    check("skips what already exists",
          any(s["name"] == "01_SalesForce_Orders" for s in plan["skipped"]))
    check("omits fields the account rejects",
          all("partitions" not in t or t["partitions"] is None for t in plan["topics"]))

    selective = es_migrate.build_plan(es, src, tgt, only_topics=["Student_Data_Math"])
    check("--topics filter", [t["name"] for t in selective["topics"]] == ["Student_Data_Math"])

    by_sub = es_migrate.build_plan(es, src, tgt, only_subscriptions=["Producer"])
    check("--subscriptions filter by bare name",
          [s["name"] for s in by_sub["subscriptions"]] == ["Producer"],
          f"got {[s['name'] for s in by_sub['subscriptions']]}")

    by_path = es_migrate.build_plan(es, src, tgt,
                                    only_subscriptions=["Student_Data_Producer/Test"])
    check("--subscriptions filter by topic/name",
          [s["name"] for s in by_path["subscriptions"]] == ["Test"])

    by_token = es_migrate.build_plan(es, src, tgt, only_tokens=["SO_Producer"])
    check("--tokens filter", [t["name"] for t in by_token["tokens"]] == [])  # exists in target

    dup_plan = es_migrate.build_plan(es, {"id": LOCAL, "name": "Local Test Atm"},
                                     {"id": PROD, "name": "Production"})
    # Production is protected, but build_plan itself does not guard -- plan/apply do.
    check("duplicate token names collapse to one",
          [t["name"] for t in dup_plan["tokens"]].count("SO_Producer") <= 1)
    check("duplicates are reported, not silently dropped",
          dup_plan["duplicateSourceTokenNames"] == ["SO_Producer"])

    try:
        es_migrate.guard_target(es, prod)
        check("protected target refused", False, "no exception")
    except es_migrate.ProtectedEnvironmentError:
        check("protected target refused", True)
    try:
        es_migrate.guard_target(es, tgt)
        check("unprotected target allowed", True)
    except es_migrate.ProtectedEnvironmentError:
        check("unprotected target allowed", False, "raised")

    rendered_plan = es_migrate.render_plan(plan)
    check("plan explains subscription type", "will report `NONE`" in rendered_plan
          or "report `NONE`" in rendered_plan)

    class Args:
        plan = None
        confirm = False

    plan_file = os.path.join(CACHE, "plan.json")
    with open(plan_file, "w", encoding="utf-8") as handle:
        json.dump(plan, handle)

    created_before = len(client.created)
    Args.plan = plan_file
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        es_migrate.cmd_apply(Args, es=es)
    check("apply without --confirm writes nothing", len(client.created) == created_before)
    check("dry run says so", "Re-run with --confirm" in buffer.getvalue())

    Args.confirm = True
    with redirect_stdout(io.StringIO()):
        es_migrate.cmd_apply(Args, es=es)
    created_kinds = [k for k, _ in client.created]
    check("apply with --confirm creates topics", created_kinds.count("topic") == 3,
          f"created {created_kinds}")
    check("apply creates subscriptions", "subscription" in created_kinds)
    check("no delete was ever attempted",
          not any("delete" in k for k in created_kinds))

    check("client exposes no delete method",
          not [m for m in dir(EventStreamsClient)
               if any(w in m.lower() for w in ("delete", "remove", "destroy", "purge"))])

    section("Verify")

    class VArgs:
        source = "Test"
        target = "Local Test Atm"

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = es_migrate.cmd_verify(VArgs, es=es)
    output = buffer.getvalue()
    check("verify reports differences with a non-zero exit", code == 2, f"exit {code}")
    check("verify lists a missing topic",
          "Student_Data_Math" in output or "Orphan_Free_Topic" in output)
    check("verify does not compare subscription type",
          "not compared" in output)
    check("verify admits it could not check config on this account",
          "does not expose" in output,
          "should say persistent/partitions were unavailable rather than imply a match")

    class VSame:
        source = "Test"
        target = "Test"

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = es_migrate.cmd_verify(VSame, es=es)
    check("verify of an environment against itself passes", code == 0, f"exit {code}")

    # -- report ---------------------------------------------------------------
    section("Combined report")
    import es_report

    report = es_report.build(es, client, None, None, quiet=True)
    for heading in ("# Event Streams report", "## Summary", "## Cross-environment drift",
                    "## Tokens", "## Inventory by environment", "## Health"):
        check(f"report contains {heading!r}", heading in report)
    check("drift matrix names a partially-promoted topic",
          "Student_Data_Math" in report and "exist in some environments but not others" in report)
    check("report surfaces the expired token", "SO_Failover_Consumer" in report)
    check("report counts environments", "Environments with Event Streams: **3**" in report)

    scoped = es_report.build(es, client, "Test", None, quiet=True)
    check("scoped report adds the topology section", "## Topology — Test" in scoped)
    check("scoped report maps a process", "MAIN-Order-Publish" in scoped)

    # -- live monitoring --------------------------------------------------------
    section("Live health analysis")
    import es_monitor

    live = [
        {"name": "stalled", "producerCount": 0, "subscriptions": [
            {"name": "s1", "backlogCount": 5, "activeConsumerCount": 0,
             "deadLetterBacklogCount": 0, "retryBacklogCount": 0}]},
        {"name": "slow", "producerCount": 1, "subscriptions": [
            {"name": "s2", "backlogCount": 5, "activeConsumerCount": 2,
             "deadLetterBacklogCount": 0, "retryBacklogCount": 0}]},
        {"name": "failing", "producerCount": 1, "subscriptions": [
            {"name": "s3", "backlogCount": 0, "activeConsumerCount": 1,
             "deadLetterBacklogCount": 4, "retryBacklogCount": 2}]},
        {"name": "idle", "producerCount": 0, "subscriptions": [
            {"name": "s4", "backlogCount": 0, "activeConsumerCount": 0,
             "deadLetterBacklogCount": 0, "retryBacklogCount": 0}]},
    ]
    lf = es_monitor.analyse(live)

    def find(subject: str, phrase: str) -> dict | None:
        # One subscription can raise several findings -- a dead letter backlog and a
        # retry backlog, say -- so look one up by what it says, not just by subject.
        return next(
            (f for f in lf if f["subject"] == subject and phrase in f["finding"]), None
        )

    # The distinction the consumer count exists to make.
    stalled = find("stalled/s1", "no active consumer")
    check("backlog with no consumer is high severity",
          stalled and stalled["severity"] == "high")
    slow = find("slow/s2", "consumer(s) attached")
    check("backlog with a consumer is only medium",
          slow and slow["severity"] == "medium",
          f"got {slow['severity'] if slow else 'no finding'}")
    dlq = find("failing/s3", "dead letter queue")
    check("dead letters are flagged high", dlq and dlq["severity"] == "high",
          f"got {dlq['severity'] if dlq else 'no dead-letter finding'}")
    retry = find("failing/s3", "awaiting retry")
    check("retry backlog is reported separately from dead letters",
          retry and retry["severity"] == "medium" and dlq is not retry)
    check("idle subscriptions collapse into one line",
          sum(1 for f in lf if "idle subscription" in f["subject"]) == 1)
    check("an idle subscription is not reported individually",
          not any(f["subject"] == "idle/s4" for f in lf),
          "would bury real findings in a quiet environment")

    section("Truncated list detection")
    # The GraphQL list fields take no pagination arguments and the response carries
    # no hasNextPage or totalCount. If a large list were ever capped server-side,
    # nothing in the reply would say so — the output would look complete and be wrong.
    # The environment's own counts are the only independent check available.
    complete = es.check_completeness(
        {"name": "Test", "eventStreams": {"topicCount": 2, "subscriptionCount": 3}},
        [{"name": "a", "subscriptions": [{"name": "s1"}, {"name": "s2"}]},
         {"name": "b", "subscriptions": [{"name": "s3"}]}],
    )
    check("agreeing counts produce no warning", complete == [], f"got {complete}")

    truncated = es.check_completeness(
        {"name": "Test", "eventStreams": {"topicCount": 150}},
        [{"name": f"t{i}", "subscriptions": []} for i in range(100)],
    )
    check("a capped topic list is detected",
          any("150" in w and "100" in w for w in truncated), f"got {truncated}")
    check("the warning says the result is unreliable",
          any("incomplete" in w for w in truncated))

    sub_short = es.check_completeness(
        {"name": "Test", "eventStreams": {}},
        [{"name": "a", "subscriptionCount": 40, "subscriptions": [{"name": "s1"}]}],
    )
    check("a capped subscription list is detected per topic",
          any("40" in w and "a" in w for w in sub_short), f"got {sub_short}")

    silent = es.check_completeness({"name": "Test", "eventStreams": {}}, [{"name": "a"}])
    check("accounts without count fields produce no false alarm", silent == [])

    inv = es.inventory()
    check("inventory carries the completeness check for every environment",
          all("completenessWarnings" in e for e in inv["environments"]
              if e["eventStreamsProvisioned"]))

    section("Message indices are one-based")
    # startIndex=0 is rejected by the API with a message about invalid indices, which
    # reads like a range problem rather than an off-by-one.
    captured = {}

    class IdxClient(MockClient):
        def graphql(self, query, variables=None):
            if "Messages" in (query or "") and variables and "input" in variables:
                captured.update(variables["input"])
                return {"eventStreamsMessages": [], "eventStreamsDeadLetterQueueMessages": []}
            return super().graphql(query, variables)

    idx_es = EventStreamsClient(IdxClient())
    idx_es.messages("env", "topic", "sub", start=0, end=0)
    check("a zero start index is corrected to one", captured.get("startIndex") == 1,
          f"sent {captured.get('startIndex')}")
    check("subscriptionName is always sent", captured.get("subscriptionName") == "sub",
          "it is ID! -- required, not optional")

    # -- the safety boundary ----------------------------------------------------
    section("Destructive capability is isolated from the read client")
    import es_admin_ops

    read_side = [m for m in dir(EventStreamsClient)
                 if any(w in m.lower() for w in
                        ("delete", "remove", "destroy", "purge", "clear", "update"))]
    check("EventStreamsClient has no mutating method at all", read_side == [],
          f"found {read_side} -- the read skills import this class")
    admin_side = {m for m in dir(es_admin_ops.EventStreamsAdmin)}
    for method in ("delete_topic", "delete_subscription", "delete_token",
                   "clear_backlog", "update_topic", "update_token"):
        check(f"EventStreamsAdmin provides {method}", method in admin_side)
    check("es_client does not import the admin module",
          "es_admin_ops" not in open("es_client.py").read(),
          "the isolation only holds if the dependency runs one way")

    section("Protected environments are refused for every write, not just deletes")
    os.environ["BOOMI_PROTECTED_ENVIRONMENTS"] = "Production"
    admin = es_admin_ops.EventStreamsAdmin.__new__(es_admin_ops.EventStreamsAdmin)
    admin.client = MockClient()
    for op in ("delete-topic", "update-topic", "clear-backlog"):
        try:
            admin.guard({"name": "Production", "id": PROD}, op)
            check(f"{op} refused on a protected environment", False, "no exception")
        except es_admin_ops.ProtectedEnvironmentError:
            check(f"{op} refused on a protected environment", True)
    try:
        admin.guard({"name": "Test", "id": TEST}, "delete-topic")
        check("unprotected environment allowed", True)
    except es_admin_ops.ProtectedEnvironmentError:
        check("unprotected environment allowed", False, "raised")

    # -- pagination ------------------------------------------------------------
    # This exercises the real rest_query_all against a faked transport. The mock
    # client above stubs rest_query_all out entirely, which is why a broken
    # queryMore body format shipped: the paging code was never run by a test.
    section("Platform API pagination")

    from boomi_auth import BoomiClient

    recorded: list[tuple[str, str, bytes | None]] = []

    def fake_transport(url, *, method="GET", headers=None, body=None):
        recorded.append((method, url, body))
        if url.endswith("/auth/jwt/generate/trainingaccount-6HOMIV"):
            return (200, "fake.jwt.token")
        if url.endswith("/ComponentMetadata/query"):
            return (200, json.dumps({
                "result": [{"componentId": f"c{i}"} for i in range(100)],
                "queryToken": "TOKEN-PAGE-2",
            }))
        if url.endswith("/ComponentMetadata/queryMore"):
            page = len([r for r in recorded if r[1].endswith("queryMore")])
            if page == 1:
                return (200, json.dumps({
                    "result": [{"componentId": f"d{i}"} for i in range(100)],
                    "queryToken": "TOKEN-PAGE-3",
                }))
            return (200, json.dumps({"result": [{"componentId": "final"}]}))
        raise AssertionError(f"unexpected url {url}")

    boomi_auth._request = fake_transport
    try:
        paging_client = BoomiClient(Config())
        rows = paging_client.rest_query_all("ComponentMetadata")
        check("follows queryMore to the end", len(rows) == 201, f"got {len(rows)}")

        # Boomi's spec declares text/plain for every queryMore path: the body is the
        # raw token, not JSON. An earlier version of this assertion required a
        # JSON-quoted string, which encoded my own wrong guess as a requirement --
        # a test can protect a bug as easily as it can catch one.
        more_bodies = [b for m, u, b in recorded if u.endswith("queryMore")]
        check("queryMore sends the raw token as plain text",
              more_bodies and more_bodies[0] == b"TOKEN-PAGE-2",
              f"sent {more_bodies[0] if more_bodies else None!r}")
        check("queryMore body has no JSON quoting",
              all(not (b or b"").startswith(b'"') for b in more_bodies))
        check("queryMore body is not a wrapped object",
              all(b"queryToken" not in (b or b"") for b in more_bodies))

        recorded.clear()
        capped = paging_client.rest_query_all("ComponentMetadata", max_results=30)
        check("max_results caps the result", len(capped) == 30, f"got {len(capped)}")
        check("max_results stops paging instead of slicing afterwards",
              not any(u.endswith("queryMore") for _, u, _ in recorded),
              "queryMore was called despite the first page covering the cap")
    finally:
        boomi_auth._request = _no_network

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    try:
        code = run()
    finally:
        shutil.rmtree(CACHE, ignore_errors=True)
    sys.exit(code)
