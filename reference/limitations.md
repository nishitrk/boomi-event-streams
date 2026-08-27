# Known limitations

Three constraints come from the Boomi Event Streams API rather than from this
plugin's design. They applied equally to the Agent Studio agent this plugin was
ported from. Two are unavoidable; one turned out to be fixable, and was fixed.

## 1. Migrated tokens receive new JWT values — unavoidable

`eventStreamsTokenCreate` mints a new token. The JWT value of the source token
cannot be transferred, because the value *is* the credential and it is issued per
environment.

**Consequence.** Any connection component in the target environment that referenced
the old token's value must be updated by hand after migration. Nothing in the
platform links a connection component to the token it uses, so this is not
detectable automatically — the integration simply fails to authenticate.

**Mitigation.** `es_migrate.py apply` prints a reminder whenever a plan includes
tokens, and the plan output carries the same warning before anything is executed.

## 2. Subscription type cannot be set at creation — unavoidable, and correct

`EventStreamsSubscriptionCreateInput` has no `type` field. Subscription type is one
of `NONE`, `EXCLUSIVE`, `SHARED`, `FAILOVER`, or `KEY_SHARED`, and the broker assigns
it when a consumer actually attaches — this is Pulsar semantics, which Event Streams
is built on.

**Consequence.** Every freshly created subscription reports `NONE` until something
consumes from it. This looks like a failed migration and is not one.

**Design response.** `create_subscription()` deliberately takes no `type` argument.
Offering the parameter would imply a guarantee the API cannot honour. `es_migrate.py
verify` excludes type from its comparison for the same reason: a live source and a
freshly migrated target will legitimately differ, and flagging that as a discrepancy
would train people to ignore the verification output.

## 3. Partition count and persistence — depends on the account

The Agent Studio version documented this as: "Source partition count is not
queryable. Migrated topics get 1 partition by default."

The situation is stranger than that, and understanding it matters more than the
field itself.

Boomi's published documentation says `EventStreamsTopic` exposes `partitions: Int`
and `persistent: Boolean`. **Schema introspection on a live account agrees** — ask
`__type(name: "EventStreamsTopic")` and both fields are listed. But sending a query
that selects them returns:

```
Validation error (FieldUndefined@[eventStreamsTopics/persistent]) :
  Field persistent in type EventStreamsTopic is undefined
```

The same endpoint advertises a field and then refuses it. The likely explanation is
that the GraphQL layer is a federated gateway: introspection returns the stitched
schema while the downstream service actually serving a given account runs an older
build. An auth failure on the same endpoint reports `Downstream Execution Error`,
which fits.

**What this means in practice.** Whether partition count and persistence are
available is a property of the account, not of the API in general, and neither the
documentation nor introspection will tell you reliably. On an account that refuses
them, migrated topics get whatever the platform defaults to and the plugin cannot
carry the source value across.

**How the plugin handles it.** It asks the executor rather than the schema. Queries
are built optimistically, and when a field is rejected the exact selection path is
parsed out of the error, dropped, and the query retried — remembered for the rest of
the run, so the cost is one extra round trip on the first call. `es_discover.py`
omits columns for fields the account cannot return, and `es_migrate.py verify` only
claims to have compared what it could actually read, so a verification pass never
implies a configuration match it did not make.

Run `es_schema.py` to see both pictures for an account: what introspection claims,
and which fields the executor actually accepted.

## Not a limitation: retention

There is no retention, TTL, or backlog-quota configuration on the Event Streams
topic type in any account observed so far. If someone asks the plugin to migrate
retention settings, the honest answer is that no such setting exists at this layer.

## A note on trusting descriptions of this API

Three separate defects during development came from believing a description of the
API instead of the API: the published docs, then introspection by type name, then
introspection at all. Two other traps are the same shape — the GraphQL endpoint
returns **HTTP 200 on authentication failure** with the error only in the response
body, and Boomi's own published Altair sample script compares a JWT `exp` claim (in
seconds) against `Date.now()` (in milliseconds), so its token cache never hits.

If you extend this plugin, test against a live account early. The schema is a
starting hypothesis here, not a specification.
