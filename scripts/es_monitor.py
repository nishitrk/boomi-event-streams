#!/usr/bin/env python3
"""
Live Event Streams health: dead letter queues, consumers, throughput, messages.

    python es_monitor.py --environment Test                    # health summary
    python es_monitor.py --environment Test --dlq              # dead letter detail
    python es_monitor.py --environment Test --peek POC_Topic   # what is on a topic
    python es_monitor.py --environment Test --json

Discovery answers "what exists". This answers "is it working", which is usually the
question behind the question. The difference matters most for two things the static
picture cannot show:

  * **Dead letter queues.** Where messages go when delivery keeps failing. Boomi
    surfaces this nowhere, so it accumulates unseen until someone thinks to look.
  * **Live consumers.** `activeConsumerCount` says whether anything is attached to a
    subscription right now. Previously that had to be inferred from an expensive
    component scan, and an inference is not the same as a fact.

Together they turn "backlog of 1" from a curiosity into a diagnosis: a backlog with
no active consumer is a stalled integration; a backlog with one attached is merely a
slow one.
"""

from __future__ import annotations

import argparse
import json
import sys

from boomi_auth import BoomiAPIError, BoomiAuthError, build_client
from es_client import EventStreamsClient


def rate(value) -> str:
    if value in (None, ""):
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return "0" if number == 0 else (f"{number:.2f}".rstrip("0").rstrip("."))


def size(value) -> str:
    if value in (None, ""):
        return "—"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def analyse(topics: list[dict]) -> list[dict]:
    """Findings that only live data can produce.

    Ordered by how silently the condition fails. A dead letter backlog and a stalled
    subscription both look completely healthy from the producer's side, which is why
    they rank above a topic that is merely idle.
    """
    findings: list[dict] = []
    idle: list[str] = []

    def add(sev, subject, finding, detail):
        findings.append({"severity": sev, "subject": subject,
                         "finding": finding, "detail": detail})

    for topic in topics:
        name = str(topic.get("name"))
        subs = topic.get("subscriptions") or []

        for sub in subs:
            sub_name = f"{name}/{sub.get('name')}"
            dlq = sub.get("deadLetterBacklogCount") or 0
            retry = sub.get("retryBacklogCount") or 0
            backlog = sub.get("backlogCount") or 0
            consumers = sub.get("activeConsumerCount")

            if dlq:
                add("high", sub_name, f"{dlq} message(s) in the dead letter queue",
                    "Delivery failed repeatedly and these were set aside. Nothing "
                    "retries them and no error is raised anywhere — inspect with "
                    "`--dlq` to see what they are before deciding.")
            if retry:
                add("medium", sub_name, f"{retry} message(s) awaiting retry",
                    "Delivery has failed at least once. If this number is not "
                    "falling, they are on their way to the dead letter queue.")
            if backlog and consumers == 0:
                add("high", sub_name, f"Backlog of {backlog} with no active consumer",
                    "Messages are queuing and nothing is attached to read them. "
                    "This is what a stopped integration looks like from the inside.")
            elif backlog and consumers:
                add("medium", sub_name, f"Backlog of {backlog}, {consumers} consumer(s) attached",
                    "Something is reading, so this is a throughput question rather "
                    "than a stoppage. Worth watching whether the number is falling.")
            if consumers == 0 and not backlog:
                idle.append(sub_name)

    # An idle subscription with no backlog is not a problem, it is an idle
    # environment. Listing each one separately produced a wall of low-severity
    # entries that buried the two findings that mattered — so they collapse into a
    # single line that says how many, without pretending each is a defect.
    if idle:
        add("low", f"{len(idle)} idle subscription(s)", "No active consumer, no backlog",
            "Nothing attached and nothing waiting, which is normal for an idle "
            "environment. It is also why these report subscription type NONE — the "
            "broker assigns type when a consumer connects. Listed: "
            + ", ".join(f"`{s}`" for s in idle[:6])
            + (f", and {len(idle) - 6} more" if len(idle) > 6 else "") + ".")

    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["subject"]))
    return findings


