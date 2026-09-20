# A session in two stores

## The problem

A store is the privacy boundary, and a harness can serve more than one
store. A shared OpenWebUI channel reaches one store for each person in
it, and the design leaves room for a store the household owns. What a
session reads and what it writes when two stores are attached is not
decided, and the answer decides the shape of the hook's configuration.

## What is known

- The hook's configuration names the stores for a surface, so the
  multiplicity is a configuration question and not a schema question.
- Writing one conversation to two stores runs extraction twice over the
  same text, and it splits the provenance of one statement across two
  places. The second is worse than the cost.
- Reading from two stores is useful. A personal session benefits from a
  decision the household recorded.
- The OpenWebUI filter receives the authenticated user, so it can pick a
  store per request. A channel with two people in it produces two
  interleaved conversations, and each person's store should receive only
  that person's turns.
- A household store has no single owner. The schema carries a
  `person_id` on every row, so a row written there still needs the
  person who said it.

## What would settle it

A decision between two shapes.

**A session writes to one store and reads from one.** The household
store is fed by its own surfaces and nothing else. This is the simpler
shape, and it proves the boundary before anything is built on top of
it.

**A session writes to one store and reads from several.** This needs a
rule for which store each retrieved memory came from, because a person
reading an injected memory must be able to tell where it originated and
who said it.

Then a drill against one OpenWebUI channel with two stores attached,
and a check that neither store received the other person's turns.
