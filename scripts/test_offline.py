#!/usr/bin/env python3
"""
Offline tests. No network, no credentials -- run them anywhere.

    python test_offline.py

These cover the parts where a silent mistake would be expensive: the protected-
environment refusal, duplicate detection, partition carry-over, and JWT expiry
parsing. They deliberately do not test the API calls themselves; those need a real
account and are checked by running the CLIs against one.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("BOOMI_ACCOUNT_ID", "test-account")
os.environ.setdefault("BOOMI_USERNAME", "tester@boomi.com")
os.environ.setdefault("BOOMI_API_TOKEN", "not-a-real-token")

from boomi_auth import BoomiAuthError, Config, _jwt_expiry  # noqa: E402
import es_migrate  # noqa: E402
from es_client import EventStreamsClient  # noqa: E402

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


class FakeES(EventStreamsClient):
    """Stands in for the API with fixed data, so plan logic can be tested alone."""

    def __init__(self, config: Config, environments, topics_by_env, tokens_by_env=None):
        class _Client:
            pass

        client = _Client()
        client.config = config
        self.client = client
        self._environments = environments
        self._topics = topics_by_env
        self._tokens = tokens_by_env or {}

    def environments(self):
        return self._environments

    def topics(self, environment_id=None):
        return self._topics.get(environment_id, [])

    def tokens(self, environment_id=None):
        return self._tokens.get(environment_id, [])


def make_config(protected: str = "") -> Config:
    os.environ["BOOMI_PROTECTED_ENVIRONMENTS"] = protected
    return Config()


def run_tests() -> int:
    """All assertions live in here rather than at module level.

    Other code imports FakeES from this file as a fixture. When the assertions ran
    on import, they left BOOMI_PROTECTED_ENVIRONMENTS reset to empty as a side
    effect -- which silently disarmed the denylist in the importing process and made
    a guardrail check appear to fail. A test file that changes global state just by
    being imported is a trap, so the body is guarded.
    """
    global PASSED

    print("Protected environment denylist")
    config = make_config("Production, 70f67aca-ae7e-42b0-adf8-9bcee92824db")
    check("matches by exact name", config.is_protected({"name": "Production", "id": "x"}))
    check("matches by name, different case", config.is_protected({"name": "PRODUCTION", "id": "x"}))
    check("matches by id", config.is_protected({"name": "Anything", "id": "70f67aca-ae7e-42b0-adf8-9bcee92824db"}))
    check("matches a bare string", config.is_protected("production"))
    check("does not match an unlisted env", not config.is_protected({"name": "Test", "id": "y"}))
    check("empty denylist protects nothing", not make_config("").is_protected({"name": "Production", "id": "z"}))

    print("\nguard_target refuses protected targets")
    config = make_config("Production")
    es = FakeES(config, [], {})
    try:
        es_migrate.guard_target(es, {"name": "Production", "id": "p1"})
        check("raises on protected target", False, "no exception raised")
    except es_migrate.ProtectedEnvironmentError:
        check("raises on protected target", True)
    try:
        es_migrate.guard_target(es, {"name": "Test", "id": "t1"})
        check("allows unprotected target", True)
    except es_migrate.ProtectedEnvironmentError:
        check("allows unprotected target", False, "raised unexpectedly")

    print("\nPlan building")
    source_env = {"id": "src", "name": "Dev"}
    target_env = {"id": "tgt", "name": "Test"}
    es = FakeES(
        make_config(""),
        [source_env, target_env],
        {
            "src": [
                {
                    "name": "orders",
                    "persistent": True,
                    "partitions": 4,
                    "description": "order events",
                    "subscriptions": [
                        {"name": "orders-sub", "type": "SHARED", "description": None},
                        {"name": "orders-audit", "type": "EXCLUSIVE", "description": None},
                    ],
                },
                {"name": "invoices", "persistent": False, "partitions": 1, "subscriptions": []},
            ],
            "tgt": [
                {
                    "name": "orders",
                    "persistent": True,
                    "partitions": 4,
                    "subscriptions": [{"name": "orders-sub", "type": "NONE"}],
                }
            ],
        },
        {
            "src": [{"name": "producer-token", "allowProduce": True, "allowConsume": False}],
            "tgt": [],
        },
    )

    plan = es_migrate.build_plan(es, source_env, target_env, None, include_tokens=True)

    topic_names = [t["name"] for t in plan["topics"]]
    check("creates only the missing topic", topic_names == ["invoices"], f"got {topic_names}")
    check(
        "skips the topic that already exists",
        any(s["kind"] == "topic" and s["name"] == "orders" for s in plan["skipped"]),
    )

    sub_names = sorted(f"{s['topicName']}/{s['name']}" for s in plan["subscriptions"])
    check(
        "creates only the missing subscription",
        sub_names == ["orders/orders-audit"],
        f"got {sub_names}",
    )
    check(
        "skips the subscription that already exists",
        any(s["kind"] == "subscription" and "orders-sub" in s["name"] for s in plan["skipped"]),
    )

    invoices = next(t for t in plan["topics"] if t["name"] == "invoices")
    check("carries partition count from source", invoices["partitions"] == 1)
    check("carries persistence flag from source", invoices["persistent"] is False)
    check("plans the missing token", [t["name"] for t in plan["tokens"]] == ["producer-token"])

    plan_no_tokens = es_migrate.build_plan(es, source_env, target_env, None, include_tokens=False)
    check("--no-tokens excludes tokens", plan_no_tokens["tokens"] == [])

    selective = es_migrate.build_plan(es, source_env, target_env, ["invoices"], include_tokens=False)
    check(
        "selective plan includes only the named topic",
        [t["name"] for t in selective["topics"]] == ["invoices"],
    )

    print("\nSubscription create takes no type argument")
    import inspect  # noqa: E402

    signature = inspect.signature(EventStreamsClient.create_subscription)
    check(
        "create_subscription has no 'type' parameter",
        "type" not in signature.parameters,
        f"params: {list(signature.parameters)}",
    )

    print("\nNo delete capability exists on the client")
    delete_like = [
        name
        for name in dir(EventStreamsClient)
        if any(word in name.lower() for word in ("delete", "remove", "destroy", "drop", "purge"))
    ]
    check("client exposes no delete-like method", delete_like == [], f"found {delete_like}")

    print("\nCredential resolution order")
    # A conversation started in any directory must find credentials, and a project
    # .env must still win so an engagement can override the default account.
    import tempfile, boomi_auth as _auth

    original = _auth.credential_sources
    saved_env = {k: os.environ.get(k) for k in
                 ("BOOMI_ACCOUNT_ID", "BOOMI_USERNAME", "BOOMI_API_TOKEN")}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            project = os.path.join(tmp, "project.env")
            store = os.path.join(tmp, "store.env")
            open(store, "w").write(
                "BOOMI_ACCOUNT_ID=store-A1B2C3\nBOOMI_USERNAME=store@boomi.com\n"
                "BOOMI_API_TOKEN=store-token\nBOOMI_PROTECTED_ENVIRONMENTS=Production\n")

            _auth.credential_sources = lambda: [(project, "project"), (store, "store")]

            for k in saved_env:
                os.environ.pop(k, None)
            _auth.load_credentials()
            check("the store is found when no project .env exists",
                  Config().account_id == "store-A1B2C3", f"got {Config().account_id}")

            open(project, "w").write(
                "BOOMI_ACCOUNT_ID=project-Z9Y8X7\nBOOMI_USERNAME=project@boomi.com\n"
                "BOOMI_API_TOKEN=project-token\n")
            for k in saved_env:
                os.environ.pop(k, None)
            _auth.load_credentials()
            check("a project .env overrides the store",
                  Config().account_id == "project-Z9Y8X7", f"got {Config().account_id}")
            check("keys the project file omits fall through to the store",
                  Config().protected == ["Production"])

            os.environ["BOOMI_ACCOUNT_ID"] = "stale-shell-value"
            _auth.load_credentials()
            check("a stale shell value does not override a file",
                  Config().account_id == "project-Z9Y8X7", f"got {Config().account_id}")
    finally:
        _auth.credential_sources = original
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print("\nCredential store must be persistent")
    # The bug this pins: the store was anchored to ~, and in some hosts HOME sits
    # inside a session sandbox that is wiped between conversations. Credentials
    # vanished and the user was asked to set up again every single time.
    check("a sandbox HOME is recognised as ephemeral",
          _auth._looks_ephemeral("/sessions/abc/.boomi-event-streams/env"))
    check("a mounted folder inside a sandbox is not ephemeral",
          not _auth._looks_ephemeral("/sessions/abc/mnt/myproject/.boomi-event-streams/env"))
    check("an ordinary home directory is not ephemeral",
          not _auth._looks_ephemeral("/home/someone/.boomi-event-streams/env"))
    check("a mounted output directory is excluded as a store",
          "outputs" in _auth._NON_PERSISTENT_MOUNTS
          and "uploads" in _auth._NON_PERSISTENT_MOUNTS)

    os.environ["BOOMI_ES_CONFIG"] = "/tmp/es-store-test/env"
    try:
        path, persistent = _auth.preferred_store()
        check("BOOMI_ES_CONFIG overrides the chosen store",
              path == "/tmp/es-store-test/env" and persistent, f"got {path}")
        check("the override appears in the read order",
              any(c == "/tmp/es-store-test/env" for c, _ in _auth.credential_sources()))
    finally:
        os.environ.pop("BOOMI_ES_CONFIG", None)

    print("\nSetup validation")
    import es_setup
    swapped = es_setup.validate({"BOOMI_ACCOUNT_ID": "me@boomi.com",
                                 "BOOMI_USERNAME": "acct-A1B2C3",
                                 "BOOMI_API_TOKEN": "t"})
    check("setup refuses a swapped account id and username",
          any("swapped" in p for p in swapped), f"got {swapped}")
    prefixed = es_setup.validate({"BOOMI_ACCOUNT_ID": "acct-A1B2C3",
                                  "BOOMI_USERNAME": "BOOMI_TOKEN.me@boomi.com",
                                  "BOOMI_API_TOKEN": "t"})
    check("setup refuses a double-prefixed username",
          any("prefix" in p for p in prefixed), f"got {prefixed}")
    good = es_setup.validate({"BOOMI_ACCOUNT_ID": "acct-A1B2C3",
                              "BOOMI_USERNAME": "me@boomi.com",
                              "BOOMI_API_TOKEN": "t"})
    check("setup accepts a valid trio", good == [], f"got {good}")
    check("the API token is masked when shown",
          "secret" not in es_setup.mask("supersecrettoken12345"))

    print("\nAdmin guard and deletion description")
    # es_admin.py was the only module with no unit coverage, and it is the only one
    # that destroys data. That combination is exactly backwards.
    import es_admin
    import es_admin_ops

    class _Client:
        pass

    guard_client = _Client()
    guard_client.config = make_config("Production, Prod-EU")
    admin = es_admin_ops.EventStreamsAdmin.__new__(es_admin_ops.EventStreamsAdmin)
    admin.client = guard_client

    for op in ("delete-topic", "clear-backlog", "update-topic", "create-topic"):
        try:
            admin.guard({"name": "Production", "id": "p"}, op)
            check(f"guard refuses {op} on a protected environment", False, "no exception")
        except es_admin_ops.ProtectedEnvironmentError:
            check(f"guard refuses {op} on a protected environment", True)
    try:
        admin.guard({"name": "Test", "id": "t"}, "delete-topic")
        check("guard allows an unprotected environment", True)
    except es_admin_ops.ProtectedEnvironmentError:
        check("guard allows an unprotected environment", False, "raised")

    # Matching is exact, not prefix. Documented now; asserted here so it stays true.
    try:
        admin.guard({"name": "Production US", "id": "p2"}, "delete-topic")
        check("matching is exact, not prefix", True,
              "'Production' must not silently cover 'Production US'")
    except es_admin_ops.ProtectedEnvironmentError:
        check("matching is exact, not prefix", False, "unexpectedly refused")

    class FakeAdmin:
        def __init__(self):
            self.es = self
        def topics(self, env_id):
            return [{"name": "orders", "subscriptions": [
                {"name": "s1", "backlogCount": 40}, {"name": "s2", "backlogCount": 2}]}]
        def tokens(self, env_id):
            return []

    class A:
        command = "delete-topic"; name = "orders"; environment = "Test"
    described = es_admin.describe_deletion(FakeAdmin(), A, "env")
    joined = " ".join(described)
    check("deletion description counts the subscriptions", "2 subscription(s)" in joined,
          f"got {joined}")
    check("deletion description counts the messages at risk", "42 queued" in joined,
          f"got {joined}")

    class B:
        command = "clear-backlog"; topic = "orders"; name = "s1"; environment = "Test"
    cleared = " ".join(es_admin.describe_deletion(FakeAdmin(), B, "env"))
    check("clear-backlog says the subscription survives", "survives" in cleared)
    check("clear-backlog counts what is discarded", "40 queued" in cleared, f"got {cleared}")

    print("\nJWT expiry parsing")
    future = int(time.time()) + 300
    payload = base64.urlsafe_b64encode(json.dumps({"exp": future}).encode()).decode().rstrip("=")
    check("reads the exp claim", _jwt_expiry(f"header.{payload}.signature") == float(future))
    check("returns None for a malformed token", _jwt_expiry("not-a-jwt") is None)
    check("returns None when exp is absent", _jwt_expiry(
        "h." + base64.urlsafe_b64encode(b'{"sub":"x"}').decode().rstrip("=") + ".s"
    ) is None)

    print("\nCredential validation")
    os.environ["BOOMI_USERNAME"] = "BOOMI_TOKEN.tester@boomi.com"
    try:
        Config().validate()
        check("rejects a double-prefixed username", False, "no exception raised")
    except BoomiAuthError:
        check("rejects a double-prefixed username", True)
    os.environ["BOOMI_USERNAME"] = "tester@boomi.com"

    os.environ["BOOMI_ACCOUNT_ID"] = ""
    try:
        Config().validate()
        check("rejects a missing account id", False, "no exception raised")
    except BoomiAuthError as exc:
        check("rejects a missing account id", "BOOMI_ACCOUNT_ID" in str(exc))
    os.environ["BOOMI_ACCOUNT_ID"] = "test-account"

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_tests())