def render(env_name: str, topics: list[dict], findings: list[dict]) -> str:
    lines = [f"# Event Streams live health — {env_name}", ""]

    lines += ["## Topics", "",
              "| Topic | In/s | Out/s | Backlog | Size | Producers | Subs |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for topic in sorted(topics, key=lambda t: str(t.get("name"))):
        lines.append(
            f"| `{topic.get('name')}` | {rate(topic.get('messageRateIn'))} "
            f"| {rate(topic.get('messageRateOut'))} "
            f"| {topic.get('backlogCount', '—')} | {size(topic.get('backlogSize'))} "
            f"| {topic.get('producerCount', '—')} "
            f"| {topic.get('subscriptionCount', len(topic.get('subscriptions') or []))} |"
        )
    lines.append("")

    rows = [(str(t.get("name")), s) for t in topics for s in (t.get("subscriptions") or [])]
    if rows:
        lines += ["## Subscriptions", "",
                  "| Topic | Subscription | Consumers | Backlog | Retry | Dead letter |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for topic_name, sub in sorted(rows, key=lambda r: (r[0], str(r[1].get("name")))):
            dlq = sub.get("deadLetterBacklogCount") or 0
            lines.append(
                f"| `{topic_name}` | `{sub.get('name')}` "
                f"| {sub.get('activeConsumerCount', '—')} "
                f"| {sub.get('backlogCount', 0)} "
                f"| {sub.get('retryBacklogCount', 0) or '—'} "
                f"| {'**' + str(dlq) + '**' if dlq else '—'} |"
            )
        lines.append("")

    lines += [f"## Findings ({len(findings)})", ""]
    if not findings:
        lines.append("Nothing flowing badly. No dead letters, no stalled subscriptions.")
        return "\n".join(lines)

    for severity in ("high", "medium", "low"):
        group = [f for f in findings if f["severity"] == severity]
        if not group:
            continue
        lines += [f"### {severity.title()} ({len(group)})", ""]
        for f in group:
            lines.append(f"- **`{f['subject']}` — {f['finding']}.** {f['detail']}")
        lines.append("")
    return "\n".join(lines)


def render_messages(label: str, messages: list[dict], show_payload: bool) -> str:
    if not messages:
        return f"\n_{label}: empty._\n"
    lines = [f"\n### {label} ({len(messages)})", "",
             "| Published | Producer | Redeliveries | Size | Message ID |",
             "| --- | --- | --- | --- | --- |"]
    for m in messages:
        lines.append(
            f"| {m.get('publishTime', '—')} | {m.get('producer') or '—'} "
            f"| {m.get('redeliveryCount', 0)} | {size(m.get('size'))} "
            f"| `{str(m.get('messageId'))[:24]}` |"
        )
    lines.append("")
    if show_payload:
        for m in messages:
            body = str(m.get("payload") or "")
            lines += [f"**`{str(m.get('messageId'))[:24]}`**", "```",
                      body[:1500] + ("\n… truncated" if len(body) > 1500 else ""), "```", ""]
    else:
        lines.append("_Message bodies not shown. Add `--payload` to include them — "
                     "they are customer data, so they are opt-in rather than default._")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Live Event Streams health and message inspection.")
    p.add_argument("--environment", required=True, help="Environment name or ID.")
    p.add_argument("--dlq", action="store_true",
                   help="Show dead letter queue contents for every subscription that has any.")
    p.add_argument("--peek", metavar="TOPIC", help="Show messages currently on a topic.")
    p.add_argument("--subscription", help="With --peek or --dlq, narrow to one subscription.")
    p.add_argument("--limit", type=int, default=10, help="Messages to fetch. Default 10.")
    p.add_argument("--payload", action="store_true",
                   help="Include message bodies. These are customer data.")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    try:
        es = EventStreamsClient(build_client())
        from es_discover import resolve_environment

        env_id = resolve_environment(es, args.environment)
        env_name = next((e["name"] for e in es.environments() if e["id"] == env_id),
                        args.environment)
        topics = es.live_topics(env_id)
        findings = analyse(topics)

        extra = ""
        if args.peek:
            matches = [t for t in topics if args.peek.lower() in str(t.get("name", "")).lower()]
            if not matches:
                available = ", ".join(sorted(str(t.get("name")) for t in topics))
                print(f"No topic matching '{args.peek}'. Topics here: {available}",
                      file=sys.stderr)
                return 1
            # Messages belong to a subscription, not a topic, so peeking at "a topic"
            # means peeking at each of its subscriptions. Asking the user to name one
            # would be a worse experience than just showing all of them.
            for topic in matches:
                subs = [
                    s for s in topic.get("subscriptions") or []
                    if not args.subscription or args.subscription == s.get("name")
                ]
                if not subs:
                    extra += (f"\n_`{topic.get('name')}` has no subscription"
                              + (f" named '{args.subscription}'" if args.subscription else "")
                              + ", and messages are only readable through one._\n")
                    continue
                for s in subs:
                    msgs = es.messages(env_id, str(topic.get("name")), str(s.get("name")),
                                       1, args.limit, args.payload)
                    extra += render_messages(
                        f"Messages on `{topic.get('name')}/{s.get('name')}`",
                        msgs, args.payload,
                    )

        if args.dlq:
            found_any = False
            for topic in topics:
                for sub in topic.get("subscriptions") or []:
                    if not (sub.get("deadLetterBacklogCount") or 0):
                        continue
                    if args.subscription and args.subscription != sub.get("name"):
                        continue
                    found_any = True
                    msgs = es.dead_letter_messages(
                        env_id, str(topic.get("name")), str(sub.get("name")),
                        1, args.limit, args.payload,
                    )
                    extra += render_messages(
                        f"Dead letters on `{topic.get('name')}/{sub.get('name')}`",
                        msgs, args.payload,
                    )
            if not found_any:
                extra += "\n_No subscription in this environment has a dead letter backlog._\n"
    except (BoomiAuthError, BoomiAPIError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"environment": env_name, "topics": topics,
                          "findings": findings}, indent=2))
    else:
        print(render(env_name, topics, findings) + extra)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
