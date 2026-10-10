from __future__ import annotations

import hashlib
import json

from contracts.execution_ir_v2 import ExecutionIRV2


def canonical_ir_bytes(execution_ir: ExecutionIRV2) -> bytes:
    payload = execution_ir.model_dump(mode="json")
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("ir_hash", None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def with_ir_hash(execution_ir: ExecutionIRV2) -> ExecutionIRV2:
    ir_hash = hashlib.sha256(canonical_ir_bytes(execution_ir)).hexdigest()
    metadata = execution_ir.metadata.model_copy(update={"ir_hash": ir_hash})
    return execution_ir.model_copy(update={"metadata": metadata})
