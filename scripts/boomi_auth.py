"""
Authentication and HTTP transport for the Boomi Platform APIs.

Two APIs are involved and they authenticate differently:

  Platform REST API   Basic auth, username "BOOMI_TOKEN.{email}", password = API token.
  GraphQL API         Bearer auth with a JWT minted from those same Basic credentials.

The JWT is minted at GET /auth/jwt/generate/{accountId} and the response body is the
raw token as plain text -- not JSON. It lives for about five minutes, so we cache it
and decode the `exp` claim to know when to re-mint rather than minting per call.

One quirk worth knowing: the GraphQL endpoint returns HTTP 200 even when auth fails.
The failure arrives as {"errors": [...]} in the body. Any code that only checks the
status code will happily treat "Unauthorized" as success, so `graphql()` below always
inspects the errors array.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_API_URL = "https://api.boomi.com"

STORE_DIRNAME = ".boomi-event-streams"
STORE_FILENAME = "env"

# Where credentials can live, and — critically — whether that survives.
#
# `~` is the obvious home for a credential store and it is wrong in some hosts.
# Under Claude Cowork the sandbox HOME sits inside an ephemeral session directory
# that is wiped between conversations, so credentials saved there vanish and the user
# is asked to set them up again every single time. Only the folders mounted from the
# real machine persist.
#
# So the store is not one fixed path. It is the first of several candidates that
# exists, and when writing, the best candidate that will actually still be there
# tomorrow.
GLOBAL_ENV_PATH = os.path.join(os.path.expanduser("~"), STORE_DIRNAME, STORE_FILENAME)

# Session sandboxes mount the user's real folders under a path like
# /sessions/<name>/mnt/<folder>. Anything outside those mounts is scratch space.
_SANDBOX_MARKERS = ("/sessions/",)
_MOUNT_GLOBS = ("/sessions/*/mnt/*",)
# Mounts that are managed by the host rather than owned by the user: transient,
# read-only, or replaced on every session. Not somewhere to keep a credential.
_NON_PERSISTENT_MOUNTS = ("outputs", "uploads", ".remote-plugins", ".claude")

_SOURCE_LOG: list[str] = []


def _looks_ephemeral(path: str) -> bool:
    """True if this path is scratch space that gets wiped between sessions.

    Inside a session sandbox, only the /mnt/ subtree is mapped to the user's real
    machine. Everything else -- including the sandbox's own HOME -- is scratch.
    Outside a sandbox, an ordinary filesystem path is assumed to persist.
    """
    resolved = os.path.abspath(path)
    if not any(marker in resolved for marker in _SANDBOX_MARKERS):
        return False
    return "/mnt/" not in resolved


def _mounted_folders() -> list[str]:
    """Writable folders mounted from the user's real machine, best first."""
    import glob

    found = []
    for pattern in _MOUNT_GLOBS:
        for path in sorted(glob.glob(pattern)):
            name = os.path.basename(path)
            if name in _NON_PERSISTENT_MOUNTS:
                continue
            if os.path.isdir(path) and os.access(path, os.W_OK):
                found.append(path)
    return found


def _ancestors_with_store(start: str = ".") -> list[str]:
    """Walk up from `start` looking for an existing credential store.

    Handles the ordinary case where someone is working inside a project or a
    connected folder that already holds one, without needing to know anything about
    how the host lays out its filesystem.
    """
    found = []
    current = os.path.abspath(start)
    while True:
        candidate = os.path.join(current, STORE_DIRNAME, STORE_FILENAME)
        if os.path.exists(candidate):
            found.append(candidate)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return found


def credential_sources() -> list[tuple[str, str]]:
    """(path, label) pairs to read credentials from, most specific first."""
    sources: list[tuple[str, str]] = [("./.env", ".env in the current directory")]

    override = os.environ.get("BOOMI_ES_CONFIG", "").strip()
    if override:
        sources.append((override, "BOOMI_ES_CONFIG"))

    for path in _ancestors_with_store():
        sources.append((path, "a store in this directory or one above it"))

    for folder in _mounted_folders():
        sources.append(
            (os.path.join(folder, STORE_DIRNAME, STORE_FILENAME),
             f"your connected folder ({os.path.basename(folder)})")
        )

    sources.append((GLOBAL_ENV_PATH, "your home directory"))

    seen, unique = set(), []
    for path, label in sources:
        key = os.path.abspath(path)
        if key not in seen:
            seen.add(key)
            unique.append((path, label))
    return unique


