from __future__ import annotations

from typing import Mapping

from domain.execution_ir import ExecutionIR, thaw_json
from ports.agent_preparation import RegistrySnapshotRepositoryPort
from ports.self_test import CanaryProbeResult, ProbeResult
from ports.workflow_engine import WorkflowReference


class SnapshotAgentBindingProbe:
    def __init__(self, snapshots: RegistrySnapshotRepositoryPort) -> None:
        self.snapshots = snapshots

    def probe(self, execution_ir: ExecutionIR) -> ProbeResult:
        snapshot = self.snapshots.get_registry_snapshot(execution_ir.registry_snapshot_id)
        expected = {item.requirement_id: item for item in snapshot.bindings}
        for node in execution_ir.nodes:
            binding = node.agent_binding
            if binding is None:
                continue
            frozen = expected.get(binding.requirement_id)
            if frozen is None or (
                binding.agent_id, binding.version, binding.endpoint, binding.credential_ref
            ) != (frozen.agent_id, frozen.version, frozen.endpoint, frozen.credential_ref):
                return ProbeResult(False, "BINDING_MISMATCH", rule_id="IR-BIND-002",
                                   node_id=node.node_id, agent_id=binding.agent_id,
                                   normalized_error_class="snapshot_binding")
        return ProbeResult(True, evidence={"snapshot_match": True, "bindings_checked": True})


class GatewayCanaryProbe:
    def __init__(self, gateway: object) -> None:
        self.gateway = gateway

    def run(self, workflow: WorkflowReference, execution_ir: ExecutionIR, *, operation_key: str) -> CanaryProbeResult:
        canary = getattr(self.gateway, "canary", None)
        if not callable(canary):
            return CanaryProbeResult(ProbeResult(False, "CANARY_UNAVAILABLE",
                normalized_error_class="gateway_contract"))
        response = canary(workflow.workflow_id, operation_key=operation_key)
        ok = bool(response.get("ok", response.get("passed", response.get("valid", False))))
        execution_id = response.get("execution_id", response.get("id"))
        evidence = {"execution_mode": "SELF_TEST", "response": response}
        if not ok or execution_id is None:
            return CanaryProbeResult(ProbeResult(False, "AGENT_INVOCATION_FAILED",
                normalized_error_class="canary", evidence=evidence))
        return CanaryProbeResult(ProbeResult(True, evidence=evidence), str(execution_id))


class CanaryArtifactMetadataProbe:
    """P0-07 metadata-only artifact seam; no MinIO operations."""

    def probe(self, canary: CanaryProbeResult, execution_ir: ExecutionIR) -> ProbeResult:
        evidence = thaw_json(canary.probe.evidence)
        response = evidence.get("response", {}) if isinstance(evidence, dict) else {}
        artifact = response.get("artifact", {}) if isinstance(response, Mapping) else {}
        ok = isinstance(artifact, Mapping) and bool(artifact.get("write_ok"))
        if not ok:
            return ProbeResult(False, "ARTIFACT_PROBE_FAILED",
                normalized_error_class="artifact_metadata")
        return ProbeResult(True, evidence={"write_ok": True, "metadata": dict(artifact)})


class GatewayObservabilityProbe:
    def __init__(self, gateway: object) -> None:
        self.gateway = gateway

    def probe(self, canary: CanaryProbeResult, execution_ir: ExecutionIR) -> ProbeResult:
        status = getattr(self.gateway, "status", None)
        if not callable(status) or canary.execution_id is None:
            return ProbeResult(False, "MONITORING_MISSING",
                normalized_error_class="gateway_contract")
        response = status(canary.execution_id)
        terminal = bool(response.get("terminal", False))
        completed = bool(response.get("completed", terminal))
        if not terminal:
            return ProbeResult(False, "TERMINAL_SIGNAL_MISSING",
                normalized_error_class="observability", evidence={"response": response})
        if not completed:
            return ProbeResult(False, "COMPLETION_FAILED",
                normalized_error_class="observability", evidence={"response": response})
        return ProbeResult(True, evidence={"monitoring_evidence_present": True,
            "terminal_signal_present": True, "completion_satisfied": True,
            "response": response})
