-- Four tables, because the record has four parts with different lifetimes.
--
-- `resources` and `scopes` are the two maps every OpenTelemetry signal
-- carries, whatever the signal is. A log record, a span, and a metric all
-- point at the same resource and the same instrumentation scope, so the maps
-- get a table each rather than a column each in every table that follows.
-- There are two distinct values of each today, and they are immutable, so the
-- join is a lookup and the rows are tiny.
--
-- `otel_exports` is what arrived, verbatim, and it is the permanent copy. It
-- exists so a change to the unpack can be replayed against records already
-- stored instead of asking every harness to send them again. Nothing reads it
-- but the unpack, so it carries almost no indexing.
--
-- `logs` is the unpacked form, and it is what a query reads. It takes the
-- shape ClickHouse gives OpenTelemetry logs: identity columns flattened beside
-- the body, with the hot fields lifted out of the attribute map so a query
-- does not have to walk one.

CREATE TABLE IF NOT EXISTS resources (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- A hash of the map, so an identical resource is found rather than
    -- written again.
    fingerprint text NOT NULL,
    resource    jsonb NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS resources_fingerprint
    ON resources (fingerprint);

CREATE TABLE IF NOT EXISTS scopes (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fingerprint text NOT NULL,

    -- Kept as columns as well as inside the map, because the name is what
    -- tells one producer from another and it is worth reading directly.
    name        text,
    version     text,
    attributes  jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS scopes_fingerprint
    ON scopes (fingerprint);

CREATE TABLE IF NOT EXISTS otel_exports (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    received_at  timestamptz NOT NULL DEFAULT now(),

    -- A hash of the resource, the scope, and the record, truncated to what
    -- identifying a record needs. Deduplication lives here rather than in the
    -- unpacked table, because the unpacked table is a projection of this one
    -- and cannot hold anything this one does not.
    content_hash text NOT NULL,

    resource_id  bigint NOT NULL REFERENCES resources(id),
    scope_id     bigint NOT NULL REFERENCES scopes(id),
    record       jsonb NOT NULL,

    -- Null until the unpack has read it, so the unpack knows what is new.
    unpacked_at  timestamptz
);

-- The only index the raw table needs, and the only thing that makes it
-- cheaper to send a record twice.
CREATE UNIQUE INDEX IF NOT EXISTS otel_exports_content
    ON otel_exports (content_hash);

-- Small, because it covers only the rows the unpack has not read.
CREATE INDEX IF NOT EXISTS otel_exports_pending
    ON otel_exports (id)
    WHERE unpacked_at IS NULL;

CREATE TABLE IF NOT EXISTS logs (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    export_id         bigint NOT NULL REFERENCES otel_exports(id),
    received_at       timestamptz NOT NULL,

    -- The resource is reachable through the export, and carried here anyway,
    -- because it is eight bytes and it means a query never has to join for it.
    resource_id       bigint NOT NULL REFERENCES resources(id),
    scope_id          bigint NOT NULL REFERENCES scopes(id),

    -- identity, flattened, the way every OpenTelemetry store does it
    occurred_at       timestamptz,
    observed_at       timestamptz,
    severity          text,
    severity_number   smallint,
    trace_id          text,
    span_id           text,

    -- the payload. The attribute map stays whole, because it is the source of
    -- truth for everything lifted below it.
    body              text,
    attributes        jsonb NOT NULL,

    -- the hot fields, lifted out of the maps so a query does not have to walk
    -- one
    session_id        text,
    previous_session  text,
    entry_id          text,
    harness           text,
    machine           text,
    actor             text,
    actor_depth       integer,
    root              text,
    kind              text,
    operation         text,
    provider          text,
    model             text,
    response_model    text,
    tool_name         text,
    scope_key         text,
    scope_kind        text,
    repository        text,
    owner             text,
    revision          text,
    working_directory text,
    input_tokens      bigint,
    output_tokens     bigint,
    cache_read_tokens bigint,
    cache_write_tokens bigint,
    reasoning_tokens  bigint,
    cost_total        numeric(12, 6),
    error_type        text
);

CREATE INDEX IF NOT EXISTS logs_session
    ON logs (session_id, occurred_at);

CREATE INDEX IF NOT EXISTS logs_scope
    ON logs (scope_key, occurred_at DESC);

CREATE INDEX IF NOT EXISTS logs_kind
    ON logs (kind);

CREATE INDEX IF NOT EXISTS logs_occurred
    ON logs (occurred_at DESC);

CREATE INDEX IF NOT EXISTS logs_harness
    ON logs (harness);
