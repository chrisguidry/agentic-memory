/**
 * Sends what pi records to an agentic-memory service as OTLP log records.
 *
 * Capture happens here, at the hook, instead of by reading session files
 * afterwards. pi hands over each message in its own structure, so this
 * extension parses no file format and does not depend on one.
 *
 * Nothing here may throw. It runs inside the harness, so an exception takes
 * down a turn, and a send that fails costs a record that the sweep will find.
 */

import { homedir, hostname } from "node:os";

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

import {
  kept,
  nanos,
  payload,
  projectOf,
  real,
  remoteParts,
  sessionIdIn,
  text,
  whole,
  type Attribute,
  type Record,
} from "./otlp";
import { scopeOf, type Scope } from "./scope";
import { block, recall } from "./recall";

const ENDPOINT =
  process.env.AGENTIC_MEMORY_ENDPOINT ?? "http://127.0.0.1:4318/v1/logs";
// The whole Authorization value, so a deployment behind a proxy can ask for
// `Basic ...` or `Bearer ...` without the extension knowing which.
const AUTHORIZATION = process.env.AGENTIC_MEMORY_AUTHORIZATION;
const MACHINE = process.env.AGENTIC_MEMORY_MACHINE ?? hostname();
const ENABLED = process.env.AGENTIC_MEMORY_DISABLED !== "1";

// Recall is a second path with its own endpoint and its own deadline. It shares
// nothing with capture: a batch being sent never delays a turn's ask, and an
// ask never rides in a batch.
const RECALL_ENDPOINT =
  process.env.AGENTIC_MEMORY_RECALL_ENDPOINT ?? ENDPOINT.replace(/\/v1\/logs$/, "/recall");
const RECALL_LIMIT = Number(process.env.AGENTIC_MEMORY_RECALL_LIMIT ?? "10");
const RECALL_ENABLED = process.env.AGENTIC_MEMORY_RECALL_DISABLED !== "1";
// pi holds the turn until this handler returns, so the deadline is short and
// it is ours. Past it, the turn goes on with no memory.
const RECALL_DEADLINE_MS = 150;

const BATCH = 64;
const FLUSH_AFTER_MS = 2_000;
const SEND_TIMEOUT_MS = 5_000;

const pending: Record[] = [];
let timer: ReturnType<typeof setTimeout> | undefined;
let repository: Attribute[] = [];
let scope: Scope | undefined;

/** Add a record to the batch, and send the batch when it is time. */
function enqueue(record: Record): void {
  pending.push(record);
  if (pending.length >= BATCH) {
    void flush();
  } else if (timer === undefined) {
    timer = setTimeout(() => void flush(), FLUSH_AFTER_MS);
  }
}

/**
 * Send what is pending.
 *
 * A send that fails drops the batch. Those records are not lost, because the
 * sweep reads the same session file afterwards, so nothing is retried here.
 */
async function flush(): Promise<void> {
  if (timer !== undefined) {
    clearTimeout(timer);
    timer = undefined;
  }
  if (pending.length === 0) return;

  const batch = pending.splice(0, pending.length);
  try {
    await fetch(ENDPOINT, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(AUTHORIZATION ? { authorization: AUTHORIZATION } : {}),
      },
      body: JSON.stringify(payload(MACHINE, batch)),
      signal: AbortSignal.timeout(SEND_TIMEOUT_MS),
    });
  } catch {
    // A missing collector is ordinary, and it is never the harness's problem.
  }
}

/** What describes the session, and is therefore on every record from it. */
function session(ctx: ExtensionContext): Attribute[] {
  const manager = ctx.sessionManager;
  const id = manager.getSessionId();
  const cwd = manager.getCwd() ?? ctx.cwd;

  return kept(
    text("session.id", id),
    text("session.previous_id", sessionIdIn(manager.getHeader()?.parentSession)),
    text("gen_ai.conversation.id", id),
    text("gen_ai.agent.name", "pi"),
    text("agentic_memory.scope", scope?.key),
    text("agentic_memory.scope.kind", scope?.kind),
    text("agentic_memory.root", "person"),
    whole("agentic_memory.actor.depth", 0),
    text("process.working_directory", cwd),
  );
}

