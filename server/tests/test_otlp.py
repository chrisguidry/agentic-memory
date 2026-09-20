"""Reading an OTLP logs export.

These cover the decoding, which is where the defects have been. Each one is
a shape a producer can send, and the answer the store should hold.
"""

import json

import pytest

from agentic_memory.otlp import (
    attributes,
    clean,
    entry_of,
    fingerprint,
    raw,
    resource_row,
    row,
    scope_row,
    walk,
)


def logs(record: dict, resource: dict | None = None) -> dict:
    """One log record, in the envelopes OTLP puts around it."""
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": resource or []},
                "scopeLogs": [{"scope": {"name": "agentic-memory.test"}, "logRecords": [record]}],
            }
        ]
    }


def only(payload: dict) -> dict:
    """The single row a payload produces."""
    rows = [row(resource, scope, record) for resource, scope, record in walk(payload)]
    assert len(rows) == 1
    return rows[0]


def text(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def whole(key: str, value: int) -> dict:
    return {"key": key, "value": {"intValue": str(value)}}


def carrying(revision: str, entry: str = "e1") -> dict:
    """A record from one entry, with context that changes between captures.

    The revision stands for everything a path records about the repository
    around a message rather than about the message itself.
    """
    return {
        "body": {"stringValue": "hi"},
        "attributes": [
            text("session.id", "s1"),
            text("agentic_memory.entry.id", entry),
            text("vcs.ref.head.revision", revision),
        ],
    }


class TestBody:
    def test_a_string_body_decodes_to_its_text(self):
        found = only(logs({"body": {"stringValue": "hello"}}))
        assert found["body"] == "hello"

    def test_a_structured_body_is_kept_as_json(self):
        found = only(logs({"body": {"kvlistValue": {"values": [text("k", "v")]}}}))
        assert json.loads(found["body"]) == {"k": "v"}

    def test_a_missing_body_is_null(self):
        assert only(logs({}))["body"] is None

    def test_a_body_that_is_not_an_any_value_is_null_not_a_placeholder(self):
        # A bare string is not OTLP. Turning it into the text "null" would
        # hide the difference between a missing body and a broken one.
        assert only(logs({"body": "hello"}))["body"] is None


class TestProvenance:
    def test_the_standard_names_land_in_their_columns(self):
        found = only(
            logs(
                {
                    "body": {"stringValue": "hi"},
                    "attributes": [
                        text("session.id", "s1"),
                        text("session.previous_id", "s0"),
                        text("gen_ai.agent.name", "pi"),
                        text("gen_ai.operation.name", "chat"),
                        text("gen_ai.request.model", "a-model"),
                        text("gen_ai.tool.name", "bash"),
                        text("agentic_memory.entry.id", "e1"),
                        text("agentic_memory.actor", "human"),
                        whole("agentic_memory.actor.depth", 0),
                        text("agentic_memory.root", "person"),
                        text("agentic_memory.kind", "prompt"),
                        text("vcs.repository.url.full", "https://example.test/r.git"),
                        text("process.working_directory", "/tmp/p"),
                    ],
                }
            )
        )
        assert found["session_id"] == "s1"
        assert found["previous_session"] == "s0"
        assert found["harness"] == "pi"
        assert found["operation"] == "chat"
        assert found["model"] == "a-model"
        assert found["tool_name"] == "bash"
        assert found["entry_id"] == "e1"
        assert found["actor"] == "human"
        assert found["actor_depth"] == 0
        assert found["root"] == "person"
        assert found["kind"] == "prompt"
        assert found["repository"] == "https://example.test/r.git"
        assert found["working_directory"] == "/tmp/p"

    def test_a_resource_attribute_fills_a_column_the_record_leaves_out(self):
        found = only(
            logs(
                {"body": {"stringValue": "hi"}, "attributes": []},
                resource=[text("host.name", "a-machine"), text("gen_ai.agent.name", "pi")],
            )
        )
        assert found["machine"] == "a-machine"
        assert found["harness"] == "pi"

    def test_the_record_wins_over_the_resource(self):
        found = only(
            logs(
                {"body": {"stringValue": "hi"}, "attributes": [text("gen_ai.agent.name", "pi")]},
                resource=[text("gen_ai.agent.name", "something-else")],
            )
        )
        assert found["harness"] == "pi"


class TestUsage:
    def test_tokens_and_cost_land_in_their_columns(self):
        found = only(
            logs(
                {
                    "body": {"stringValue": "hi"},
                    "attributes": [
                        whole("gen_ai.usage.input_tokens", 1200),
                        whole("gen_ai.usage.output_tokens", 40),
                        whole("gen_ai.usage.cache_read.input_tokens", 900),
                        {"key": "agentic_memory.cost.total", "value": {"doubleValue": 0.0042}},
                    ],
                }
            )
        )
        assert found["input_tokens"] == 1200
        assert found["output_tokens"] == 40
        assert found["cache_read_tokens"] == 900
        assert found["cost_total"] == pytest.approx(0.0042)

    def test_a_token_count_that_is_not_a_number_is_null(self):
        found = only(
            logs(
                {
                    "body": {"stringValue": "hi"},
                    "attributes": [text("gen_ai.usage.input_tokens", "many")],
                }
            )
        )
        assert found["input_tokens"] is None


class TestTime:
    def test_nanoseconds_become_a_timestamp(self):
        # 1767225600 seconds is 2026-01-01T00:00:00Z.
        found = only(logs({"body": {"stringValue": "hi"}, "timeUnixNano": "1767225600000000000"}))
        assert found["occurred_at"].isoformat() == "2026-01-01T00:00:00+00:00"

    def test_a_missing_time_is_null(self):
        assert only(logs({"body": {"stringValue": "hi"}}))["occurred_at"] is None

    def test_a_time_that_is_not_a_number_is_null(self):
        found = only(logs({"body": {"stringValue": "hi"}, "timeUnixNano": "later"}))
        assert found["occurred_at"] is None


class TestClean:
    def test_a_nul_byte_becomes_the_replacement_character(self):
        assert clean("a\x00b") == "a\ufffdb"

    def test_a_nul_byte_inside_a_nested_value_is_replaced(self):
        assert clean({"k": ["a\x00b"]}) == {"k": ["a\ufffdb"]}

    def test_a_record_carrying_a_nul_reaches_the_store(self):
        # Postgres refuses U+0000 in both text and jsonb, so a transcript
        # that carries one has to be cleaned before it is stored.
        payload = logs({"body": {"stringValue": "before\x00after"}})
        assert "\x00" not in only(payload)["body"]

        resource, scope, record = next(walk(payload))
        assert "\x00" not in raw(record)["record"]


class TestTheSplit:
    def test_the_raw_row_keeps_the_record_whole_and_names_the_entry(self):
        payload = logs(
            {
                "body": {"stringValue": "hi"},
                "timeUnixNano": "1767225600000000000",
                "attributes": [
                    text("session.id", "s1"),
                    text("agentic_memory.entry.id", "e1"),
                ],
            }
        )
        _, _, record = next(walk(payload))
        found = raw(record)
        # The maps have tables of their own, and the session and the entry are
        # the deduplication key, so the raw row names both rather than
        # repeating them.
        assert set(found) == {"session_id", "entry_id", "record"}
        assert (found["session_id"], found["entry_id"]) == ("s1", "e1")

    def test_the_unpacked_row_carries_the_lifted_fields(self):
        found = only(
            logs(
                {
                    "body": {"stringValue": "hi"},
                    "attributes": [text("session.id", "s1"), text("agentic_memory.kind", "prompt")],
                },
                resource=[text("host.name", "a-machine")],
            )
        )
        assert found["body"] == "hi"
        assert found["session_id"] == "s1"
        assert found["kind"] == "prompt"
        assert found["machine"] == "a-machine"
        assert "session.id" in found["attributes"]

    def test_the_unpacked_row_leaves_the_maps_to_their_tables(self):
        found = only(logs({"body": {"stringValue": "hi"}}))
        assert "resource" not in found
        assert "scope_name" not in found
        assert "scope_attributes" not in found

    def test_two_paths_capturing_one_entry_name_it_the_same_way(self):
        # A live hook and a backfill read the same entry and record different
        # context around it. They derive the same identity, so the record is
        # stored once.
        live = next(walk(logs(carrying("aaa"))))
        swept = next(walk(logs(carrying("bbb"))))
        assert entry_of(live[2]) == entry_of(swept[2])

    def test_two_entries_are_named_differently(self):
        first = next(walk(logs(carrying("aaa", entry="e1"))))
        second = next(walk(logs(carrying("aaa", entry="e2"))))
        assert entry_of(first[2]) != entry_of(second[2])

    def test_a_record_with_no_entry_of_its_own_has_no_identity(self):
        _, _, record = next(walk(logs({"body": {"stringValue": "hi"}})))
        assert entry_of(record) == (None, None)


class TestTheMaps:
    def test_a_resource_row_holds_its_map_and_a_name_for_it(self):
        resource, _, _ = next(walk(logs({"body": {}}, resource=[text("host.name", "a-machine")])))
        found = resource_row(resource)
        assert found["fingerprint"] == fingerprint(resource)
        assert "a-machine" in found["resource"]

    def test_a_scope_row_lifts_the_name_and_keeps_the_attributes(self):
        _, scope, _ = next(walk(logs({"body": {}})))
        found = scope_row(scope)
        assert found["fingerprint"] == fingerprint(scope)
        assert set(found) == {"fingerprint", "name", "version", "attributes"}

    def test_two_identical_maps_get_the_same_name(self):
        first, _, _ = next(walk(logs({"body": {}}, resource=[text("host.name", "a-machine")])))
        second, _, _ = next(walk(logs({"body": {}}, resource=[text("host.name", "a-machine")])))
        assert fingerprint(first) == fingerprint(second)

    def test_two_different_maps_get_different_names(self):
        first, _, _ = next(walk(logs({"body": {}}, resource=[text("host.name", "a-machine")])))
        second, _, _ = next(walk(logs({"body": {}}, resource=[text("host.name", "another")])))
        assert fingerprint(first) != fingerprint(second)


class TestEnvelopes:
    def test_every_record_in_an_export_is_read(self):
        payload = {
            "resourceLogs": [
                {
                    "resource": {"attributes": []},
                    "scopeLogs": [
                        {
                            "scope": {"name": "s"},
                            "logRecords": [
                                {"body": {"stringValue": "one"}},
                                {"body": {"stringValue": "two"}},
                            ],
                        }
                    ],
                }
            ]
        }
        bodies = [r["body"] for _, _, record in walk(payload) for r in [row({}, {}, record)]]
        assert bodies == ["one", "two"]

    def test_an_export_with_no_records_produces_nothing(self):
        assert list(walk({"resourceLogs": []})) == []


class TestAttributeValues:
    @pytest.mark.parametrize(
        "encoded, expected",
        [
            ({"stringValue": "s"}, "s"),
            ({"intValue": "7"}, 7),
            ({"doubleValue": 1.5}, 1.5),
            ({"boolValue": True}, True),
            ({"arrayValue": {"values": [{"intValue": "1"}]}}, [1]),
            ({"unknownValue": "x"}, None),
            ("not-an-object", None),
        ],
    )
    def test_each_type_decodes(self, encoded, expected):
        assert attributes([{"key": "k", "value": encoded}]) == {"k": expected}