def preferred_store() -> tuple[str, bool]:
    """Where to SAVE credentials, and whether that location survives a session.

    Returns (path, persistent). A caller that gets persistent=False should say so
    rather than let the user discover it by being asked to set up again tomorrow.
    """
    override = os.environ.get("BOOMI_ES_CONFIG", "").strip()
    if override:
        return (override, True)

    existing = _ancestors_with_store()
    if existing:
        return (existing[0], not _looks_ephemeral(existing[0]))

    folders = _mounted_folders()
    if folders:
        return (os.path.join(folders[0], STORE_DIRNAME, STORE_FILENAME), True)

    return (GLOBAL_ENV_PATH, not _looks_ephemeral(GLOBAL_ENV_PATH))

# Re-mint this many seconds before the token actually expires. Covers clock skew
# between us and Boomi plus the round trip of whatever call we're about to make.
_EXPIRY_MARGIN_SECONDS = 30

_TIMEOUT_SECONDS = 60


class BoomiAuthError(RuntimeError):
    """Credentials are missing, malformed, or rejected."""


class BoomiAPIError(RuntimeError):
    """The API was reached but returned an error."""


class BoomiFieldError(BoomiAPIError):
    """The query asked for fields the executor does not recognise.

    Boomi's GraphQL appears to be a federated gateway: introspection returns the
    stitched schema, but the downstream service actually serving a given account can
    be an older build that lacks some of those fields. The result is that
    introspection reports a field as present and the validator then rejects it.

    Since the schema cannot be trusted as a description of what will execute, the
    executor's own rejection is the reliable signal. This error carries the rejected
    field paths so a caller can drop them and retry.
    """

    def __init__(self, message: str, field_paths: set[str]) -> None:
        super().__init__(message)
        self.field_paths = field_paths


