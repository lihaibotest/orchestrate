from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from application.state_transition_service import StateTransitionService
from application.agent_preparation_service import AgentPreparationService
from application.execution_ir_service import ExecutionIRValidationService
from application.workflow_compilation_service import WorkflowCompilationService
from application.self_test_controller import SelfTestController, SelfTestRepairService
from application.self_test_stages import (
    S0ValidationStage,
    S1BindingStage,
    S2EngineStage,
    S3CanaryArtifactStage,
    S4ObservabilityStage,
)
from application.workflow_release_service import WorkflowReleaseService
from application.execution_controller import ExecutionController
from application.runtime_event_service import ReconciliationService, RuntimeEventService
from application.planning_blueprint_ingestion_service import PlanningBlueprintIngestionService
from application.production_planning_blueprint_generation import ProductionPlanningBlueprintGenerationService
from adapters.health import RepositoryHealthAdapter
from adapters.postgres import PostgresControlPlaneRepository
from adapters.postgres_self_test import PostgresSelfTestRepository
from adapters.postgres_execution import PostgresExecutionRepository
from adapters.postgres_planning_blueprint import PostgresPlanningBlueprintRepository
from adapters.postgres_artifact import PostgresArtifactRepository
from adapters.minio_artifact import MinioArtifactAdapter
from adapters.production_agent_runtime import GatewayAgentRegistryRuntimeAdapter
from adapters.production_self_test import (
    CanaryArtifactMetadataProbe,
    GatewayCanaryProbe,
    GatewayObservabilityProbe,
    SnapshotAgentBindingProbe,
)
from backends.n8n.backend import N8nExecutionBackend
from backends.n8n.workflow_compiler import N8nCompilerConfig, N8nWorkflowCompilerAdapter
from backends.n8n.workflow_engine import N8nWorkflowEngineAdapter
from capabilities.registry import EndpointPolicy
from capabilities.ranking import RankingWeights
from domain.health import ComponentHealth
from domain.self_test import SelfTestPolicy
from history.orchestration_repository import OrchestrationRepository
from infrastructure.config import OrchestratorSettings
from infrastructure.observability import PrometheusMetrics, configure_otel
from planning.reasoner_client import ReasonerClient
from ports.external_control import ExternalControlPort
from ports.agent_preparation import AgentRegistryPort, AgentRuntimePort
from ports.observability import MetricsPort, TracerPort
from ports.workflow_engine import WorkflowEnginePort
from ports.artifact import ArtifactManagerPort
from ports.self_test import (
    AgentBindingProbePort,
    ArtifactProbePort,
    CanaryProbePort,
    ObservabilityProbePort,
    WorkflowRepairPort,
)
from services.durable_publisher import DurablePublisher
from services.external_adapters import ExternalControlPlane
from services.outbox_dispatcher import OutboxDispatcher
from services.planning_agent_delivery import planning_agent_outbox_handlers
from services.planning_job_runner import PlanningJobRunner
from services.planning_job_worker import PlanningJobWorker
from services.workflow_publisher import DurableWorkflowPublisherFacade


ExternalControlFactory = Callable[[], ExternalControlPort]


