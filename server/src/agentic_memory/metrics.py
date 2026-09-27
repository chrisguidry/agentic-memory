"""The numbers Prometheus scrapes from the service.

Two sources make up a scrape. The counts of what the store holds are read from
the store at scrape time, because the table is the truth and a count kept in
the process restarts at zero. The turn path's latency and outcomes are counted
in the process, because a recall leaves no row behind when it hands nothing,
fails, or has lost its client, and a histogram of a latency cannot be read
back from a table.

The process counters have a registry of their own, so a scrape holds only
these, without the collectors the client library registers by default.
"""

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

REGISTRY = CollectorRegistry()

# The bastion waits 500 ms, and the plan for the match asks the service to
# answer inside 150 ms, so the buckets are fine below both and coarse above.
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.25, 0.5, 1.0, 2.5)

PHASES = Histogram(
    "agentic_memory_recall_seconds",
    "How long each phase of a recall took: embedding the prompt, each query, and"
    " the whole request.",
    ["phase"],
    buckets=BUCKETS,
    registry=REGISTRY,
)

# `gone` is a recall whose client stopped waiting before its statements were
# recorded, so nothing was recorded for it.
RECALLS = Counter(
    "agentic_memory_recalls",
    "Recalls by the form the turn path took and what came of it.",
    ["form", "outcome"],
    registry=REGISTRY,
)

# A probe is a recall that records nothing, asked to judge the turn path. It has
# a counter of its own, so the live counts and the dashboard that sums them hold
# only what a person's sessions asked.
PROBES = Counter(
    "agentic_memory_probe_recalls",
    "Probe recalls by the form the turn path took and what came of it.",
    ["form", "outcome"],
    registry=REGISTRY,
)

# The bastion counts the recalls that missed its deadline and sends the count
# with its next recall, because Prometheus does not scrape a laptop.
MISSES = Counter(
    "agentic_memory_recall_deadline_misses",
    "Recalls a client stopped waiting for, as the client counted them.",
    registry=REGISTRY,
)


def phase(name: str):
    """Time one phase of a recall into its histogram."""
    return PHASES.labels(phase=name).time()


def render(gauges: dict[str, int], calls: list[tuple[str, int]]) -> str:
    """The whole scrape in Prometheus's text format.

    The store's numbers are written out by hand, because they are read fresh
    each scrape, and the client library has no object that holds them.
    """
    lines: list[str] = []
    for name, value in gauges.items():
        lines.append(f"# TYPE agentic_memory_{name} gauge")
        lines.append(f"agentic_memory_{name} {value}")
    lines.append("# TYPE agentic_memory_model_calls counter")
    for task, count in calls:
        lines.append(f'agentic_memory_model_calls{{task="{task}"}} {count}')
    return "\n".join(lines) + "\n" + generate_latest(REGISTRY).decode()
