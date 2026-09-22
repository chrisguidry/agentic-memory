# Deploying it

The base in this directory runs the api and the worker. It names the
`agentic-memory` namespace, and it creates no namespace and no Secret, so a
consumer supplies both. Three components are optional: monitoring, an
in-cluster Postgres, and an in-cluster Redis.

## The Secret

The api and the worker read one Secret named `agentic-memory`, whose keys are
the environment variable names. Nothing here creates it.

| Key | What it holds |
|---|---|
| `AGENTIC_MEMORY_DATABASE_URL` | `postgresql://user:password@host:5432/database` |
| `AGENTIC_MEMORY_REDIS_URL` | `redis://host:6379/0` |
| `TYPESAFE_API_KEY` | the classifier's provider key |
| `DEEPINFRA_API_KEY` | the writer's provider key |

```bash
kubectl create namespace agentic-memory
kubectl -n agentic-memory create secret generic agentic-memory \
  --from-literal=AGENTIC_MEMORY_DATABASE_URL=... \
  --from-literal=AGENTIC_MEMORY_REDIS_URL=... \
  --from-literal=TYPESAFE_API_KEY=... \
  --from-literal=DEEPINFRA_API_KEY=...
```

## Pointing an overlay at this base

A consumer names a full commit, not a branch, so a deploy is the same
tomorrow as it was today. The image tag is the other thing to pin.

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: agentic-memory
resources:
  - https://github.com/chrisguidry/agentic-memory//deploy?ref=<full-sha>
components:
  - https://github.com/chrisguidry/agentic-memory//deploy/monitoring?ref=<full-sha>
  - https://github.com/chrisguidry/agentic-memory//deploy/postgres?ref=<full-sha>
  - https://github.com/chrisguidry/agentic-memory//deploy/redis?ref=<full-sha>
images:
  - name: ghcr.io/chrisguidry/agentic-memory
    newTag: 2026.09.21-000-dev-039-a7f94c07
```

## The components

**monitoring** adds a PodMonitor over `/metrics` and a Grafana dashboard. It
needs the Prometheus Operator, and a Grafana whose dashboard sidecar looks for
ConfigMaps labeled `grafana_dashboard`. The dashboard lands in the app's
namespace beside the PodMonitor, so a Grafana restricted to one namespace
needs the ConfigMap patched into that one. Add it only where that operator
exists.

**postgres** runs the pgvector build of Postgres with its own volume and a
Service named `postgres`. The statement embeddings need the `vector`
extension, so a stock Postgres will not do. The password is a placeholder;
override it and point `AGENTIC_MEMORY_DATABASE_URL` at the Service.

**redis** runs Redis with append-only persistence and a Service named `redis`,
for the docket queue. Point `AGENTIC_MEMORY_REDIS_URL` at it.

A consumer with a Postgres and a Redis already leaves both off and brings its
own, which is what the homelab does.
