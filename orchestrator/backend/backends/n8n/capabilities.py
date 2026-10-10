from __future__ import annotations

N8N_V2_NODE_KINDS = frozenset({
    "invoke", "evaluate", "decision", "parallel", "join", "bounded_loop", "wait", "end",
})


def compiler_capabilities() -> set[str]:
    return set(N8N_V2_NODE_KINDS) | {"gateway_authorize", "runtime_event", "automatic_start"}