interface About {
  kind: string;
  actor: string;
  body: string;
  /** Distinguishes several records that came from one session entry. */
  within?: string;
  extra?: (Attribute | undefined)[];
}

function build(
  ctx: ExtensionContext,
  message: { timestamp?: number },
  about: About,
): Record {
  const entry = ctx.sessionManager.getLeafId();
  const identity =
    entry === undefined
      ? undefined
      : [entry, about.kind, about.within].filter(Boolean).join(":");

  return {
    timeUnixNano: nanos(message.timestamp ?? Date.now()),
    severityText: "INFO",
    body: about.body,
    attributes: [
      ...session(ctx),
      ...repository,
      ...kept(
        text("agentic_memory.entry.id", identity),
        text("agentic_memory.actor", about.actor),
        text("agentic_memory.kind", about.kind),
      ),
      ...kept(...(about.extra ?? [])),
    ],
  };
}

function blocksOf(message: { content?: unknown }): { type: string; [k: string]: unknown }[] {
  const content = message.content;
  if (Array.isArray(content)) return content as { type: string }[];
  return [{ type: "text", text: String(content ?? "") }];
}

function joined(blocks: { type: string; [k: string]: unknown }[]): string {
  return blocks
    .filter((block) => block.type === "text")
    .map((block) => String(block.text ?? ""))
    .join("\n");
}

