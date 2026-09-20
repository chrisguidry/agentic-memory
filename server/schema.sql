-- The service stores each log record twice over: once as the OTLP envelope
-- that arrived, and once as the fields this project queries.
--
-- The envelope is what a producer actually sent. Keeping it means a change
-- to the extraction below can be replayed against records already stored,
-- instead of asking every producer to send them again.

CREATE TABLE IF NOT EXISTS otlp_log_records (
    id                bigserial PRIMARY KEY,
    received_at       timestamptz NOT NULL DEFAULT now(),

    -- the OTLP envelope, kept whole
    resource              jsonb NOT NULL,
    instrumentation_scope jsonb NOT NULL,
    record                jsonb NOT NULL,

    -- the log record itself
    occurred_at       timestamptz,
    observed_at       timestamptz,
    severity          text,
    body              text,

    -- provenance, from the attributes in docs/otel-conventions.md
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
    scope             text,
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
    error_type        text,
    trace_id          text,
    span_id           text,
    attributes        jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS otlp_log_records_session
    ON otlp_log_records (session_id, occurred_at);

CREATE INDEX IF NOT EXISTS otlp_log_records_scope
    ON otlp_log_records (scope, occurred_at DESC);

CREATE INDEX IF NOT EXISTS otlp_log_records_kind
    ON otlp_log_records (kind);

CREATE INDEX IF NOT EXISTS otlp_log_records_received
    ON otlp_log_records (received_at DESC);

-- A record that arrives twice is stored once. The entry id comes from the
-- harness, so a shim and a sweep derive the same value for the same entry
-- and the second copy lands on this index.
CREATE UNIQUE INDEX IF NOT EXISTS otlp_log_records_entry
    ON otlp_log_records (harness, session_id, entry_id)
    WHERE entry_id IS NOT NULL;
