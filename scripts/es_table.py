#!/usr/bin/env python3
"""Markdown tables, rendered the same way everywhere.

Every skill in this plugin prints tables through here rather than building rows
by hand, because inconsistent output is a real cost: a reader who has learned
that a dash means "not applicable" in one report should not have to relearn it
in the next.

Three conventions this module enforces:

  - **Absent and false are different.** `None` renders as an em-dash, `False`
    renders as "No". A PRODUCE operation has no dead-letter setting at all,
    while a CONSUME operation may have it switched off. Printing "No" for both
    would claim the producer has a setting it cannot have.

  - **Pipes are escaped.** A topic named `orders|eu` would otherwise split a
    row into the wrong number of cells and corrupt every column after it.

  - **Empty tables say why.** A table with a header and no rows reads as a
    rendering failure. `table()` returns the caller's placeholder instead.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Sequence

DASH = "—"


def cell(value: Any) -> str:
    """One value, rendered for a markdown table cell."""
    if value is None:
        return DASH
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple, set)):
        items = [str(v) for v in value if v not in (None, "")]
        return ", ".join(sorted(items)) if items else DASH
    text = str(value).strip()
    if not text:
        return DASH
    # A literal pipe would end the cell early and shift every column after it.
    return text.replace("|", "\\|")


def table(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    empty: str = "_None found._",
) -> str:
    """A markdown table, or `empty` when there are no rows."""
    body = [[cell(v) for v in row] for row in rows]
    if not body:
        return empty
    lines = [
        "| " + " | ".join(str(h) for h in headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def records(
    headers: Sequence[str],
    items: Iterable[dict],
    keys: Sequence[str] | None = None,
    empty: str = "_None found._",
) -> str:
    """A table built from dicts, taking `keys` (defaulting to the headers)."""
    fields = list(keys or headers)
    return table(headers, ([item.get(k) for k in fields] for item in items), empty)


def numbered(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    empty: str = "_None found._",
) -> str:
    """A table with a leading `#` column, for lists people refer to by position."""
    body = list(rows)
    if not body:
        return empty
    return table(["#", *headers], ([i, *r] for i, r in enumerate(body, start=1)))


def counts(title_column: str, pairs: Iterable[tuple[str, Any]], empty: str = "_None._") -> str:
    """A two-column count table, sorted high to low."""
    items = [(k, v) for k, v in pairs]
    items.sort(key=lambda kv: (-kv[1] if isinstance(kv[1], int) else 0, str(kv[0])))
    return table([title_column, "Count"], items, empty)


def yes_no(value: Any, when_missing: str = DASH) -> str:
    """Tri-state flag: Yes / No / not applicable."""
    if value is None:
        return when_missing
    return "Yes" if value else "No"


def status(deployed: Any, environments: Sequence[str] | None = None) -> tuple[str, Any]:
    """(deployed cell, environment cell) for one operation.

    `None` means the deployment query itself failed. Reporting that as "No"
    would turn a tooling problem into a false finding about the account, which
    is the kind of error that gets acted on.
    """
    if deployed is None:
        return "unknown", None
    if not deployed:
        return "No", None
    return "Yes", list(environments or []) or None


def section(title: str, body: str, level: int = 2) -> str:
    """A titled block, with the blank lines markdown needs around a table."""
    return f"{'#' * level} {title}\n\n{body}\n"


def summarise(rows: Iterable[dict], key: str) -> list[tuple[str, int]]:
    """Count rows by one field, for a quick distribution table."""
    tally: dict[str, int] = {}
    for row in rows:
        value = row.get(key)
        label = DASH if value is None else str(value)
        tally[label] = tally.get(label, 0) + 1
    return sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))


def wrap_findings(findings: Iterable[dict]) -> str:
    """The standard findings table, ordered by severity."""
    # This plugin emits high/medium/low; the words critical/warning/info appear in
    # findings copied from other tools. Rank both rather than silently sorting one
    # of them to the bottom.
    order = {"critical": 0, "high": 0, "warning": 1, "medium": 1, "info": 2, "low": 2}
    items = sorted(
        findings,
        key=lambda f: (order.get(str(f.get("severity", "info")).lower(), 3), str(f.get("subject", ""))),
    )
    return table(
        ["#", "Severity", "Subject", "Finding", "Detail"],
        (
            [
                i,
                str(f.get("severity", "info")).title(),
                f.get("subject"),
                f.get("finding"),
                f.get("detail"),
            ]
            for i, f in enumerate(items, start=1)
        ),
        empty="_No findings._",
    )


def severity_summary(findings: Iterable[dict]) -> str:
    """Counts by severity, so a long findings table has a headline."""
    items = list(findings)
    tally = summarise(items, "severity")
    rows = [(str(k).title(), v) for k, v in tally]
    rows.append(("Total", len(items)))
    return table(["Severity", "Count"], rows, empty="_No findings._")
