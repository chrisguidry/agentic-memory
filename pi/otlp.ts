/**
 * Encoding records as OTLP log records.
 *
 * The attribute names are listed in `docs/otel-conventions.md`. This module
 * only builds the payload; `index.ts` defines what a record is.
 */

import { readFileSync } from "node:fs";
import { arch, release, type as osType } from "node:os";
import { basename } from "node:path";

export const VERSION = "0.0.1";
export const SCOPE = "agentic-memory.pi";

/** An OTLP attribute value is a one-key object whose key names the type. */
export type Value =
  | { stringValue: string }
  | { intValue: string }
  | { doubleValue: number }
  | { boolValue: boolean };

export interface Attribute {
  key: string;
  value: Value;
}

export function text(key: string, value: string | undefined): Attribute | undefined {
  return value === undefined ? undefined : { key, value: { stringValue: value } };
}

export function whole(key: string, value: number | undefined): Attribute | undefined {
  return value === undefined ? undefined : { key, value: { intValue: String(Math.trunc(value)) } };
}

export function real(key: string, value: number | undefined): Attribute | undefined {
  return value === undefined ? undefined : { key, value: { doubleValue: value } };
}

/** Drop the attributes a caller could not fill. */
export function kept(...attributes: (Attribute | undefined)[]): Attribute[] {
  return attributes.filter((attribute): attribute is Attribute => attribute !== undefined);
}

function machineId(): string | undefined {
  for (const path of ["/etc/machine-id", "/var/lib/dbus/machine-id"]) {
    try {
      return readFileSync(path, "utf8").trim();
    } catch {
      // The next path may exist.
    }
  }
  return undefined;
}

/**
 * What describes the machine, and is therefore the same on every record.
 *
 * Every name here is an OpenTelemetry resource attribute, so a backend that
 * understands them places this client without being told what it is.
 */
export function resource(machine: string): { attributes: Attribute[] } {
  return {
    attributes: kept(
      text("service.name", "agentic-memory"),
      text("service.version", VERSION),
      text("service.instance.id", machine),
      text("host.name", machine),
      text("host.id", machineId()),
      text("host.arch", arch()),
      text("os.type", osType().toLowerCase()),
      text("os.version", release()),
      text("telemetry.sdk.name", "agentic-memory"),
      text("telemetry.sdk.language", "typescript"),
      text("telemetry.sdk.version", VERSION),
    ),
  };
}

export interface Record {
  /** Nanoseconds since the epoch, as OTLP encodes it. */
  timeUnixNano: string;
  severityText: string;
  body: string;
  attributes: Attribute[];
}

/**
 * Wrap the batch in the three envelopes OTLP expects.
 *
 * A log record's body is an AnyValue, not a bare string, so the text is
 * wrapped in a one-key object the same way an attribute value is. A body
 * sent as a plain string decodes to nothing on the other side.
 */
export function payload(
  machine: string,
  records: Record[],
): { resourceLogs: unknown[] } {
  return {
    resourceLogs: [
      {
        resource: resource(machine),
        scopeLogs: [
          {
            scope: { name: SCOPE, version: VERSION },
            logRecords: records.map((record) => ({
              timeUnixNano: record.timeUnixNano,
              severityText: record.severityText,
              body: { stringValue: record.body },
              attributes: record.attributes,
            })),
          },
        ],
      },
    ],
  };
}

/**
 * The session id inside a session file path.
 *
 * pi reports a parent as the path of the session file it forked from. The
 * convention names the previous session with `session.id`, so the id is taken
 * out of the path rather than the path being sent as an identifier.
 */
export function sessionIdIn(path: string | undefined): string | undefined {
  if (path === undefined) return undefined;
  const name = (path.split("/").pop() ?? path).replace(/\.jsonl$/, "");
  const underscore = name.lastIndexOf("_");
  return underscore === -1 ? name : name.slice(underscore + 1);
}

/**
 * The owner and the repository name inside a git remote URL.
 *
 * Both shapes appear: `git@host:owner/name.git` and `https://host/owner/name`.
 * A URL with no owner gives the name alone.
 */
export function remoteParts(url: string | undefined): {
  owner?: string;
  name?: string;
} {
  if (url === undefined) return {};
  let path = url;
  if (url.includes("://")) path = url.split("://")[1];
  else if (url.includes("@") && url.includes(":")) path = url.split(":")[1];

  const segments = path.split("/").filter(Boolean);
  if (segments.length === 0) return {};

  return {
    owner: segments.length >= 2 ? segments[segments.length - 2] : undefined,
    name: segments[segments.length - 1].replace(/\.git$/, ""),
  };
}

/** The project a working directory belongs to, for grouping records. */
export function projectOf(cwd: string | undefined): string | undefined {
  return cwd ? basename(cwd) : undefined;
}

/** Milliseconds since the epoch, as OTLP encodes them. */
export function nanos(milliseconds: number): string {
  return String(Math.trunc(milliseconds) * 1_000_000);
}
