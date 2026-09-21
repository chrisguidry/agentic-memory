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

    -- The session and the entry a record came from, lifted out of the record.
    -- Two paths that capture the same session read the same entry and derive
    -- the same value for it, while the context they record around it differs.
    -- The record's own bytes cannot decide this, or a live hook and a backfill
    -- would store one entry twice.
    --
    -- Null when a harness sends a record with no entry of its own. A null
    -- never conflicts, so such a record is stored every time it arrives.
    session_id   text,
    entry_id     text,

    resource_id  bigint NOT NULL REFERENCES resources(id),
    scope_id     bigint NOT NULL REFERENCES scopes(id),
    record       jsonb NOT NULL,

    -- Null until the unpack has read it, so the unpack knows what is new.
    unpacked_at  timestamptz
);

-- The only index the raw table needs, and the only thing that makes it
-- cheaper to send a record twice.
CREATE UNIQUE INDEX IF NOT EXISTS otel_exports_entry
    ON otel_exports (session_id, entry_id);

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

-- The scope index uses text_pattern_ops because the database collates as
-- en_US.utf8, and a plain btree cannot serve `scope_key LIKE 'prefix%'` under
-- that collation. Retrieval walks up the scope path, so the prefix match is
-- the common case and equality is the rare one. The pattern index serves
-- both, where a plain one served only the equality and left the prefix to a
-- filter over every row in the scope.
CREATE INDEX IF NOT EXISTS logs_scope
    ON logs (scope_key text_pattern_ops, occurred_at DESC);

-- Search over the body, because the question is usually "where did I say
-- this" rather than "what happened at 14:32". Unindexed this is a sequential
-- scan that stops early on a common word and reads the whole table on a rare
-- one, which is the case that matters.
CREATE INDEX IF NOT EXISTS logs_body_search
    ON logs USING gin (to_tsvector('english', body));

-- Containment over the attribute map, for the keys that are not lifted into
-- columns. jsonb_path_ops rather than the default jsonb_ops, because every
-- query here is containment and the default also indexes keys and values,
-- which costs 146 MB against 63 for a capability nothing asks for.
--
-- This buys coverage, not speed. A btree on one of these keys is 9 MB and
-- answers that key just as fast, so the reason to keep it is the handful of
-- high-cardinality ids worth looking up exactly. The low-cardinality keys are
-- not worth indexing anywhere: the branch a record is on has four values.
CREATE INDEX IF NOT EXISTS logs_attributes
    ON logs USING gin (attributes jsonb_path_ops);

CREATE INDEX IF NOT EXISTS logs_kind
    ON logs (kind);

CREATE INDEX IF NOT EXISTS logs_occurred
    ON logs (occurred_at DESC);

CREATE INDEX IF NOT EXISTS logs_harness
    ON logs (harness);

-- What the classifier read, and what it made of it. One row per prompt that
-- was read, written by the worker rather than at the door.
--
-- The probabilities are kept rather than a decision about them, so the
-- threshold is asked at read time. Moving it costs a query instead of reading
-- every window again.
--
-- The state is kept as well as the record it came from, because the record
-- grows: an entry the harness writes later falls inside a window that was
-- already read, and the reading would no longer be reproducible from the
-- record alone. This is the evidence of what the model saw.
CREATE TABLE IF NOT EXISTS classifications (
    id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id            text NOT NULL,
    entry_id              text NOT NULL,
    scope_key             text,
    model                 text NOT NULL,

    -- A short hash of the questions that were asked. The model name cannot do
    -- this job, because the questions move without the model moving, and an
    -- answer to the old question is not an answer to the new one.
    questions_fingerprint text NOT NULL,
    rounds                integer NOT NULL,

    -- The message being judged, and the exchanges before it that make the
    -- message readable. The questions inspect the first and use the second.
    state                 jsonb NOT NULL,

    -- One probability per kind of memory, lifted out of the map the model
    -- returned so a query does not have to walk one and a threshold per kind
    -- can use an index. A kind that is added or removed changes these columns,
    -- which is a migration, and the kinds are a closed set the code defines.
    semantic              real NOT NULL,
    procedural            real NOT NULL,
    prospective           real NOT NULL,
    preference            real NOT NULL,
    correction            real NOT NULL,
    praise                real NOT NULL,

    -- The highest probability any kind got, so the queue for the next stage is
    -- a range scan rather than a walk through every verdict. Derived rather
    -- than written, because it is a fact about the row and not a judgment.
    best real GENERATED ALWAYS AS (
        greatest(semantic, procedural, prospective, preference, correction, praise)
    ) STORED,

    classified_at         timestamptz NOT NULL DEFAULT now()
);

-- One reading per window per model per question set, so a retry writes
-- nothing, a second model can be added beside the first, and changing a
-- question does not leave the old answers standing as though they were
-- answers to the new one.
CREATE UNIQUE INDEX IF NOT EXISTS classifications_window
    ON classifications (session_id, entry_id, model, questions_fingerprint);

CREATE INDEX IF NOT EXISTS classifications_best
    ON classifications (best DESC, classified_at DESC);

-- A threshold per kind, because a kind like praise fires on far more than it
-- should and one number for all six cannot separate them.
CREATE INDEX IF NOT EXISTS classifications_correction
    ON classifications (correction DESC)
    WHERE correction >= 0.5;

CREATE INDEX IF NOT EXISTS classifications_scope
    ON classifications (scope_key, classified_at DESC);