/** The value when it is a string, and undefined when it is not. */
function str(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

/** Turn one pi message into the records it deserves. */
function records(message: any, ctx: ExtensionContext): Record[] {
  const blocks = blocksOf(message);

  switch (message.role) {
    case "user":
      return [build(ctx, message, { kind: "prompt", actor: "human", body: joined(blocks) })];

    case "assistant": {
      const usage = message.usage ?? {};
      const records = [
        build(ctx, message, {
          kind: "response",
          actor: "agent",
          body: joined(blocks),
          extra: [
            text("gen_ai.operation.name", "chat"),
            text("gen_ai.provider.name", message.provider),
            text("gen_ai.request.model", message.model),
            text("gen_ai.response.model", message.responseModel ?? message.model),
            text("gen_ai.response.id", message.responseId),
            text("gen_ai.response.finish_reasons", message.stopReason),
            text("gen_ai.request.reasoning.level", message.providerThinkingLevel),
            whole("gen_ai.usage.input_tokens", usage.input),
            whole("gen_ai.usage.output_tokens", usage.output),
            whole("gen_ai.usage.cache_read.input_tokens", usage.cacheRead),
            whole("gen_ai.usage.cache_write.input_tokens", usage.cacheWrite),
            whole("gen_ai.usage.reasoning.output_tokens", usage.reasoning),
            real("agentic_memory.cost.total", usage.cost?.total),
            text("agentic_memory.content", JSON.stringify(blocks)),
          ],
        }),
      ];

      // Reasoning is its own record. It is the part other harnesses leave
      // out, and a later pass reads it apart from the answer.
      blocks
        .filter((block) => block.type === "thinking")
        .forEach((block, index) => {
          records.push(
            build(ctx, message, {
              kind: "thinking",
              actor: "agent",
              body: String(block.thinking ?? ""),
              within: String(index),
              extra: [
                text("gen_ai.operation.name", "chat"),
                text("gen_ai.request.model", message.model),
              ],
            }),
          );
        });

      // A tool call is its own record, so a call and its result join on the
      // call id. pi puts the call in the assistant message.
      blocks
        .filter((block) => block.type === "toolCall")
        .forEach((block) => {
          const id = str(block.id);
          records.push(
            build(ctx, message, {
              kind: "tool_call",
              actor: "agent",
              body: JSON.stringify(block.arguments ?? {}),
              within: id,
              extra: [
                text("gen_ai.operation.name", "execute_tool"),
                text("gen_ai.tool.name", str(block.name)),
                text("gen_ai.tool.call.id", id),
                text("gen_ai.tool.type", "function"),
              ],
            }),
          );
        });

      return records;
    }

    case "toolResult":
      return [
        build(ctx, message, {
          kind: "tool_result",
          actor: "agent",
          body: joined(blocks),
          extra: [
            text("gen_ai.operation.name", "execute_tool"),
            text("gen_ai.tool.name", message.toolName),
            text("gen_ai.tool.call.id", message.toolCallId),
            text("gen_ai.tool.type", "function"),
            text("agentic_memory.content", JSON.stringify(blocks)),
            message.isError ? text("error.type", "tool_error") : undefined,
          ],
        }),
      ];

    case "system":
      return [
        build(ctx, message, {
          kind: "system",
          actor: "system",
          body: JSON.stringify(message.sections ?? message.content ?? ""),
        }),
      ];

    default:
      return [];
  }
}

/** Read the repository once per session, so every record can name it. */
async function readRepository(pi: ExtensionAPI, cwd: string): Promise<void> {
  const run = async (args: string[]): Promise<string | undefined> => {
    try {
      const result = await pi.exec("git", ["-C", cwd, ...args], { timeout: 2_000 });
      return result.code === 0 ? result.stdout.trim() || undefined : undefined;
    } catch {
      return undefined;
    }
  };

  const remote = await run(["config", "--get", "remote.origin.url"]);
  const root = await run(["rev-parse", "--show-toplevel"]);
  const parts = remoteParts(remote);

  // The scope is derived from the directories rather than declared, and the
  // repository root is what the derivation starts from.
  scope = scopeOf(cwd, homedir(), root);

  repository = kept(
    text("vcs.repository.name", root === undefined ? parts.name : projectOf(root)),
    text("vcs.owner.name", parts.owner),
    text("vcs.repository.url.full", remote),
    text("vcs.ref.head.name", await run(["rev-parse", "--abbrev-ref", "HEAD"])),
    text("vcs.ref.head.revision", await run(["rev-parse", "HEAD"])),
  );
}

export default function (pi: ExtensionAPI) {
  if (!ENABLED) return;

  pi.on("session_start", async (_event, ctx) => {
    try {
      await readRepository(pi, ctx.sessionManager.getCwd() ?? ctx.cwd);
    } catch {
      repository = [];
    }
  });

  // The turn path. One ask, one block, and nothing here may throw or wait past
  // the deadline. The block is a message of its own after the prompt, so the
  // cached prefix of the conversation survives and only the block is new. It is
  // displayed, because the design says an injection is visible.
  pi.on("before_agent_start", async (event, ctx) => {
    if (!RECALL_ENABLED || !(RECALL_LIMIT > 0)) return;
    try {
      const statements = await recall(
        RECALL_ENDPOINT,
        {
          session_id: ctx.sessionManager.getSessionId(),
          harness: "pi",
          scope_key: scope?.key,
          prompt: event.prompt,
          limit: RECALL_LIMIT,
        },
        RECALL_DEADLINE_MS,
      );
      const content = block(statements);
      if (content === undefined) return;
      if (ctx.hasUI) {
        ctx.ui.notify(`agentic-memory: ${statements.length} statements recalled`, "info");
      }
      return {
        message: { customType: "agentic-memory", content, display: true },
      };
    } catch {
      // No memory this turn, and the turn proceeds.
      return;
    }
  });

  pi.on("message_end", async (event, ctx) => {
    try {
      for (const record of records(event.message, ctx)) enqueue(record);
    } catch {
      // Capture never breaks a turn.
    }
  });

  // The harness is going away, so this is the last chance to send.
  pi.on("session_shutdown", async () => {
    try {
      await flush();
    } catch {
      // Nothing left to do.
    }
  });
}
