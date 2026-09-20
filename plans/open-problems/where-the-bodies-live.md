# Where the bodies live

## The problem

The record stores every session body verbatim in Postgres, and the
design accepts that until the volume says otherwise. The measurement
covers one machine and two months. A body large enough to slow every
read would show up as a slow service rather than as a full disk, and
the point where that happens is not known.

## What is known

- One machine held 1,609 session files totalling 2.4 GB. The most
  recent month produced 1,850 MB in twenty days, which is about 92 MB
  per day and near 33 GB per year.
- Tool results are 79 percent of the bytes and the conversation between
  the person and the agent is 8 percent. The record keeps both.
- Postgres stores a large `text` value through TOAST, which compresses
  it and moves it out of the main heap. A read of a value past the
  threshold pays a second lookup.
- `events` partitions by month, so a year of record is twelve
  partitions under one schema.
- The alternatives are a `bytea` column with compression in the
  application, or object storage with the row holding a key.

## What would settle it

A measurement after one month of real ingest: the size of `events`, its
size on disk after compression, the p95 time to read one session, and
the time to search one month of bodies.

The decision follows the p95 read time rather than the total size. The
plan that moves bodies sets its threshold from that number.
