# Postgres may not be the end state for the record

## The problem

The record is append-only, grows forever, and is queried in aggregates over
time. Postgres stores it as rows, which is not the shape of the data, and the
gap widens as the record grows.

It is not a problem yet. The store holds a few hundred thousand records, and
nothing has been slow. This is written down so the reasoning survives, and so
the decision is made against a number rather than against a preference.

## What is known

- One machine's sessions from three harnesses came to 656,461 records and
  4,129 MB, of which 353 MB was the text and the rest was the provenance and
  the envelope around it.
- The growth is roughly 15 GB a year at the current rate, and the rate is
  rising.
- The unpacked table is deliberately shaped like ClickHouse's `otel_logs`,
  so moving the record to a columnar engine is a schema that already matches
  rather than a redesign.
- The Iceberg path works on local disk with no object storage and no catalog
  service: PyIceberg writes the table with a SQL catalog in SQLite, and
  `pg_duckdb` reads it through Postgres by naming the metadata file.
- Iceberg records absolute paths in its metadata, so the warehouse has to be
  mounted at the same path for the writer and the reader. This is the detail
  that costs the most time and is worth remembering.
- `pg_duckdb` cannot write Iceberg without an attached REST catalog. Reading
  needs none. So the writer would be PyIceberg and the reader DuckDB.
- Writing Iceberg through a table access method is a graveyard. TimescaleDB
  built one, shipped it, and deprecated it in September 2025 after deciding
  the experiment "did not show the signals hoped for."

## What would settle it

A measurement, not a preference. The trigger to move is a query over the
record that takes more than a second on a table we have.

Until then the cheap steps are in front of us and cost nothing: partition the
two tables by month so time ranges prune, and add a BRIN index if a scan
turns out to be the slow part.

The order to try when something is slow:

1. Partitioning, which is plain Postgres.
2. An index, if the slow query is a lookup rather than a scan.
3. A columnar mirror of the record only, with Postgres staying the source of
   truth, through `pg_duckdb` or Citus columnar.
4. The record moved to Iceberg on local disk, queried through `pg_duckdb`,
   with the memories staying relational.

The last one is a real destination and not a rewrite, because the unpacked
table already has the shape it would land in.
