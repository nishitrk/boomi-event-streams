"""
Reconstruct which Boomi processes talk to which Event Streams topics.

Boomi does not expose this. The link only exists inside component XML, so it has to
be rebuilt from the Platform REST API.

The important design decision here is how an Event Streams operation is recognised.
Matching on connector subType is the obvious approach and it is fragile: the
identifier has varied across Boomi releases, and a customer account can surprise you.
Guessing wrong means the scan silently finds nothing, which looks identical to "no
process uses Event Streams".

So the primary signal is the topic names themselves. They are already known from
GraphQL, they are specific strings unlikely to appear by accident, and a component
whose XML references one is by definition talking to that topic — whatever the
connector is called. subType matching is kept as a secondary pass, which also catches
operations whose topic is set dynamically at runtime and therefore never appears
literally in the XML.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

from boomi_auth import BoomiAPIError, BoomiClient

CACHE_DIR = ".es-cache"

# How many operations to read per connector type when deciding whether that type is
# Event Streams. Reading one risks landing on an operation whose topic is set at
# runtime and learning nothing; three is enough to be confident without being
# expensive, since the cost is per connector type rather than per operation.
PROBE_PER_SUBTYPE = 3

# Secondary signal only. Extendable via BOOMI_ES_CONNECTOR_TYPES for accounts whose
# connector identifier is not in this list.
#
# "events" earns its place from a real account whose Event Streams connector is
# published as `officialboomi-X3979C-events-prod`. Account-published connectors follow
# `officialboomi-<account>-<connector>-<stage>`, so the useful part of the identifier
# is a middle segment -- matching on substrings rather than whole strings is what makes
# that reachable.
DEFAULT_CONNECTOR_HINTS = (
    "eventstreams",
    "event_streams",
    "event-streams",
    "eventstream",
    "-events-",
    "pulsar",
)

# Any UUID, wherever it appears.
#
# The earlier version looked for componentId="..." specifically, and matched nothing:
# Boomi process XML references a connector operation through other attributes
# entirely. Naming the attribute meant guessing the serialisation format, which is the
# same mistake as guessing the GraphQL schema. Collecting every UUID and intersecting
# with the set of known operation IDs needs no such guess -- a UUID that is a known
# Event Streams operation is a reference to it, whatever attribute carries it.
UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# The Event Streams connector declares direction in `customOperationType`
# (PRODUCE / CONSUME / LISTEN). Note the decoy alongside it: `operationType` is
# always "EXECUTE" regardless of direction, so a pattern loose enough to match it
# classifies every operation as a producer. Specificity matters more than coverage
# here -- an attribute that is always the same value is worse than no attribute.
ACTION_PATTERNS = (
    re.compile(r"\bcustomOperationType=\"([^\"]+)\"", re.IGNORECASE),
    re.compile(r"\baction(?:Type)?=\"([^\"]+)\"", re.IGNORECASE),
)

# Configuration lives in <field id="..." value="..."/> elements, so the topic is at
# id="topic". The looser patterns after it are kept for other connectors and older
# component shapes.
TOPIC_FIELD_PATTERN = re.compile(
    r"<field\b[^>]*\bid=\"topic(?:Name)?\"[^>]*\bvalue=\"([^\"]*)\"", re.IGNORECASE
)

TOPIC_XML_PATTERNS = (
    re.compile(r"<[^>]*\bname=\"topic(?:Name)?\"[^>]*>([^<]+)<", re.IGNORECASE),
    re.compile(r"\btopic(?:Name)?=\"([^\"]+)\"", re.IGNORECASE),
    re.compile(r"<topic(?:Name)?>([^<]+)</topic", re.IGNORECASE),
)

# Access mode -- Exclusive, Shared, Failover -- as configured on the operation.
ACCESS_MODE_PATTERN = re.compile(
    r"<field\b[^>]*\bid=\"(?:producer|consumer)AccessMode\"[^>]*\bvalue=\"([^\"]*)\"",
    re.IGNORECASE,
)

# A topic name set from a dynamic process property rather than hardcoded.
DYNAMIC_MARKERS = ("DPP_", "DDP_", "{", "$")


def connector_hints() -> tuple[str, ...]:
    extra = os.environ.get("BOOMI_ES_CONNECTOR_TYPES", "")
    return DEFAULT_CONNECTOR_HINTS + tuple(
        h.strip().lower() for h in extra.split(",") if h.strip()
    )


def cache_path(component_id: str, version: Any) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{component_id}.v{version or 'x'}.xml")


def fetch_xml(client: BoomiClient, component_id: str, version: Any = None) -> str:
    path = cache_path(component_id, version)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    xml = client.rest_raw(f"Component/{component_id}")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(xml)
    return xml


def query_components(
    client: BoomiClient, component_type: str, limit: int | None = None
) -> list[dict]:
    """List components of one type, stopping paging once `limit` is reached.

    The limit is pushed down into pagination rather than applied afterwards. Slicing
    the result later still pays for every page in the account, which defeats the point
    of asking for a bounded scan.
    """
    return client.rest_query_all(
        "ComponentMetadata",
        {
            "QueryFilter": {
                "expression": {
                    "operator": "and",
                    "nestedExpression": [
                        {"operator": "EQUALS", "property": "type", "argument": [component_type]},
                        {"operator": "EQUALS", "property": "deleted", "argument": ["false"]},
                    ],
                }
            }
        },
        max_results=limit,
    )


def extract_topic(xml: str, known_topics: set[str]) -> tuple[str | None, str]:
    """Find the topic an operation refers to, and how confidently.

    The connector stores it in a <field id="topic" value="..."/> element, so that is
    read first -- and it is worth more than a literal scan for known names, because
    it also returns topics that do *not* exist in this environment. Those are the
    orphaned operations, the most valuable thing this scan finds, and a
    known-names-only approach is structurally blind to them.

    Confidence levels:
      exact    -- declared, and the topic exists in this environment
      declared -- declared, but no such topic here (an orphan)
      dynamic  -- resolved at runtime; the value is an expression, not a topic
      pattern  -- inferred from a looser match, treat with suspicion
    """
    match = TOPIC_FIELD_PATTERN.search(xml)
    if match:
        value = match.group(1).strip()
        if not value or any(marker in value for marker in DYNAMIC_MARKERS):
            return (value or None, "dynamic")
        return (value, "exact" if value in known_topics else "declared")

    for topic in sorted(known_topics, key=len, reverse=True):
        if topic and topic in xml:
            return (topic, "exact")

    for pattern in TOPIC_XML_PATTERNS:
        match = pattern.search(xml)
        if match:
            value = match.group(1).strip()
            if any(marker in value for marker in DYNAMIC_MARKERS):
                return (value, "dynamic")
            return (value, "pattern")
    return (None, "unknown")


def _classify_words(text: str) -> str | None:
    lowered = text.lower()
    if "listen" in lowered:
        return "listen"
    if any(w in lowered for w in ("send", "produce", "publish", "upsert", "write")):
        return "produce"
    if any(w in lowered for w in ("consume", "receive", "retrieve", "subscribe", "get", "query")):
        return "consume"
    return None


def classify_action(xml: str, name: str = "") -> str:
    """Decide whether an operation produces, consumes, or listens.

    The connector's own declaration wins: `customOperationType` says PRODUCE,
    CONSUME, or LISTEN outright. The operation's name is a fallback, because Boomi
    operations are conventionally named for what they do -- and it is only a
    fallback, since a name is a human label and can lie.

    Returning "unknown" honestly matters. Downstream analysis suppresses publisher
    and consumer findings when direction is unreadable, because "no process
    publishes to this topic" is a damaging thing to assert on a guess.
    """
    raw = ""
    for pattern in ACTION_PATTERNS:
        match = pattern.search(xml)
        if not match:
            continue
        raw = raw or match.group(1)
        classified = _classify_words(match.group(1))
        if classified:
            return classified

    from_name = _classify_words(name)
    if from_name:
        return from_name

    return raw.lower() or "unknown"


def access_mode(xml: str) -> str | None:
    """Exclusive / Shared / Failover, as configured on the operation."""
    match = ACCESS_MODE_PATTERN.search(xml)
    return match.group(1) if match else None


def _census_from(metadata: list[dict]) -> dict[str, int]:
    census: dict[str, int] = {}
    for meta in metadata:
        sub_type = str(meta.get("subType", "")) or "(none)"
        census[sub_type] = census.get(sub_type, 0) + 1
    return census


def connector_census(client: BoomiClient) -> dict[str, int]:
    """Connector operations counted by subType.

    One paged metadata query, no component bodies -- which is what makes it safe to
    run on its own. `--diagnose` answers "why did the scan find nothing", and that
    answer must not cost a full scan: anyone reaching for it is already stuck, and
    telling them to wait several minutes for a list they could have had in seconds
    is how a diagnostic becomes the thing people stop running.
    """
    return _census_from(query_components(client, "connector-action"))


def find_operations(
    client: BoomiClient,
    known_topics: set[str],
    limit: int | None = None,
    verbose: bool = True,
    exhaustive: bool = False,
) -> tuple[list[dict], dict[str, int]]:
    """Locate Event Streams operations. Returns (operations, subType census).

    Two passes, because matching on topic names alone has a blind spot that matters:
    an operation pointing at a topic which does not exist in this environment cannot
    match any known name — and that is precisely the orphaned-operation case, the
    most valuable thing this scan can find.

    So pass one identifies operations by literal topic match and learns which
    connector subTypes those operations use. Pass two admits every operation sharing
    a confirmed subType. If `custom_es_v2` demonstrably talks to a real topic, then
    all `custom_es_v2` operations are Event Streams operations, whatever their topic
    turns out to be. The account teaches us its own connector naming rather than us
    guessing it.

    The census is returned even when nothing matches, because "here are the connector
    types this account actually has" is the answer to "why did the scan find nothing".
    """
    hints = connector_hints()

    # Metadata is cheap -- it is one paged query and carries no component bodies -- so
    # always take all of it. That makes the census complete regardless of `limit`,
    # which matters because the census is what answers "does this account even have
    # an Event Streams connector". Capping it turns a definitive answer into a
    # misleading one.
    all_metadata = query_components(client, "connector-action")
    census = _census_from(all_metadata)

    # Reading each component's XML is the expensive part, so the question is which
    # ones are worth reading. The insight that makes this cheap: Event Streams is a
    # property of a *connector type*, not of individual operations. So decide per
    # connector type, and the cost scales with the number of types (seventeen on a
    # real account) rather than the number of operations (794).
    #
    #   1. Read every operation on a connector type that looks like Event Streams.
    #   2. Read a few operations from each remaining type, just enough to tell whether
    #      that type is Event Streams. This is what stops step 1 from being a trap:
    #      an account running Event Streams through two connectors -- an old one and a
    #      new one, say -- would otherwise only reveal the recognisable one.
    #   3. For any type now confirmed, read all its remaining operations.
    #
    # `exhaustive` reads everything, for the rare case where a connector's sampled
    # operations all set their topic at runtime and so prove nothing.
    def is_hinted(meta: dict) -> bool:
        return any(
            h in str(meta.get("subType", "")).lower() or h in str(meta.get("name", "")).lower()
            for h in hints
        )

    def sub_type_of(meta: dict) -> str:
        return str(meta.get("subType", "")) or "(none)"

    by_subtype: dict[str, list[dict]] = {}
    for meta in all_metadata:
        by_subtype.setdefault(sub_type_of(meta), []).append(meta)

    hinted_subtypes = {s for s, group in by_subtype.items() if any(is_hinted(m) for m in group)}

    if exhaustive:
        candidates, scope = list(all_metadata), "exhaustive"
    else:
        candidates = [m for m in all_metadata if sub_type_of(m) in hinted_subtypes]
        # Probe the connector types that gave nothing away by name.
        for sub_type, group in sorted(by_subtype.items()):
            if sub_type not in hinted_subtypes:
                candidates.extend(group[:PROBE_PER_SUBTYPE])
        scope = (
            f"{len(hinted_subtypes)} matched type(s) in full, "
            f"{PROBE_PER_SUBTYPE} probe(s) per other type"
        )
    if limit:
        candidates = candidates[:limit]

    if verbose:
        print(
            f"  {len(all_metadata)} operations across {len(census)} connector type(s); "
            f"reading {len(candidates)} — {scope}",
            file=sys.stderr,
        )

    examined: list[dict] = []
    seen_ids: set[str] = set()

    def examine(batch: list[dict], label: str) -> None:
        for index, meta in enumerate(batch, start=1):
            component_id = meta.get("componentId")
            if not component_id or component_id in seen_ids:
                continue
            seen_ids.add(component_id)
            if verbose and index % 50 == 0:
                print(f"  read {index}/{len(batch)} {label}", file=sys.stderr)
            try:
                xml = fetch_xml(client, component_id, meta.get("currentVersion"))
            except BoomiAPIError:
                continue
            topic, confidence = extract_topic(xml, known_topics)
            examined.append(
                {
                    "componentId": component_id,
                    "name": meta.get("name"),
                    "subType": str(meta.get("subType", "")) or "(none)",
                    "topic": topic,
                    "confidence": confidence,
                    "action": classify_action(xml, str(meta.get("name", ""))),
                    "accessMode": access_mode(xml),
                    "hinted": is_hinted(meta),
                }
            )

    examine(candidates, "operations")

    def confirmed_from(entries: list[dict]) -> set[str]:
        # "declared" counts as confirmation: the operation named a topic through the
        # connector's own topic field, which identifies the connector as Event
        # Streams whether or not that topic exists in this environment.
        found = {
            op["subType"] for op in entries
            if op["confidence"] in ("exact", "declared") or (op["hinted"] and op["topic"])
        }
        found.discard("(none)")
        return found

    confirmed_subtypes = confirmed_from(examined)

    # Tier 3: pull in the siblings of anything confirmed, so a narrow start still
    # produces a complete answer for the connectors that matter.
    if confirmed_subtypes and not exhaustive:
        remaining = [
            m for m in all_metadata
            if str(m.get("subType", "")) in confirmed_subtypes
            and m.get("componentId") not in seen_ids
        ]
        if remaining:
            if verbose:
                print(
                    f"  confirmed {', '.join(sorted(confirmed_subtypes))}; "
                    f"reading {len(remaining)} sibling operation(s)",
                    file=sys.stderr,
                )
            examine(remaining, "siblings")
            confirmed_subtypes = confirmed_from(examined)

    # Pass two: admit anything with a confirmed subType, or independently identified.
    operations: list[dict] = []
    for op in examined:
        by_topic = op["confidence"] in ("exact", "declared")
        by_hint = op["hinted"] and op["topic"]
        by_learned_subtype = op["subType"] in confirmed_subtypes and op["topic"]
        if by_topic or by_hint or by_learned_subtype:
            entry = {k: v for k, v in op.items() if k != "hinted"}
            # An operation admitted only because its connector was confirmed
            # elsewhere, pointing at a topic no environment has, is the orphan case.
            if not by_topic and not by_hint:
                entry["confidence"] = "by-connector"
            operations.append(entry)

    return operations, census


def _process_names(client: BoomiClient) -> dict[str, str]:
    """{componentId -> name} for every process. One cheap metadata query."""
    return {
        str(m.get("componentId")): str(m.get("name"))
        for m in query_components(client, "process")
        if m.get("componentId")
    }


# Field names that might carry the referencing (parent) component's ID. Documented as
# parentComponentId; the alternatives and the UUID-shape fallback below cost nothing
# and mean a naming surprise degrades into a slower answer rather than a wrong one.
PARENT_ID_KEYS = (
    "parentComponentId",
    "parentComponentid",
    "parentId",
    "id",
)

# How many levels of the reference graph to walk.
#
# ComponentReference returns immediate references only -- it does not recurse the way
# the UI's "Show Where Used" does. A process usually references its connector
# operation directly, but not always, so one extra hop catches an operation reached
# through an intermediate component. Two is enough in practice and bounds the cost.
MAX_REFERENCE_DEPTH = 2


def flatten_references(rows: list[dict]) -> list[dict]:
    """Pull the reference entries out of a ComponentReference query result.

    The response nests one level deeper than a query result usually does: each
    result[] entry is a wrapper whose only content is a `references` array, so the
    fields live at result[].references[], not result[]. Reading them off the wrapper
    finds nothing and looks exactly like "this component is unreferenced" -- which is
    precisely the wrong answer to report confidently.

    Rows that are already flat are passed through, so this stays correct if the shape
    ever changes.
    """
    flattened: list[dict] = []
    for row in rows:
        nested = row.get("references")
        if isinstance(nested, list):
            flattened.extend(r for r in nested if isinstance(r, dict))
        else:
            flattened.append(row)
    return flattened


def _map_via_references(
    client: BoomiClient,
    by_id: dict[str, dict],
    verbose: bool,
    debug: bool = False,
) -> list[dict] | None:
    """Map operations to processes via the dependency graph, or None to fall back.

    Reading every process's XML is the obvious approach and brutally expensive: 1726
    component fetches on a real account to find links to 67 operations. Boomi can
    answer "what references this component" directly, so ask that instead.

    Returns None -- meaning "fall back to scanning" -- not only when the object is
    unqueryable, but also when it returns nothing usable. A dependency lookup that
    yields zero links across every operation is far more likely to mean the response
    was not understood than that the account genuinely has no references, and
    reporting the latter would be a confident wrong answer. Falling back costs time;
    reporting a false empty costs trust.
    """
    operation_ids = list(by_id)

    def references_to(component_id: str) -> list[dict]:
        return client.rest_query_all(
            "ComponentReference",
            {
                "QueryFilter": {
                    "expression": {
                        "operator": "EQUALS",
                        "property": "componentId",
                        "argument": [component_id],
                    }
                }
            },
        )

    try:
        probe = references_to(operation_ids[0])
    except Exception as exc:
        if debug:
            print(f"  [debug] ComponentReference query failed: {exc}", file=sys.stderr)
        return None

    if debug:
        print(
            f"  [debug] ComponentReference returned {len(probe)} row(s) for "
            f"{operation_ids[0]}:\n"
            + json.dumps(probe[:3], indent=2),
            file=sys.stderr,
        )

    if verbose:
        print(
            f"  using the component dependency graph ({len(operation_ids)} lookups "
            "instead of a full process scan)",
            file=sys.stderr,
        )

    names = _process_names(client)
    usages: list[dict] = []
    rows_seen = 0
    keys_seen: set[str] = set()

    def parents_of(component_id: str, rows: list[dict]) -> list[str]:
        nonlocal rows_seen
        found: list[str] = []
        for row in flatten_references(rows):
            rows_seen += 1
            keys_seen.update(row.keys())
            candidates = [str(row.get(k) or "") for k in PARENT_ID_KEYS]
            # Any UUID in the row that is not the component we asked about is a
            # plausible parent. Costs nothing and survives a field rename.
            candidates += [
                str(v) for v in row.values()
                if isinstance(v, str) and UUID_PATTERN.fullmatch(v)
            ]
            found.extend(c for c in candidates if c and c != component_id)
        return list(dict.fromkeys(found))

    for index, component_id in enumerate(operation_ids, start=1):
        if verbose and index % 25 == 0:
            print(f"  resolved {index}/{len(operation_ids)} operations", file=sys.stderr)
        try:
            rows = probe if index == 1 else references_to(component_id)
        except Exception:
            continue

        operation = by_id[component_id]
        # Walk up: direct parents first, then one more level for operations that a
        # process reaches through an intermediate component. The API returns
        # immediate references only, so the recursion has to happen here.
        seen_parents: set[str] = set()
        frontier = parents_of(component_id, rows)
        depth = 1
        while frontier and depth <= MAX_REFERENCE_DEPTH:
            next_frontier: list[str] = []
            for parent in frontier:
                if parent in seen_parents:
                    continue
                seen_parents.add(parent)
                if parent in names:
                    usages.append(
                        {
                            "processName": names[parent],
                            "processId": parent,
                            "operationName": operation["name"],
                            "topic": operation["topic"],
                            "action": operation["action"],
                            "confidence": operation["confidence"],
                        }
                    )
                    continue
                # Not a process -- it may itself be referenced by one.
                if depth < MAX_REFERENCE_DEPTH:
                    try:
                        next_frontier.extend(parents_of(parent, references_to(parent)))
                    except Exception:
                        pass
            frontier = next_frontier
            depth += 1

    if not usages:
        if verbose:
            detail = (
                f"returned {rows_seen} row(s) with keys {sorted(keys_seen)}"
                if rows_seen
                else "returned no rows"
            )
            print(
                f"  the dependency graph {detail} but resolved no processes — "
                "not trusting that as an answer",
                file=sys.stderr,
            )
        return None

    return usages


def map_processes(
    client: BoomiClient,
    operations: list[dict],
    limit: int | None = None,
    verbose: bool = True,
    use_reference_api: bool = True,
    debug: bool = False,
) -> list[dict]:
    by_id = {op["componentId"]: op for op in operations}
    if not by_id:
        return []

    if use_reference_api:
        via_references = _map_via_references(client, by_id, verbose, debug)
        if via_references is not None:
            return via_references
        if verbose:
            print(
                "  falling back to scanning process XML — slower, but it is the "
                "answer rather than the absence of one",
                file=sys.stderr,
            )

    processes = query_components(client, "process", limit)

    usages: list[dict] = []
    for index, meta in enumerate(processes, start=1):
        component_id = meta.get("componentId")
        if not component_id:
            continue
        if verbose and index % 50 == 0:
            print(f"  scanned {index}/{len(processes)} processes", file=sys.stderr)
        try:
            xml = fetch_xml(client, component_id, meta.get("currentVersion"))
        except BoomiAPIError:
            continue
        for referenced in set(UUID_PATTERN.findall(xml)):
            operation = by_id.get(referenced)
            if not operation:
                continue
            usages.append(
                {
                    "processName": meta.get("name"),
                    "processId": component_id,
                    "operationName": operation["name"],
                    "topic": operation["topic"],
                    "action": operation["action"],
                    "confidence": operation["confidence"],
                }
            )
    return usages


def analyse(topics: list[dict], usages: list[dict], tokens: list[dict] | None = None) -> list[dict]:
    """Health findings, ordered by how quietly the problem fails.

    The ordering is deliberate: the conditions at the top produce no error anywhere
    until something downstream breaks, which is exactly why they need surfacing.
    """
    findings: list[dict] = []
    topic_names = {str(t.get("name")) for t in topics}

    producers: dict[str, list[str]] = {}
    consumers: dict[str, list[str]] = {}
    unclassified = 0
    for usage in usages:
        topic = str(usage.get("topic") or "")
        if not topic:
            continue
        action = usage.get("action")
        if action == "produce":
            producers.setdefault(topic, []).append(str(usage["processName"]))
        elif action in ("consume", "listen"):
            consumers.setdefault(topic, []).append(str(usage["processName"]))
        else:
            unclassified += 1

    # Direction matters more than it looks. "No process publishes to this topic" is
    # a strong claim, and asserting it because an operation's direction could not be
    # read -- rather than because no producer exists -- turns a parsing gap into a
    # page of false findings. When most operations are unclassified, the honest move
    # is to say the direction is unknown and skip those checks entirely.
    direction_known = usages and unclassified < len(usages) / 2

    def add(severity: str, subject: str, finding: str, detail: str) -> None:
        findings.append(
            {"severity": severity, "subject": subject, "finding": finding, "detail": detail}
        )

    for topic in topics:
        name = str(topic.get("name"))
        subs = topic.get("subscriptions") or []

        if not subs:
            add("high", name, "No subscriptions",
                "Nothing consumes this topic, so anything published to it is "
                "discarded on arrival. Indistinguishable from working, from the "
                "producer's side.")

        if subs and not consumers.get(name) and direction_known:
            add("medium", name, "Subscriptions exist but no process consumes them",
                "The subscription accumulates a backlog with no reader. Either a "
                "consuming process is missing, or consumption happens outside this "
                "account over REST.")

        if direction_known and not producers.get(name):
            add("low", name, "No process publishes to this topic",
                "May be produced to externally, or left over from work that has "
                "moved on.")

        if topic.get("persistent") is False and subs:
            add("high", name, "Non-persistent topic has subscribers",
                "Messages are not retained across a broker restart. Subscribers "
                "lose whatever was in flight, with no error raised.")

        for sub in subs:
            backlog = sub.get("backlogCount") or 0
            if backlog > 0:
                severity = "high" if not consumers.get(name) else "medium"
                add(severity, f"{name}/{sub.get('name')}",
                    f"Backlog of {backlog}",
                    "Messages have arrived and not been consumed. A small persistent "
                    "backlog usually means a consumer stopped or never attached.")

        # Only interesting when several processes consume through one subscription --
        # a genuine shared bottleneck. One subscription with one consumer is the
        # ordinary case and flagging it is noise.
        if len(subs) == 1 and len(consumers.get(name, [])) > 1:
            add("low", name, "Single subscription shared by several consumers",
                f"{len(consumers[name])} processes consume through one subscription. "
                "Often deliberate, but worth confirming it is not an unnoticed "
                "single point of failure.")

    # Grouped by topic rather than one finding per process. Several processes
    # referencing the same missing topic is a single problem with one fix -- listing
    # it repeatedly buries the other findings and overstates how much is wrong.
    orphans: dict[str, list[str]] = {}
    for usage in usages:
        topic = usage.get("topic")
        if topic and usage.get("confidence") != "dynamic" and topic not in topic_names:
            orphans.setdefault(str(topic), []).append(str(usage["processName"]))

    for topic, processes in orphans.items():
        unique = sorted(set(processes))
        listed = ", ".join(f"'{p}'" for p in unique[:4])
        if len(unique) > 4:
            listed += f", and {len(unique) - 4} more"
        add("high", topic, f"Orphaned operation in {len(unique)} process(es)",
            f"{listed} reference a topic that does not exist in this environment, so "
            "those operations fail at runtime. Usually a process promoted without its "
            "topic — check whether the topic exists in the environment it came from.")

    for token in tokens or []:
        from es_discover import expiry_state

        state, label = expiry_state(token.get("expirationTime"))
        if state == "expired":
            add("high", str(token.get("name")), f"Token expired ({label})",
                "Anything still using this token is failing authentication now. An "
                "expired token surfaces as a connection error, never as a warning.")
        elif state == "expiring":
            add("medium", str(token.get("name")), f"Token expires soon ({label})",
                "Renew before it lapses; there is no grace period.")

    if usages and not direction_known:
        add("low", "(direction)", "Operation direction could not be determined",
            f"{unclassified} of {len(usages)} operations did not declare whether "
            "they produce or consume, so publisher and consumer checks were skipped "
            "rather than guessed. The topic-to-process map above is unaffected.")

    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["subject"]))
    return findings