@dataclass
class ApplicationContainer:
    """The application composition root and owner of process-scoped resources."""

    settings: OrchestratorSettings
    repository: OrchestrationRepository
    metrics: MetricsPort
    tracer: TracerPort
    external_control_factory: ExternalControlFactory | None = None
    _external_control: ExternalControlPort | None = field(default=None, init=False, repr=False)
    _external_control_resolved: bool = field(default=False, init=False, repr=False)
    _publisher: object | None = field(default=None, init=False, repr=False)
    _planning_worker: PlanningJobWorker | None = field(default=None, init=False, repr=False)
    _control_plane_repository: PostgresControlPlaneRepository | None = field(default=None, init=False, repr=False)
    _state_transition_service: StateTransitionService | None = field(default=None, init=False, repr=False)
    _execution_ir_validation_service: ExecutionIRValidationService | None = field(
        default=None, init=False, repr=False
    )
    _workflow_compilation_service: WorkflowCompilationService | None = field(
        default=None, init=False, repr=False
    )
    _workflow_engine: WorkflowEnginePort | None = field(
        default=None, init=False, repr=False
    )
    _self_test_repository: PostgresSelfTestRepository | None = field(
        default=None, init=False, repr=False
    )
    _planning_blueprint_service: PlanningBlueprintIngestionService | None = field(
        default=None, init=False, repr=False
    )
    _planning_blueprint_repository: PostgresPlanningBlueprintRepository | None = field(
        default=None, init=False, repr=False
    )
    _artifact_manager: ArtifactManagerPort | None = field(
        default=None, init=False, repr=False
    )
    _artifact_manager_resolved: bool = field(default=False, init=False, repr=False)
    _runtime_event_service: RuntimeEventService | None = field(default=None, init=False, repr=False)

    def external_control(self) -> ExternalControlPort | None:
        if self._external_control_resolved:
            return self._external_control
        self._external_control_resolved = True
        if self.external_control_factory is not None:
            self._external_control = self.external_control_factory()
        elif self.settings.external_control_configured:
            self._external_control = ExternalControlPlane(self.settings)
        return self._external_control

    def control_plane_repository(self) -> PostgresControlPlaneRepository | None:
        """Return PostgreSQL control-plane persistence when configured."""
        if not self.settings.postgres_dsn:
            return None
        if self._control_plane_repository is None:
            self._control_plane_repository = PostgresArtifactRepository(
                self.settings.postgres_dsn,
                schema=self.settings.postgres_schema,
            )
        return self._control_plane_repository

    def state_transition_service(self) -> StateTransitionService | None:
        repository = self.control_plane_repository()
        if repository is None:
            return None
        if self._state_transition_service is None:
            self._state_transition_service = StateTransitionService(repository)
        return self._state_transition_service

    def agent_preparation_service(
        self,
        registry: AgentRegistryPort,
        runtime: AgentRuntimePort,
    ) -> AgentPreparationService | None:
        repository = self.control_plane_repository()
        if repository is None:
            return None
        return AgentPreparationService(registry, runtime, repository)

    def execution_ir_validation_service(self) -> ExecutionIRValidationService | None:
        repository = self.control_plane_repository()
        if repository is None:
            return None
        if self._execution_ir_validation_service is None:
            self._execution_ir_validation_service = ExecutionIRValidationService(repository)
        return self._execution_ir_validation_service

    def workflow_compilation_service(self) -> WorkflowCompilationService | None:
        validation = self.execution_ir_validation_service()
        repository = self.control_plane_repository()
        if validation is None or not isinstance(repository, PostgresExecutionRepository):
            return None
        if self._workflow_compilation_service is None:
            compiler = N8nWorkflowCompilerAdapter(
                N8nCompilerConfig(
                    webhook_credential_ref=self.settings.n8n_webhook_credential_ref,
                    monitoring_endpoint=self.settings.monitoring_endpoint,
                    task_orchestration_endpoint=self.settings.task_orchestration_endpoint,
                )
            )
            self._workflow_compilation_service = WorkflowCompilationService(
                validation,
                compiler,
                repository,
            )
        return self._workflow_compilation_service

    def workflow_engine(self) -> WorkflowEnginePort | None:
        control = self.external_control()
        if control is None:
            return None
        if self._workflow_engine is None:
            self._workflow_engine = N8nWorkflowEngineAdapter(control.gateway)
        return self._workflow_engine

    def self_test_repository(self) -> PostgresSelfTestRepository | None:
        """Return the PostgreSQL authority used by Self-Test and release gates."""
        if not self.settings.postgres_dsn:
            return None
        if self._self_test_repository is None:
            self._self_test_repository = PostgresArtifactRepository(
                self.settings.postgres_dsn,
                schema=self.settings.postgres_schema,
            )
        return self._self_test_repository

    def self_test_controller(
        self,
        binding_probe: AgentBindingProbePort,
        canary_probe: CanaryProbePort,
        artifact_probe: ArtifactProbePort,
        observability_probe: ObservabilityProbePort,
        *,
        engine: WorkflowEnginePort | None = None,
    ) -> SelfTestController | None:
        repository = self.self_test_repository()
        validation = self.execution_ir_validation_service()
        resolved_engine = engine or self.workflow_engine()
        if repository is None or validation is None or resolved_engine is None:
            return None
        return SelfTestController(
            repository,
            (
                S0ValidationStage(validation),
                S1BindingStage(binding_probe),
                S2EngineStage(resolved_engine),
                S3CanaryArtifactStage(canary_probe, artifact_probe),
                S4ObservabilityStage(observability_probe),
            ),
        )

    def self_test_repair_service(
        self,
        controller: SelfTestController,
        repair: WorkflowRepairPort,
    ) -> SelfTestRepairService | None:
        repository = self.self_test_repository()
        compiler = self.workflow_compilation_service()
        if repository is None or compiler is None:
            return None
        return SelfTestRepairService(
            repository,
            controller,
            compiler,
            repair,
            SelfTestPolicy(self.settings.self_test_max_repair_attempts),
        )

    def workflow_release_service(
        self,
        *,
        engine: WorkflowEnginePort | None = None,
    ) -> WorkflowReleaseService | None:
        repository = self.self_test_repository()
        resolved_engine = engine or self.workflow_engine()
        if repository is None or resolved_engine is None:
            return None
        return WorkflowReleaseService(repository, resolved_engine)

    def execution_controller(
        self,
        *,
        engine: WorkflowEnginePort | None = None,
    ) -> ExecutionController | None:
        repository = self.self_test_repository()
        release = self.workflow_release_service(engine=engine)
        if repository is None or release is None:
            return None
        if not isinstance(repository, PostgresExecutionRepository):
            return None
        return ExecutionController(repository, release, self.artifact_manager())

    def artifact_manager(self) -> ArtifactManagerPort | None:
        if self._artifact_manager_resolved:
            return self._artifact_manager
        self._artifact_manager_resolved = True
        repository = self.control_plane_repository()
        if not self.settings.artifact_store_configured or not isinstance(
            repository, PostgresArtifactRepository
        ):
            return None
        endpoint = self.settings.minio_endpoint
        if "://" in endpoint:
            endpoint = urlparse(endpoint).netloc
        self._artifact_manager = MinioArtifactAdapter.connect(
            endpoint,
            self.settings.read_secret(
                self.settings.minio_access_key_file,
                setting_name="ORCHESTRATOR_MINIO_ACCESS_KEY_FILE",
            ),
            self.settings.read_secret(
                self.settings.minio_secret_key_file,
                setting_name="ORCHESTRATOR_MINIO_SECRET_KEY_FILE",
            ),
            repository,
            bucket=self.settings.minio_bucket,
            secure=self.settings.minio_secure,
        )
        return self._artifact_manager

    def runtime_event_service(self) -> RuntimeEventService | None:
        repository = self.control_plane_repository()
        transitions = self.state_transition_service()
        if not isinstance(repository, PostgresExecutionRepository) or transitions is None:
            return None
        if self._runtime_event_service is None:
            self._runtime_event_service = RuntimeEventService(
                repository,
                ReconciliationService(
                    repository,
                    repository,
                    transitions,
                    self.artifact_manager(),
                ),
            )
        return self._runtime_event_service

    def planning_blueprint_service(self) -> PlanningBlueprintIngestionService | None:
        if not self.settings.postgres_dsn:
            return None
        if self._planning_blueprint_service is None:
            if self._planning_blueprint_repository is None:
                self._planning_blueprint_repository = PostgresArtifactRepository(
                    self.settings.postgres_dsn,
                    schema=self.settings.postgres_schema,
                )
            repository = self._planning_blueprint_repository
            generation = None
            control = self.external_control()
            engine = self.workflow_engine()
            if control is not None and engine is not None:
                gateway = control.gateway
                agents = GatewayAgentRegistryRuntimeAdapter(gateway)
                validation = ExecutionIRValidationService(repository)
                compiler = N8nWorkflowCompilerAdapter(
                    N8nCompilerConfig(
                        webhook_credential_ref=self.settings.n8n_webhook_credential_ref,
                        monitoring_endpoint=self.settings.monitoring_endpoint,
                        task_orchestration_endpoint=self.settings.task_orchestration_endpoint,
                    )
                )
                compilation = WorkflowCompilationService(validation, compiler, repository)
                controller = SelfTestController(
                    repository,
                    (
                        S0ValidationStage(validation),
                        S1BindingStage(SnapshotAgentBindingProbe(repository)),
                        S2EngineStage(engine),
                        S3CanaryArtifactStage(
                            GatewayCanaryProbe(gateway), CanaryArtifactMetadataProbe()
                        ),
                        S4ObservabilityStage(GatewayObservabilityProbe(gateway)),
                    ),
                )
                release = WorkflowReleaseService(repository, engine)
                execution = ExecutionController(repository, release, self.artifact_manager())
                generation = ProductionPlanningBlueprintGenerationService(
                    repository,
                    repository,
                    agents,
                    agents,
                    validation,
                    compilation,
                    engine,
                    controller,
                    release,
                    execution,
                )
            self._planning_blueprint_service = PlanningBlueprintIngestionService(
                repository, generation=generation
            )
        return self._planning_blueprint_service

    def v2_gateway_ready(self) -> bool:
        control = self.external_control()
        if not self.settings.v2_enabled or not self.settings.v2_agent_allowed_hosts or control is None:
            return False
        try:
            control.require_v2_gateway()
            return True
        except Exception:
            return False

    def workflow_publisher(self) -> object | None:
        control = self.external_control()
        if control is None:
            return None
        if self._publisher is None:
            backend = N8nExecutionBackend(control.gateway)
            self._publisher = DurableWorkflowPublisherFacade(
                DurablePublisher(self.repository.database_path, backend)
            )
        return self._publisher

    def planning_worker(self, *, gateway_ready: bool | None = None) -> PlanningJobWorker | None:
        ready = self.v2_gateway_ready() if gateway_ready is None else gateway_ready
        control = self.external_control()
        if not ready or control is None:
            return None
        if self._planning_worker is None:
            publisher = DurablePublisher(
                self.repository.database_path, N8nExecutionBackend(control.gateway)
            )
            runner = PlanningJobRunner(
                self.repository.database_path,
                registry_loader=control.gateway.registry_v2,
                endpoint_policy=EndpointPolicy(
                    allowed_hosts=set(self.settings.v2_agent_allowed_hosts),
                    allowed_private_cidrs=self.settings.v2_allowed_private_cidrs,
                ),
                reasoner=ReasonerClient(
                    lambda value, operation_key: control.generate_reasoner_proposal(
                        value, operation_key=operation_key
                    ),
                    deadline_seconds=self.settings.reasoner_deadline_seconds,
                ),
                publisher=publisher,
                ranking_weights=RankingWeights(
                    cost=self.settings.rank_cost_weight,
                    latency=self.settings.rank_latency_weight,
                ),
                compiler_context={
                    "gateway_base_url": self.settings.n8n_base_url,
                    "gateway_credential_ref": self.settings.n8n_gateway_credential_ref,
                    "webhook_credential_ref": self.settings.n8n_webhook_credential_ref,
                },
            )
            self._planning_worker = PlanningJobWorker(self.repository.database_path, runner)
        return self._planning_worker

    def drain_outbox(self) -> None:
        control = self.external_control()
        if control is None:
            return
        dispatcher = OutboxDispatcher(
            {
                "n8n.resume": lambda value, _key: control.resume(value),
                "n8n.retry": lambda value, _key: control.resume(value),
                **planning_agent_outbox_handlers(control),
            }
        )
        for item in self.repository.pending_outbox(exclude_kinds={"planning.run"}):
            try:
                dispatcher.dispatch(item)
            except Exception as error:
                self.repository.mark_outbox_failed(str(item["outbox_id"]), str(error))
                continue
            self.repository.mark_outbox_sent(str(item["outbox_id"]))

    def workflow_compile_kwargs(self) -> dict[str, str]:
        return {
            "webhook_path_secret": self.settings.read_secret(
                self.settings.n8n_webhook_path_secret_file,
                setting_name="N8N_WEBHOOK_PATH_SECRET_FILE",
            ),
            "webhook_credential_ref": self.settings.n8n_webhook_credential_ref,
        }

    def health(self) -> list[ComponentHealth]:
        checks = [
            ComponentHealth("orchestrator", "ok"),
            RepositoryHealthAdapter(self.repository).check(),
        ]
        control = self.external_control()
        if control is not None:
            try:
                healthy = control.probe()
            except Exception:
                healthy = False
            checks.append(ComponentHealth("external_control", "ok" if healthy else "error"))
        elif self.settings.readiness_require_external_control:
            checks.append(ComponentHealth("external_control", "not_configured"))
        return checks

    def shutdown(self) -> None:
        close = getattr(self._external_control, "close", None)
        if callable(close):
            close()


def build_container(
    settings: OrchestratorSettings | None = None,
    *,
    repository: OrchestrationRepository | None = None,
    metrics: MetricsPort | None = None,
    tracer: TracerPort | None = None,
    external_control_factory: ExternalControlFactory | None = None,
) -> ApplicationContainer:
    resolved = settings or OrchestratorSettings.from_environment()
    repo = repository or OrchestrationRepository(Path(resolved.database_path))
    resolved_metrics = metrics or PrometheusMetrics(enabled=resolved.metrics_enabled)
    resolved_tracer = tracer or configure_otel(
        resolved.otel_enabled,
        service_name=resolved.service_name,
        endpoint=resolved.otel_endpoint,
    )
    return ApplicationContainer(
        resolved,
        repo,
        resolved_metrics,
        resolved_tracer,
        external_control_factory,
    )
