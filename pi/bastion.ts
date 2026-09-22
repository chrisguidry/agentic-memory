/**
 * Talks to the bastion over its unix socket.
 *
 * The bastion holds the service's address and authorization, so nothing
 * here sends either. The socket itself is the credential, and the kernel
 * decides who may open it. A path nobody is listening on behaves like a
 * collector nobody started: this module answers `undefined`, and every
 * caller already treats that as ordinary.
 */

import { request } from "node:http";
import { userInfo } from "node:os";

const SOCKET_NAME = "agentic-memory.sock";

function defaultSocketPath(): string {
  const runtimeDir = process.env.XDG_RUNTIME_DIR;
  if (runtimeDir) return `${runtimeDir}/${SOCKET_NAME}`;
  return `/run/user/${userInfo().uid}/${SOCKET_NAME}`;
}

const SOCKET_PATH = process.env.AGENTIC_MEMORY_SOCKET ?? defaultSocketPath();

export interface Response {
  status: number;
  body: string;
}

/**
 * POST a JSON body to the bastion at `path`, and give back what it answered.
 *
 * `undefined` covers every way the bastion can fail to answer in time: no
 * socket, a refused connection, or an answer slower than `timeoutMs`. A
 * caller never learns which, because the deadline is the only thing it acts
 * on; global `fetch` cannot dial a unix socket without a custom dispatcher,
 * so this uses `node:http` directly.
 */
export function post(
  path: string,
  body: unknown,
  timeoutMs: number,
): Promise<Response | undefined> {
  return new Promise((resolve) => {
    const payload = JSON.stringify(body);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);

    const req = request(
      {
        socketPath: SOCKET_PATH,
        path,
        method: "POST",
        headers: {
          "content-type": "application/json",
          "content-length": Buffer.byteLength(payload),
        },
        signal: controller.signal,
      },
      (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (chunk: Buffer) => chunks.push(chunk));
        res.on("end", () => {
          clearTimeout(timer);
          resolve({ status: res.statusCode ?? 0, body: Buffer.concat(chunks).toString("utf8") });
        });
        res.on("error", () => {
          clearTimeout(timer);
          resolve(undefined);
        });
      },
    );
    req.on("error", () => {
      clearTimeout(timer);
      resolve(undefined);
    });
    req.end(payload);
  });
}
