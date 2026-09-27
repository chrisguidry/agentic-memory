# 09, The retries

Closed 2026-09-27.

## The problem

Two failures lose work today, one on each side of the network.

On the person's machine, the bastion treats every failure to ship a
transcript as a refusal by the service. It keeps the file in memory and
offers it again with a backoff that caps at five minutes, for as long as
the bastion runs. A transcript that was never written, or that Claude Code
has deleted, fails to open on every attempt. Over three days the bastion
logged about 1,200 retries for two such files, and it will log more until
it restarts. A restart has the opposite defect: the pending set is only in
memory, so a file the service really did refuse is dropped, and its lines
wait until the next event in that session ships them.

In the worker, a model call that fails is retried four times over about a
minute. On 2026-09-23 the provider refused 20 classify calls with a
permission error for longer than that. The tasks are keyed by the entry, so
nothing scheduled them again, and at least two of the person's prompts
from that afternoon have no reading and no statement.

## The shape

### The bastion

A failure to ship has one of three causes, and each gets its own handling.

| cause | example | handling |
|---|---|---|
| the file does not exist yet | a session's first prompt, before Claude Code writes the transcript | retry for a short grace period, then treat it as gone |
| the file is gone | a transcript the harness deleted or never wrote | one line in the journal, then drop it |
| the service or the network failed | a 503, a DNS failure, a TLS error | back off as today, with no limit on attempts |

The pending set, with each file's offset and fingerprint, is written to
the bastion's state directory, so a restart resumes where the last process
stopped. The count of files behind is visible in `top`.

### The worker

A `Perpetual` docket task sweeps for work that ended in a failure. It
finds:

- real prompts that have no reading under the current questions
- readings that cleared a kind's threshold and have no statement under the
  current questions

and schedules each one again, with the same task keys the live path uses,
so a sweep that meets a live task writes nothing twice. The ledger's
`error` rows name the session and entry of each failed call, so the sweep
can start from them and not scan the whole record.

A refusal is not swept. The provider refused the request itself, and
sending it again gets the same refusal.

## The contracts

- The bastion's state file is owned by the person and has mode 0600.
- The sweep's interval, and how far back it looks, are settings.
- The sweep's calls are recorded in the ledger under the run `sweep`, so
  they total separately from live use.

## How it is proved

- A test per cause of failure in the bastion, with invented transcripts:
  a missing file ships once it appears within the grace period, a gone file
  logs once and is dropped, and a service failure backs off and ships when
  the service answers.
- A bastion restarted with a file pending ships that file from its saved
  offset, and ships no line twice.
- A classify call that fails past its retries is read by the next sweep,
  and one that the provider refused is not.
- On its first run against the homelab store, the sweep reads the prompts
  the 2026-09-23 outage left unread.

## What the drill measured

The bastion ran from a separate build against a stub service, with its own
state directory and socket. Two lines shipped. While the stub answered 503,
two more failed and were counted behind. A transcript that did not exist yet
was held for its grace period, and a directory named as a transcript was
dropped at once with one journal line. `top` showed two transcripts behind,
and both state records had mode 0600.

After a SIGTERM and a restart, the bastion logged that it resumed two files
behind. When the stub answered 200, it shipped the two lines from their
saved offset, and the stub had received four lines, all distinct. At two
minutes the missing transcript was dropped, and the count behind was zero.

The grace period is two minutes. Claude Code writes the first prompt a
moment after the event that names the transcript, so two minutes covers a
slow disk with room. A file the bastion shipped from before and that is now
missing is gone at once, with no grace period. So is a path that is not a
regular file, or one that fails with a permission error, `ENOTDIR`,
`ELOOP`, or `ENAMETOOLONG`. Any other local error backs off like a service
failure, because waiting can fix it.

The offset is saved after each batch the service accepts. A file of 250
lines whose second batch was refused once resumed after the first batch, and
the service received each line once.

The sweep looks back 30 days, because an outage is often noticed days after
it happens and the ledger is about 1,500 rows. An entry stops being a
candidate at 12 error calls, which is three sweeps of a task's four
attempts, so an entry that fails for good is not retried for a month.

The sweep's first run against the homelab store, and whether it reads the
prompts the 2026-09-23 outage left unread, is measured at deploy.