class Config:
    """Resolved credentials and endpoints, read from the environment.

    Environment variables match the conventions used by the other Boomi skills so
    a single .env works across all of them:

        BOOMI_ACCOUNT_ID    required
        BOOMI_USERNAME      required, the bare email -- we add the BOOMI_TOKEN. prefix
        BOOMI_API_TOKEN     required
        BOOMI_API_URL       optional, defaults to https://api.boomi.com
                            UK/GB accounts use https://api.platform.gb.boomi.com

        BOOMI_PROTECTED_ENVIRONMENTS
                            optional, comma-separated environment names or IDs that
                            migration refuses to write into. See is_protected().
    """

    def __init__(self) -> None:
        self.account_id = os.environ.get("BOOMI_ACCOUNT_ID", "").strip()
        self.username = os.environ.get("BOOMI_USERNAME", "").strip()
        self.api_token = os.environ.get("BOOMI_API_TOKEN", "").strip()
        self.api_url = os.environ.get("BOOMI_API_URL", DEFAULT_API_URL).strip().rstrip("/")
        raw_protected = os.environ.get("BOOMI_PROTECTED_ENVIRONMENTS", "")
        self.protected = [p.strip() for p in raw_protected.split(",") if p.strip()]

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("BOOMI_ACCOUNT_ID", self.account_id),
                ("BOOMI_USERNAME", self.username),
                ("BOOMI_API_TOKEN", self.api_token),
            )
            if not value
        ]
        if missing:
            raise BoomiAuthError(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + ".\nCopy .env.example to .env and fill it in, or export them in your shell."
            )
        # Everything below catches a credential mistake that would otherwise surface
        # as a bare 401 from Boomi several calls later. A 401 tells you nothing about
        # which of three values was wrong, so it is worth being specific here.

        # The two most easily confused values sit next to each other in .env and look
        # nothing alike, which is exactly why they get swapped: an account ID is
        # "something-A1B2C3" and a username is an email address. If both look like
        # the other's format, say so plainly rather than letting Boomi reject an
        # email address in the account-ID slot.
        account_looks_like_email = "@" in self.account_id
        username_looks_like_account = "@" not in self.username

        if account_looks_like_email and username_looks_like_account:
            raise BoomiAuthError(
                "BOOMI_ACCOUNT_ID and BOOMI_USERNAME look swapped.\n"
                f"  BOOMI_ACCOUNT_ID = {self.account_id}   <- this is an email address\n"
                f"  BOOMI_USERNAME   = {self.username}   <- this is an account ID\n"
                "Swap them. The account ID looks like 'company-A1B2C3' and is in "
                "Settings -> Account Information; the username is your email."
            )
        if account_looks_like_email:
            raise BoomiAuthError(
                f"BOOMI_ACCOUNT_ID is set to '{self.account_id}', which is an email "
                "address. It should be your Boomi account ID -- the 'company-A1B2C3' "
                "string from Settings -> Account Information."
            )

        # A frequent mistake is pasting "BOOMI_TOKEN.someone@boomi.com" into
        # BOOMI_USERNAME. We add that prefix ourselves, so doubling it produces a
        # confusing 401 much later. Catch it here instead.
        if self.username.startswith("BOOMI_TOKEN."):
            raise BoomiAuthError(
                "BOOMI_USERNAME should be your bare email address, not the "
                "BOOMI_TOKEN.-prefixed form. The prefix is added automatically."
            )
        if username_looks_like_account:
            raise BoomiAuthError(
                f"BOOMI_USERNAME is set to '{self.username}', which does not look "
                "like an email address. It should be the email you sign in to Boomi "
                "with -- the account ID goes in BOOMI_ACCOUNT_ID."
            )

    @property
    def rest_base(self) -> str:
        return f"{self.api_url}/api/rest/v1/{self.account_id}"

    @property
    def graphql_url(self) -> str:
        return f"{self.api_url}/graphql"

    @property
    def basic_header(self) -> str:
        raw = f"BOOMI_TOKEN.{self.username}:{self.api_token}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def is_protected(self, environment: dict[str, Any] | str) -> bool:
        """True if this environment is on the migration denylist.

        Matches on either name or ID, case-insensitively, so an operator can write
        either form in BOOMI_PROTECTED_ENVIRONMENTS without having to look up IDs.
        """
        if isinstance(environment, str):
            candidates = [environment]
        else:
            candidates = [
                str(environment.get("name", "")),
                str(environment.get("id", "")),
            ]
        lowered = {c.strip().lower() for c in candidates if c and c.strip()}
        return any(p.lower() in lowered for p in self.protected)


def _request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, str]:
    req = urllib.request.Request(url, method=method, data=body)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code in (401, 403):
            raise BoomiAuthError(
                f"Boomi rejected the credentials ({exc.code}) for {method} {url}.\n"
                "Check BOOMI_USERNAME (bare email), BOOMI_API_TOKEN, and that the "
                "token has not expired or been revoked.\n"
                f"Response: {detail[:400]}"
            ) from exc
        raise BoomiAPIError(
            f"{method} {url} failed with HTTP {exc.code}.\nResponse: {detail[:800]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise BoomiAPIError(
            f"Could not reach {url}: {exc.reason}.\n"
            "If you are on a corporate network, check that the VPN or proxy allows "
            "api.boomi.com. TLS interception (Zscaler, Netskope, Cisco Umbrella) is a "
            "common cause and usually shows up as a certificate error."
        ) from exc


def _jwt_expiry(token: str) -> float | None:
    """Read the `exp` claim without verifying the signature.

    We are not authenticating the token, only asking when it stops being useful, so
    decoding the payload is enough. `exp` is in seconds since the epoch. Boomi's own
    published sample script compares it against Date.now() in milliseconds, which
    makes the cache never hit -- do not copy that.
    """
    try:
        payload_segment = token.split(".")[1]
        padding = "=" * (-len(payload_segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_segment + padding))
        exp = payload.get("exp")
        return float(exp) if exp is not None else None
    except Exception:
        # An opaque or malformed token is not fatal -- we just cannot cache it.
        return None


