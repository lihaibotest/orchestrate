# Planning Agent test substitute

The container accepts bounded ordinary JSON envelopes, persists the last request atomically at `PLANNING_AGENT_STATE_FILE`, and exposes `/health` and `/last`. Evaluator-owned fields are rejected; evaluator opinion remains on its direct channel.
