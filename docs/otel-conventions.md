# OpenTelemetry conventions this project uses

Every attribute below is a name OpenTelemetry already defines. This file
is the list the client and the service both write against, so a tag is
spelled once.

Read on 2026-09-20 from two sources.

| Source | Revision |
|---|---|
| [`open-telemetry/semantic-conventions`](https://github.com/open-telemetry/semantic-conventions) | `main` |
| [`open-telemetry/semantic-conventions-genai`](https://github.com/open-telemetry/semantic-conventions-genai) | `main`, commit `c88d504` |

The GenAI attributes moved out of the main repository. The page at
`opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/` is
deprecated and points at the second repository. Take the names from
there, not from the old page.

## The signals

A transcript record is an OpenTelemetry **log record**. The wire is
OTLP/HTTP on `/v1/logs`.

The standard event for a model call is
`gen_ai.client.inference.operation.details`. Its `requirement_level` is
`opt_in`, so a producer emits it deliberately, which is what this
project does.

## Resource attributes

These describe the process that emitted the record, so they are the same
for every record from one client.

| Attribute | Where it comes from |
|---|---|
| `service.name` | `agentic-memory` |
| `service.version` | the client's version |
| `service.instance.id` | one value per machine, stable across restarts |
| `host.name` | the machine's hostname |
| `host.id` | `/etc/machine-id` on Linux, `IOPlatformUUID` on macOS |
| `host.arch` | the machine's architecture |
| `os.type` | `linux`, `darwin`, `windows` |
| `os.version` | the kernel release |
| `os.description` | the distribution, when known |
| `telemetry.sdk.name` | `agentic-memory` |
| `telemetry.sdk.language` | `typescript` or `python` |
| `telemetry.sdk.version` | the SDK's version |

## What the record is

| Attribute | Values |
|---|---|
| `gen_ai.operation.name` | `chat` for a model response, `execute_tool` for a tool result, `invoke_agent` for a subagent, `search_memory` for a recall, `create_memory` for an extraction |
| `gen_ai.agent.name` | the harness: `pi`, `claude-code`, `openwebui` |
| `gen_ai.agent.version` | the harness's version, when it reports one |
| `gen_ai.provider.name` | `anthropic`, `openai`, `deepinfra` |
| `gen_ai.request.model` | the model the harness asked for |
| `gen_ai.response.model` | the model that answered |
| `gen_ai.response.id` | the provider's response identifier |
| `gen_ai.response.finish_reasons` | the stop reason |

## The conversation

| Attribute | Meaning |
|---|---|
| `session.id` | the harness's session identifier |
| `session.previous_id` | the session this one forked or resumed from |
| `gen_ai.conversation.id` | the same value as `session.id`, so a consumer of either convention finds it |
| `gen_ai.input.messages` | the messages that went to the model |
| `gen_ai.output.messages` | the messages that came back |
| `gen_ai.system_instructions` | the system prompt |

## Tokens and cost

| Attribute | Meaning |
|---|---|
| `gen_ai.usage.input_tokens` | input tokens |
| `gen_ai.usage.output_tokens` | output tokens |
| `gen_ai.usage.cache_read.input_tokens` | tokens read from the prompt cache |
| `gen_ai.usage.cache_write.input_tokens` | tokens written to the prompt cache |
| `gen_ai.usage.reasoning.output_tokens` | tokens spent on reasoning |
| `gen_ai.request.reasoning.level` | the thinking level the harness asked for |

Cost has no OpenTelemetry attribute. It goes in
`agentic_memory.cost.total`, in United States dollars.

## Tools

| Attribute | Meaning |
|---|---|
| `gen_ai.tool.name` | the tool |
| `gen_ai.tool.call.id` | the identifier the harness assigned the call |
| `gen_ai.tool.type` | `function`, `extension`, or `datastore` |
| `gen_ai.tool.call.arguments` | what the tool received |
| `gen_ai.tool.call.result` | what the tool returned |

## Memory

The specification defines these, and they are the reason the recall path
can use standard names.

| Attribute | Meaning |
|---|---|
| `gen_ai.memory.store.id` | the store a record came from |
| `gen_ai.memory.query.text` | the query a recall ran |
| `gen_ai.memory.records` | the records a recall returned |
| `gen_ai.memory.record.id` | one memory's identifier |
| `gen_ai.memory.record.count` | how many records the operation touched |

## The outcome

| Attribute | Meaning |
|---|---|
| `gen_ai.evaluation.name` | what was measured |
| `gen_ai.evaluation.score.value` | the score |
| `gen_ai.evaluation.score.label` | the label that goes with the score |
| `gen_ai.evaluation.explanation` | why the score is what it is |

## Where the work happened

| Attribute | Meaning |
|---|---|
| `vcs.repository.name` | the repository |
| `vcs.repository.url.full` | the repository's remote |
| `vcs.owner.name` | the organization the repository belongs to |
| `vcs.ref.head.name` | the branch |
| `vcs.ref.head.revision` | the commit |
| `process.working_directory` | the working directory the session started in |
| `code.file.path`, `code.line.number` | a location in a file, for a record about one |
| `error.type` | the error, on a record that carries one |

## The scope

| Attribute | Meaning |
|---|---|
| `agentic_memory.scope` | the scope key, such as `github.com/acme/widget` |
| `agentic_memory.scope.kind` | `repo`, `org`, `forge`, or `directory` |

The scope has no OpenTelemetry attribute. It is derived on the client from
the directories the session is in, because only the machine knows its own
layout, and [`plans/00-design.md`](../plans/00-design.md) holds the scheme.

## What OpenTelemetry does not define

These carry what the conventions above do not reach. They use one prefix
so a reader can tell them apart from a standard name at a glance.

| Attribute | Meaning |
|---|---|
| `agentic_memory.entry.id` | the harness's own identifier for this entry, which makes a re-send harmless |
| `agentic_memory.actor` | `human`, `agent`, or `system` |
| `agentic_memory.actor.depth` | how many agents separate the text from a person |
| `agentic_memory.root` | `person` when a person began the session, `autonomous` when no person did |
| `agentic_memory.kind` | `prompt`, `response`, `thinking`, `tool_call`, `tool_result`, or `system` |
| `agentic_memory.scope` | the scope key |
| `agentic_memory.scope.kind` | `repo`, `org`, `forge`, or `directory` |
| `agentic_memory.cost.total` | the cost of the operation, in United States dollars |
| `agentic_memory.content` | the harness's own structured form of the message, so nothing is lost in the translation |