class BoomiClient:
    """Talks to both Boomi APIs, holding one cached JWT for the GraphQL side.

    Deliberately has no delete or teardown methods. Discovery, topology, and
    migration are all reads plus additive writes. Anything destructive has to be
    done by a human in the platform UI, which is the intended safety property:
    a capability that does not exist cannot be talked into existing.
    """

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self.config.validate()
        self._jwt: str | None = None
        self._jwt_expires_at: float = 0.0

    # -- GraphQL ----------------------------------------------------------------

    def _jwt_token(self) -> str:
        now = time.time()
        if self._jwt and now < self._jwt_expires_at - _EXPIRY_MARGIN_SECONDS:
            return self._jwt

        url = f"{self.config.api_url}/auth/jwt/generate/{self.config.account_id}"
        headers = {"Authorization": self.config.basic_header, "Accept": "text/plain"}
        otp = os.environ.get("BOOMI_OTP", "").strip()
        if otp:
            headers["X-Boomi-OTP"] = otp

        _, text = _request(url, headers=headers)
        token = text.strip()
        if not token:
            raise BoomiAuthError(f"{url} returned an empty token.")

        self._jwt = token
        exp = _jwt_expiry(token)
        # Without a readable exp, fall back to a conservative window. The documented
        # lifetime is five minutes; four keeps us clear of the edge.
        self._jwt_expires_at = exp if exp is not None else now + 240
        return token

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._jwt_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        _, text = _request(self.config.graphql_url, method="POST", headers=headers, body=payload)

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BoomiAPIError(f"GraphQL returned non-JSON: {text[:400]}") from exc

        # The endpoint answers 200 even for auth failures, so the errors array is the
        # real status. Checking only the HTTP code silently turns Unauthorized into
        # an empty result set, which looks like "this account has no topics".
        errors = parsed.get("errors")
        if errors:
            messages = "; ".join(e.get("message", str(e)) for e in errors)
            if "unauthor" in messages.lower() or "forbidden" in messages.lower():
                raise BoomiAuthError(
                    f"GraphQL rejected the token: {messages}\n"
                    "The JWT is minted per account -- confirm BOOMI_ACCOUNT_ID matches "
                    "the account whose data you are asking for."
                )
            # Boomi reports unknown fields as
            #   Validation error (FieldUndefined@[eventStreamsTopics/persistent]) : ...
            # The bracketed path identifies the exact selection, which lets a caller
            # prune precisely rather than dropping a field name that may be valid
            # elsewhere in the same query.
            field_paths = set(re.findall(r"FieldUndefined@\[([^\]]+)\]", messages))
            if field_paths:
                raise BoomiFieldError(f"GraphQL error: {messages}", field_paths)
            raise BoomiAPIError(f"GraphQL error: {messages}")

        data = parsed.get("data")
        if data is None:
            raise BoomiAPIError(f"GraphQL returned no data: {text[:400]}")
        return data

    # -- Platform REST ----------------------------------------------------------

    def rest(
        self,
        path: str,
        *,
        method: str = "POST",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.config.rest_base}/{path.lstrip('/')}"
        headers = {
            "Authorization": self.config.basic_header,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        _, text = _request(url, method=method, headers=headers, body=body)
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise BoomiAPIError(f"REST returned non-JSON from {path}: {text[:400]}") from exc

    def rest_raw(self, path: str, accept: str = "application/xml") -> str:
        """GET a REST path and return the body as text.

        Component definitions come back as XML rather than JSON, so they need a
        path that does not try to parse the response.
        """
        url = f"{self.config.rest_base}/{path.lstrip('/')}"
        _, text = _request(
            url, headers={"Authorization": self.config.basic_header, "Accept": accept}
        )
        return text

    def _query_more(self, object_name: str, token: str) -> dict[str, Any]:
        """Fetch the next page of a Platform API query.

        The token is the entire request body as **raw plain text** -- no JSON quotes,
        no wrapper object. Boomi's spec declares text/plain for all 73 queryMore
        paths. Wrapping it as {"queryToken": ...} returns "Query token does not have
        the proper format", and JSON-quoting it ("abc") sends the quotes as part of
        the token. The JSON form is kept only as a fallback.

        Tokens routinely contain / and + characters, so the value is sent verbatim
        with no encoding or trimming.
        """
        url = f"{self.config.rest_base}/{object_name}/queryMore"
        attempts = (
            ("text/plain", token.encode("utf-8")),
            ("application/json", json.dumps(token).encode("utf-8")),
        )
        last_error: Exception | None = None
        for content_type, body in attempts:
            try:
                _, text = _request(
                    url,
                    method="POST",
                    headers={
                        "Authorization": self.config.basic_header,
                        "Content-Type": content_type,
                        "Accept": "application/json",
                    },
                    body=body,
                )
                return json.loads(text) if text.strip() else {}
            except BoomiAPIError as exc:
                last_error = exc
        raise last_error if last_error else BoomiAPIError("queryMore failed")

    def rest_query_all(
        self,
        object_name: str,
        query_filter: dict[str, Any] | None = None,
        max_results: int | None = None,
    ) -> list[dict[str, Any]]:
        """Run a Platform API query, following queryMore until done or capped.

        Note this is POST /{Object}/query, not GET /{Object}. There is no GET on the
        collection endpoints -- code that assumes there is will appear to work against
        small accounts and then fail in ways that are hard to trace.

        max_results stops the paging early. Without it, a caller that only wants the
        first thirty components still pays for every page in the account, which on a
        large account is both slow and a lot of avoidable requests.
        """
        payload: dict[str, Any] = query_filter or {"QueryFilter": {"expression": {}}}
        results: list[dict[str, Any]] = []

        response = self.rest(f"{object_name}/query", payload=payload)
        results.extend(response.get("result", []) or [])
        token = response.get("queryToken")

        while token and (max_results is None or len(results) < max_results):
            response = self._query_more(object_name, token)
            batch = response.get("result", []) or []
            if not batch:
                break
            results.extend(batch)
            token = response.get("queryToken")

        return results[:max_results] if max_results is not None else results


def build_client() -> BoomiClient:
    """Construct a client, resolving credentials from the first source that has them.

    Kept deliberately small so every CLI entry point starts the same way and any
    credential problem surfaces as one clear message before work begins.
    """
    load_credentials()
    return BoomiClient()


def load_credentials() -> None:
    """Load credentials from the project .env, then the saved global store.

    Later sources do not overwrite earlier ones, so a project `.env` wins over the
    global store, which in turn wins over the shell. Every decision is recorded so
    `es_setup.py --check` can say exactly where each value came from — which is the
    question people actually have when a credential is not what they expected.
    """
    _SOURCE_LOG.clear()
    claimed: dict[str, str] = {}

    for path, label in credential_sources():
        if not os.path.exists(path):
            _SOURCE_LOG.append(f"not found: {label} ({path})")
            continue
        loaded = _read_env_file(path)
        if not loaded:
            _SOURCE_LOG.append(f"empty: {label} ({path})")
            continue
        applied, shadowed = [], []
        for key, value in loaded.items():
            if key in claimed:
                shadowed.append(key)
                continue
            claimed[key] = label
            os.environ[key] = value
            applied.append(key)
        detail = f"{label} ({path}): {len(applied)} value(s)"
        if shadowed:
            detail += f"; {len(shadowed)} already set by a more specific source"
        _SOURCE_LOG.append(detail)

    for key in ("BOOMI_ACCOUNT_ID", "BOOMI_USERNAME", "BOOMI_API_TOKEN"):
        if key not in claimed and os.environ.get(key):
            _SOURCE_LOG.append(f"{key}: from the shell environment")


def describe_sources() -> list[str]:
    """Human-readable account of where credentials were resolved from."""
    load_credentials()
    lines = list(_SOURCE_LOG)
    config = Config()
    lines.append("")
    for key, value in (
        ("BOOMI_ACCOUNT_ID", config.account_id),
        ("BOOMI_USERNAME", config.username),
        ("BOOMI_API_TOKEN", "set" if config.api_token else ""),
        ("BOOMI_PROTECTED_ENVIRONMENTS", ", ".join(config.protected)),
    ):
        lines.append(f"{key:<30} {value or '(not set)'}")
    return lines


def _read_env_file(path: str) -> dict[str, str]:
    """Parse a KEY=value file. utf-8-sig strips the BOM Windows editors add."""
    values: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        return {}
    return values


