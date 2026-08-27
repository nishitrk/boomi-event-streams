# Boomi Event Streams API reference

What the plugin talks to, and the three shapes of the API that forced design
decisions in the code.

## Authentication

Two APIs, two schemes, one set of credentials.

**Platform REST API** — Basic auth.

```
Authorization: Basic base64("BOOMI_TOKEN.{email}:{api-token}")
Base URL:      https://api.boomi.com/api/rest/v1/{accountId}
```

**GraphQL API** — Bearer auth with a JWT minted from those same Basic credentials.

```
GET https://api.boomi.com/auth/jwt/generate/{accountId}
    Authorization: Basic base64("BOOMI_TOKEN.{email}:{api-token}")
    X-Boomi-OTP: <value>          # only if the account enforces MFA

→ the raw JWT as plain text. Not JSON. Do not call .json() on it.

POST https://api.boomi.com/graphql
    Authorization: Bearer <jwt>
    Content-Type: application/json
    {"query": "...", "variables": {...}}
```

The JWT lives about five minutes. `boomi_auth.py` caches it and decodes the `exp`
claim to know when to re-mint, rather than minting one per call.

> **A trap worth naming.** Boomi's published Altair sample script compares `exp`
> against `Date.now()`. `exp` is in seconds and `Date.now()` is in milliseconds, so
> the cache never hits and every call mints a fresh token. `_jwt_expiry()` works in
> seconds throughout.

> **A second trap.** The GraphQL endpoint returns **HTTP 200 even when auth fails.**
> The failure arrives as `{"errors": [{"message": "... Unauthorized"}]}` in the body.
> Code that checks only the status code turns "Unauthorized" into an empty result
> set, which reads as "this account has no topics". `graphql()` always inspects the
> errors array.

Regional variant: UK/GB accounts use `https://api.platform.gb.boomi.com` for both.

Schema introspection needs no authentication at all, so the schema can be verified in
CI without credentials.

## Three API shapes that forced design decisions

**There is no top-level subscription query.** Subscriptions exist only nested inside
a topic. Listing subscriptions means listing topics and flattening, which is what
`EventStreamsClient.subscriptions()` does.

**There is no token query.** Tokens hang off the platform environment tree, at a
different query root from everything else:

```graphql
{ environments { id name eventStreams { region tokens { id name allowProduce ... } } } }
```

**Subscription type is read-only.** `EventStreamsSubscriptionCreateInput` has no
`type` field. See `limitations.md`.

## Queries used

```graphql
eventStreamsTopics(environmentId: ID) -> [EventStreamsTopic!]
# omit environmentId for every environment

# EventStreamsTopic:  name, description, persistent, partitions,
#                     restProduceUrl, subscriptions { name description type
#                                                     durable backlogCount }

environments -> [Environment!]
# Environment.eventStreams: region, restProduceBaseUrl,
#                           tokens { id name data allowConsume allowProduce
#                                    expirationTime expirationEditable
#                                    createdTime description }
```

`EventStreamsToken.data` is the live JWT used in a connection component's
`environmentToken` field. The plugin never prints it.

`EventStreamsTopic.restProduceUrl` gives the REST produce URL directly — no need to
build the `{region}.eventstreams.boomi.com/rest/...` pattern by hand.

## Mutations used

All three are additive. The API also exposes `eventStreamsTopicDelete`,
`eventStreamsSubscriptionDelete`, and `eventStreamsTokenDelete`; this plugin does not
implement them, which is the point rather than an oversight.

```graphql
eventStreamsTopicCreate(input: EventStreamsTopicCreateInput!)
  # environmentId: ID!  name: ID!  persistent: Boolean
  # description: String  partitions: Int

eventStreamsSubscriptionCreate(input: EventStreamsSubscriptionCreateInput!)
  # environmentId: ID!  topicName: ID!  name: ID!  description: String
  # note: no type field

eventStreamsTokenCreate(input: EventStreamsEnvironmentTokenCreateInput!)
  # environmentId: ID!  name: String  allowConsume: Boolean!
  # allowProduce: Boolean!  expirationTime: DateTime  description: String
```

## Platform REST endpoints used

```
POST /ComponentMetadata/query    # find connector operations and processes
GET  /Component/{componentId}    # component XML, for the topology map
POST /Environment/query          # environments
```

> **Note the method.** Queries are `POST /{Object}/query`, not `GET /{Object}`.
> There is no GET on the collection endpoints. Code that assumes there is tends to
> appear to work against small accounts and then fail confusingly.

Query filter shape:

```json
{"QueryFilter": {"expression": {
  "operator": "EQUALS", "property": "environmentId", "argument": ["<id>"]}}}
```

Operators: `EQUALS`, `LIKE`, `NOT_EQUALS`, `IS_NULL`, `IS_NOT_NULL`, `BETWEEN`,
`GREATER_THAN`, `GREATER_THAN_OR_EQUAL`, `LESS_THAN`, `LESS_THAN_OR_EQUAL`,
`CONTAINS`, `NOT_CONTAINS`. Results paginate via `queryToken` → `/queryMore`, which
`rest_query_all()` follows automatically.

## Building the topology map

Boomi does not expose "which processes use this topic". It is reconstructed:

1. `POST /ComponentMetadata/query` with `type = connector-action` → operations,
   filtered to Event Streams by subType or name.
2. `GET /Component/{id}` for each → topic name and action from the XML.
3. `POST /ComponentMetadata/query` with `type = process` → every process.
4. `GET /Component/{id}` for each → match referenced component IDs against step 2.

Step 4 is the expensive one. `es_topology.py` caches component XML under `.es-cache/`
keyed by ID and version, and offers `--limit` and `--skip-processes`.

## Sources

- [GraphQL authentication](https://developer.boomi.com/docs/APIs/GraphQL/Authentication)
- [Event Streams GraphQL overview](https://developer.boomi.com/docs/APIs/GraphQL/EventStreams_GraphQL_apis_overview)
- [Subscription type enum](https://developer.boomi.com/docs/APIs/GraphQL/APIReference/types/event-streams-admin/enums/event-streams-subscription-type)
- [Platform API authentication](https://developer.boomi.com/docs/APIs/PlatformAPI/Introduction/Platform_API_and_Partner_API_authentication)
- [Platform OpenAPI spec](https://developer.boomi.com/APIs/platformOpenAPISpec.json)
