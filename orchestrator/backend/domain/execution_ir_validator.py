from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping

from domain.agent_preparation import RegistrySnapshot
from domain.execution_ir import ExecutionIR, canonical_json, thaw_json


@dataclass(frozen=True, order=True)
class IRValidationIssue:
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class IRValidationReport:
    issues: tuple[IRValidationIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues

    def by_code(self, code: str) -> tuple[IRValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.code == code)


class ExecutionIRValidationError(ValueError):
    def __init__(self, report: IRValidationReport) -> None:
        self.report = report
        summary = "; ".join(
            f"{item.code}@{item.path}: {item.message}" for item in report.issues
        )
        super().__init__(summary)


class ExecutionIRValidator:
    def validate(
        self,
        execution_ir: ExecutionIR,
        snapshot: RegistrySnapshot,
    ) -> IRValidationReport:
        issues = [
            *self._validate_graph(execution_ir),
            *self._validate_bindings(execution_ir, snapshot),
            *self._validate_schemas(execution_ir),
            *self._validate_artifacts(execution_ir),
            *self._validate_recovery(execution_ir),
            *self._validate_security(execution_ir),
        ]
        return IRValidationReport(tuple(sorted(set(issues))))

    def validate_or_raise(
        self,
        execution_ir: ExecutionIR,
        snapshot: RegistrySnapshot,
    ) -> IRValidationReport:
        report = self.validate(execution_ir, snapshot)
        if not report.ok:
            raise ExecutionIRValidationError(report)
        return report

    def _validate_graph(self, execution_ir: ExecutionIR) -> list[IRValidationIssue]:
        issues: list[IRValidationIssue] = []
        node_ids = [node.node_id for node in execution_ir.nodes]
        known = set(node_ids)
        if not node_ids:
            issues.append(self._issue("IR-DAG-001", "nodes", "at least one node is required"))
            return issues
        for node_id in sorted({item for item in node_ids if node_ids.count(item) > 1}):
            issues.append(self._issue("IR-DAG-001", f"nodes.{node_id}", "node_id must be unique"))

        edge_pairs = [(edge.source, edge.target) for edge in execution_ir.edges]
        for pair in sorted({item for item in edge_pairs if edge_pairs.count(item) > 1}):
            issues.append(
                self._issue(
                    "IR-DAG-001",
                    f"edges.{pair[0]}->{pair[1]}",
                    "edge must be unique",
                )
            )
        valid_edges: set[tuple[str, str]] = set()
        for source, target in edge_pairs:
            if source not in known or target not in known:
                issues.append(
                    self._issue(
                        "IR-DAG-001",
                        f"edges.{source}->{target}",
                        "edge references an unknown node",
                    )
                )
                continue
            if source == target:
                issues.append(
                    self._issue(
                        "IR-DAG-002",
                        f"edges.{source}->{target}",
                        "self-cycle is not allowed",
                    )
                )
                continue
            valid_edges.add((source, target))

        expected_edges: set[tuple[str, str]] = set()
        for node in execution_ir.nodes:
            if len(node.depends_on) != len(set(node.depends_on)):
                issues.append(
                    self._issue(
                        "IR-DAG-001",
                        f"nodes.{node.node_id}.depends_on",
                        "dependencies must be unique",
                    )
                )
            for dependency in node.depends_on:
                if dependency not in known:
                    issues.append(
                        self._issue(
                            "IR-DAG-001",
                            f"nodes.{node.node_id}.depends_on",
                            f"unknown dependency: {dependency}",
                        )
                    )
                else:
                    expected_edges.add((dependency, node.node_id))
        for source, target in sorted(valid_edges - expected_edges):
            issues.append(
                self._issue(
                    "IR-DAG-001",
                    f"edges.{source}->{target}",
                    "edge is not declared by target depends_on",
                )
            )
        for source, target in sorted(expected_edges - valid_edges):
            issues.append(
                self._issue(
                    "IR-DAG-001",
                    f"nodes.{target}.depends_on",
                    f"dependency has no edge: {source}->{target}",
                )
            )

        indegree = {node_id: 0 for node_id in known}
        adjacency = {node_id: [] for node_id in known}
        for source, target in valid_edges:
            adjacency[source].append(target)
            indegree[target] += 1
        ready = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
        visited = 0
        while ready:
            current = ready.pop(0)
            visited += 1
            for target in sorted(adjacency[current]):
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
                    ready.sort()
        if visited != len(known):
            issues.append(self._issue("IR-DAG-002", "edges", "Execution IR must be acyclic"))
        return issues

    def _validate_bindings(
        self,
        execution_ir: ExecutionIR,
        snapshot: RegistrySnapshot,
    ) -> list[IRValidationIssue]:
        issues: list[IRValidationIssue] = []
        if snapshot.snapshot_id != execution_ir.registry_snapshot_id:
            issues.append(
                self._issue(
                    "IR-BIND-002",
                    "registry_snapshot_id",
                    "IR does not reference the supplied Registry Snapshot",
                )
            )
        if snapshot.workflow_family_id != execution_ir.workflow_family_id:
            issues.append(
                self._issue(
                    "IR-BIND-002",
                    "workflow_family_id",
                    "Registry Snapshot belongs to a different Workflow Family",
                )
            )
        snapshot_bindings = {item.requirement_id: item for item in snapshot.bindings}
        for node in execution_ir.nodes:
            path = f"nodes.{node.node_id}.agent_binding"
            binding = node.agent_binding
            if node.node_type == "invoke_agent" and binding is None:
                issues.append(
                    self._issue("IR-BIND-001", path, "invoke_agent requires an Agent Binding")
                )
                continue
            if binding is None:
                continue
            expected = snapshot_bindings.get(binding.requirement_id)
            if expected is None:
                issues.append(
                    self._issue(
                        "IR-BIND-001",
                        path,
                        "binding requirement does not exist in Registry Snapshot",
                    )
                )
                continue
            observed = (
                binding.registry_snapshot_id,
                binding.agent_id,
                binding.version,
                binding.endpoint,
                binding.capability,
                canonical_json(binding.input_schema),
                canonical_json(binding.output_schema),
                binding.credential_ref,
            )
            required = (
                snapshot.snapshot_id,
                expected.agent_id,
                expected.version,
                expected.endpoint,
                expected.capability,
                canonical_json(expected.input_schema),
                canonical_json(expected.output_schema),
                expected.credential_ref,
            )
            if observed != required:
                issues.append(
                    self._issue(
                        "IR-BIND-002",
                        path,
                        "binding differs from the frozen Registry Snapshot",
                    )
                )
        return issues

    def _validate_schemas(self, execution_ir: ExecutionIR) -> list[IRValidationIssue]:
        issues: list[IRValidationIssue] = []
        nodes = {node.node_id: node for node in execution_ir.nodes}
        for node in execution_ir.nodes:
            for contract_name, schema in (
                ("input_contract", node.input_contract.schema),
                ("output_contract", node.output_contract.schema),
            ):
                if not self._schema_valid(schema):
                    issues.append(
                        self._issue(
                            "IR-SCHEMA-001",
                            f"nodes.{node.node_id}.{contract_name}.schema",
                            "contract is not a supported JSON Schema object",
                        )
                    )
            if node.agent_binding is not None:
                if canonical_json(node.input_contract.schema) != canonical_json(
                    node.agent_binding.input_schema
                ):
                    issues.append(
                        self._issue(
                            "IR-SCHEMA-001",
                            f"nodes.{node.node_id}.input_contract",
                            "node input contract differs from its Agent Binding",
                        )
                    )
                if canonical_json(node.output_contract.schema) != canonical_json(
                    node.agent_binding.output_schema
                ):
                    issues.append(
                        self._issue(
                            "IR-SCHEMA-001",
                            f"nodes.{node.node_id}.output_contract",
                            "node output contract differs from its Agent Binding",
                        )
                    )
        for edge in execution_ir.edges:
            source = nodes.get(edge.source)
            target = nodes.get(edge.target)
            if source is None or target is None:
                continue
            if not self._schema_compatible(
                source.output_contract.schema,
                target.input_contract.schema,
            ):
                issues.append(
                    self._issue(
                        "IR-SCHEMA-001",
                        f"edges.{edge.source}->{edge.target}",
                        "upstream output schema is incompatible with downstream input schema",
                    )
                )
        return issues

    def _validate_artifacts(self, execution_ir: ExecutionIR) -> list[IRValidationIssue]:
        issues: list[IRValidationIssue] = []
        node_ids = {node.node_id for node in execution_ir.nodes}
        artifact_ids = [item.artifact_id for item in execution_ir.artifacts]
        known_artifacts = set(artifact_ids)
        for artifact_id in sorted(
            {item for item in artifact_ids if artifact_ids.count(item) > 1}
        ):
            issues.append(
                self._issue(
                    "IR-ART-001",
                    f"artifacts.{artifact_id}",
                    "artifact_id must be unique",
                )
            )
        for artifact in execution_ir.artifacts:
            path = f"artifacts.{artifact.artifact_id}"
            if artifact.producer_node_id is not None and artifact.producer_node_id not in node_ids:
                issues.append(
                    self._issue("IR-ART-001", path, "producer node does not exist")
                )
            if artifact.reusable and (
                artifact.producer_node_id is None
                or artifact.content_hash is None
                or not artifact.schema
                or artifact.input_fingerprint is None
                or artifact.permission_scope is None
            ):
                issues.append(
                    self._issue(
                        "IR-ART-001",
                        path,
                        "reusable artifact requires producer, hash, schema, input fingerprint, and permission scope",
                    )
                )
            if artifact.uri is not None and artifact.uri.lower().startswith(
                ("data:", "inline:")
            ):
                issues.append(
                    self._issue(
                        "IR-ART-001",
                        path,
                        "artifact body must not be embedded in Execution IR",
                    )
                )
        for node in execution_ir.nodes:
            policy = node.artifact_policy
            for artifact_id in (*policy.produces, *policy.consumes):
                if artifact_id not in known_artifacts:
                    issues.append(
                        self._issue(
                            "IR-ART-001",
                            f"nodes.{node.node_id}.artifact_policy",
                            f"unknown artifact: {artifact_id}",
                        )
                    )
            for artifact_id in policy.produces:
                declarations = [
                    item
                    for item in execution_ir.artifacts
                    if item.artifact_id == artifact_id
                ]
                if declarations and declarations[0].producer_node_id != node.node_id:
                    issues.append(
                        self._issue(
                            "IR-ART-001",
                            f"nodes.{node.node_id}.artifact_policy",
                            f"artifact producer mismatch: {artifact_id}",
                        )
                    )
        return issues

    def _validate_recovery(self, execution_ir: ExecutionIR) -> list[IRValidationIssue]:
        issues: list[IRValidationIssue] = []
        known_nodes = {node.node_id for node in execution_ir.nodes}
        known_artifacts = {item.artifact_id for item in execution_ir.artifacts}
        checkpoint_ids = [item.checkpoint_id for item in execution_ir.checkpoints]
        for checkpoint_id in sorted(
            {item for item in checkpoint_ids if checkpoint_ids.count(item) > 1}
        ):
            issues.append(
                self._issue(
                    "IR-REC-001",
                    f"checkpoints.{checkpoint_id}",
                    "checkpoint_id must be unique",
                )
            )
        checkpoint_nodes: set[str] = set()
        for checkpoint in execution_ir.checkpoints:
            path = f"checkpoints.{checkpoint.checkpoint_id}"
            checkpoint_nodes.add(checkpoint.node_id)
            if checkpoint.node_id not in known_nodes:
                issues.append(self._issue("IR-REC-001", path, "checkpoint node does not exist"))
            for artifact_id in checkpoint.artifact_ids:
                if artifact_id not in known_artifacts:
                    issues.append(
                        self._issue(
                            "IR-REC-001",
                            path,
                            f"checkpoint references unknown artifact: {artifact_id}",
                        )
                    )
        for node in execution_ir.nodes:
            policy = node.checkpoint_policy
            if policy is None:
                continue
            for artifact_id in policy.required_artifacts:
                if artifact_id not in known_artifacts:
                    issues.append(
                        self._issue(
                            "IR-REC-001",
                            f"nodes.{node.node_id}.checkpoint_policy",
                            f"unknown required artifact: {artifact_id}",
                        )
                    )
            if policy.enabled and node.node_id not in checkpoint_nodes:
                issues.append(
                    self._issue(
                        "IR-REC-001",
                        f"nodes.{node.node_id}.checkpoint_policy",
                        "enabled checkpoint policy has no checkpoint declaration",
                    )
                )

        recovery = execution_ir.recovery
        referenced = set(recovery.reused_nodes) | set(recovery.invalidated_nodes)
        if recovery.resume_root is not None:
            referenced.add(recovery.resume_root)
        for node_id in sorted(referenced - known_nodes):
            issues.append(
                self._issue(
                    "IR-REC-001",
                    "recovery",
                    f"recovery references unknown node: {node_id}",
                )
            )
        reused = set(recovery.reused_nodes)
        invalidated = set(recovery.invalidated_nodes)
        if reused & invalidated:
            issues.append(
                self._issue(
                    "IR-REC-001",
                    "recovery",
                    "reused_nodes and invalidated_nodes must be disjoint",
                )
            )
        if invalidated and recovery.resume_root is None:
            issues.append(
                self._issue(
                    "IR-REC-001",
                    "recovery.resume_root",
                    "invalidated nodes require a resume_root",
                )
            )
        if not invalidated and recovery.resume_root is not None:
            issues.append(
                self._issue(
                    "IR-REC-001",
                    "recovery.resume_root",
                    "resume_root requires invalidated nodes",
                )
            )
        root = recovery.resume_root
        if root in known_nodes and invalidated <= known_nodes:
            if root not in invalidated:
                issues.append(
                    self._issue(
                        "IR-REC-001",
                        "recovery.resume_root",
                        "resume_root must be included in invalidated_nodes",
                    )
                )
            for node_id in sorted(invalidated):
                if node_id != root and not self._reaches(execution_ir, root, node_id):
                    issues.append(
                        self._issue(
                            "IR-REC-001",
                            "recovery.resume_root",
                            f"resume_root does not cover invalidated node: {node_id}",
                        )
                    )
            for node_id in sorted(reused):
                if node_id == root or not self._reaches(execution_ir, node_id, root):
                    issues.append(
                        self._issue(
                            "IR-REC-001",
                            "recovery.reused_nodes",
                            f"reused node is not a predecessor of resume_root: {node_id}",
                        )
                    )
        return issues

    def _validate_security(self, execution_ir: ExecutionIR) -> list[IRValidationIssue]:
        issues: list[IRValidationIssue] = []
        for path, message in self._plaintext_secret_findings(execution_ir.as_dict()):
            issues.append(self._issue("IR-SEC-001", path, message))
        return issues

    @staticmethod
    def _issue(code: str, path: str, message: str) -> IRValidationIssue:
        return IRValidationIssue(code, path, message)

    @staticmethod
    def _schema_valid(schema: Mapping[str, object]) -> bool:
        try:
            document = thaw_json(schema)
            canonical_json(document)
        except (TypeError, ValueError):
            return False
        if not isinstance(document, dict):
            return False
        schema_type = document.get("type")
        if schema_type is not None and schema_type not in {
            "object",
            "array",
            "string",
            "number",
            "integer",
            "boolean",
            "null",
        }:
            return False
        if schema_type == "object":
            properties = document.get("properties", {})
            required = document.get("required", [])
            if not isinstance(properties, dict) or not isinstance(required, list):
                return False
            if any(not isinstance(item, str) or item not in properties for item in required):
                return False
        return True

    @classmethod
    def _schema_compatible(
        cls,
        producer: Mapping[str, object],
        consumer: Mapping[str, object],
    ) -> bool:
        produced = thaw_json(producer)
        consumed = thaw_json(consumer)
        if not isinstance(produced, dict) or not isinstance(consumed, dict):
            return False
        if not consumed:
            return True
        if not produced:
            return False
        producer_type = produced.get("type")
        consumer_type = consumed.get("type")
        if producer_type is not None and consumer_type is not None and producer_type != consumer_type:
            return False
        if consumer_type == "object":
            producer_properties = produced.get("properties", {})
            consumer_properties = consumed.get("properties", {})
            producer_required = set(produced.get("required", []))
            consumer_required = set(consumed.get("required", []))
            if not isinstance(producer_properties, dict) or not isinstance(consumer_properties, dict):
                return False
            if not consumer_required <= producer_required:
                return False
            for name in consumer_required:
                producer_property = producer_properties.get(name)
                consumer_property = consumer_properties.get(name)
                if not isinstance(producer_property, dict) or not isinstance(consumer_property, dict):
                    return False
                if not cls._schema_compatible(producer_property, consumer_property):
                    return False
        if consumer_type == "array" and isinstance(consumed.get("items"), dict):
            if not isinstance(produced.get("items"), dict):
                return False
            if not cls._schema_compatible(produced["items"], consumed["items"]):
                return False
        producer_enum = produced.get("enum")
        consumer_enum = consumed.get("enum")
        if isinstance(producer_enum, list) and isinstance(consumer_enum, list):
            produced_values = {canonical_json(item) for item in producer_enum}
            consumed_values = {canonical_json(item) for item in consumer_enum}
            if not produced_values <= consumed_values:
                return False
        return True

    @staticmethod
    def _reaches(execution_ir: ExecutionIR, source: str, target: str) -> bool:
        adjacency: dict[str, list[str]] = {}
        for edge in execution_ir.edges:
            adjacency.setdefault(edge.source, []).append(edge.target)
        pending = [source]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            if current == target and current != source:
                return True
            pending.extend(adjacency.get(current, ()))
        return False

    @staticmethod
    def _plaintext_secret_findings(value: object) -> list[tuple[str, str]]:
        forbidden_keys = {
            "secret",
            "password",
            "api_key",
            "access_key",
            "private_key",
            "credential",
            "authorization",
            "auth_token",
        }
        secret_value = re.compile(
            r"(?i)(bearer\s+[a-z0-9._-]+|(?<![a-z0-9])sk-[a-z0-9_-]{8,}|"
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----|[?&](token|api_key|secret|password)=)"
        )
        findings: list[tuple[str, str]] = []

        def visit(item: object, path: str) -> None:
            if isinstance(item, Mapping):
                for key, nested in item.items():
                    normalized = str(key).strip().lower().replace("-", "_")
                    child_path = f"{path}.{key}" if path else str(key)
                    if (
                        normalized in forbidden_keys
                        and not normalized.endswith("_ref")
                        and not isinstance(nested, Mapping)
                    ):
                        findings.append((child_path, "plaintext credential field is forbidden"))
                    visit(nested, child_path)
            elif isinstance(item, (list, tuple)):
                for index, nested in enumerate(item):
                    visit(nested, f"{path}[{index}]")
            elif isinstance(item, str) and secret_value.search(item):
                findings.append((path, "value appears to contain plaintext credential material"))

        visit(value, "ir")
        return findings
