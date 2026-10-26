from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    raw = _env(name, "true" if default else "false").lower()
    if raw in TRUE_VALUES:
        return True
    if raw in FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be a boolean")


def _float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(_env(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be a number") from error
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(_env(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _csv(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in _env(name).split(",") if item.strip())


@dataclass(frozen=True)
class OrchestratorSettings:
    """Single validated configuration source for the orchestrator process."""

    database_path: str = "/var/lib/orchestrator/orchestrator.sqlite3"
    postgres_dsn: str = ""
    postgres_schema: str = "orchestrator"
    redis_url: str = ""
    redis_required: bool = False
    service_name: str = "execution-orchestrator"
    environment: str = "development"
    log_level: str = "INFO"
    metrics_enabled: bool = True
    otel_enabled: bool = False
    otel_endpoint: str = ""
    readiness_require_external_control: bool = False
    request_id_header: str = "X-Request-ID"
    execution_submit_token_file: str = ""

    v2_enabled: bool = False
    v2_agent_allowed_hosts: tuple[str, ...] = ()
    v2_allowed_private_cidrs: tuple[str, ...] = ()
    reasoner_deadline_seconds: float = 300.0
    planning_worker_poll_seconds: float = 1.0
    rank_cost_weight: int = 1
    rank_latency_weight: int = 1

    n8n_base_url: str = ""
    n8n_api_key_file: str = ""
    n8n_gateway_credential_ref: str = "orchestrator-gateway"
    n8n_webhook_credential_ref: str = "orchestrator-webhook"
    n8n_webhook_path_secret_file: str = ""
    n8n_webhook_auth_header: str = "X-Orchestrator-Webhook"
    n8n_webhook_auth_value_file: str = ""
    monitoring_endpoint: str = "http://172.26.0.1:8080"
    task_orchestration_endpoint: str = "http://172.26.0.1:7000"
    gateway_mutation_timeout_seconds: float = 180.0
    self_test_max_repair_attempts: int = 3

    minio_endpoint: str = ""
    minio_bucket: str = "orchestrator-artifacts"
    minio_access_key_file: str = ""
    minio_secret_key_file: str = ""
    minio_secure: bool = True

    planning_agent_base_url: str = ""
    planning_agent_hmac_secret_file: str = ""
    n8n_hmac_secret_file: str = ""
    workflow_llm_base_url: str = ""
    workflow_llm_api_key_file: str = ""
    workflow_llm_model: str = "deepseek-v4-flash"
    workflow_llm_timeout_seconds: float = 120.0
    workflow_llm_network_attempts: int = 3

    @property
    def external_control_configured(self) -> bool:
        required = (
            self.n8n_base_url,
            self.planning_agent_base_url,
            self.workflow_llm_base_url,
            self.n8n_api_key_file,
            self.n8n_webhook_auth_value_file,
            self.n8n_webhook_path_secret_file,
        )
        if not all(value.strip() for value in required):
            return False
        if urlparse(self.workflow_llm_base_url).hostname != "api.deepseek.com":
            return True
        return self._secret_exists(self.workflow_llm_api_key_file)

    @property
    def artifact_store_configured(self) -> bool:
        return bool(
            self.minio_endpoint
            and self.minio_bucket
            and self._secret_exists(self.minio_access_key_file)
            and self._secret_exists(self.minio_secret_key_file)
        )

    @staticmethod
    def _secret_exists(path: str) -> bool:
        try:
            return bool(path and Path(path).is_file() and Path(path).read_text(encoding="utf-8").strip())
        except OSError:
            return False

    @staticmethod
    def read_secret(path: str, *, setting_name: str) -> str:
        if not path:
            raise RuntimeError(f"{setting_name} must point to a mounted secret file")
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError as error:
            raise RuntimeError(f"unable to read secret configured by {setting_name}") from error
        if not value:
            raise RuntimeError(f"secret configured by {setting_name} must not be empty")
        return value

    @classmethod
    def from_environment(cls) -> "OrchestratorSettings":
        path = _env("ORCHESTRATOR_DATABASE_PATH", cls.database_path)
        if not path:
            raise ValueError("ORCHESTRATOR_DATABASE_PATH must not be empty")
        level = _env("ORCHESTRATOR_LOG_LEVEL", cls.log_level).upper()
        if level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("ORCHESTRATOR_LOG_LEVEL is invalid")
        return cls(
            database_path=str(Path(path)),
            postgres_dsn=_env("ORCHESTRATOR_POSTGRES_DSN"),
            postgres_schema=_env("ORCHESTRATOR_POSTGRES_SCHEMA", "orchestrator"),
            redis_url=_env("ORCHESTRATOR_REDIS_URL"),
            redis_required=_bool("ORCHESTRATOR_REDIS_REQUIRED", False),
            service_name=_env("ORCHESTRATOR_SERVICE_NAME", cls.service_name),
            environment=_env("ORCHESTRATOR_ENVIRONMENT", cls.environment),
            log_level=level,
            metrics_enabled=_bool("ORCHESTRATOR_METRICS_ENABLED", True),
            otel_enabled=_bool("ORCHESTRATOR_OTEL_ENABLED", False),
            otel_endpoint=_env("OTEL_EXPORTER_OTLP_ENDPOINT"),
            readiness_require_external_control=_bool(
                "ORCHESTRATOR_READINESS_REQUIRE_EXTERNAL_CONTROL", False
            ),
            request_id_header=_env("ORCHESTRATOR_REQUEST_ID_HEADER", cls.request_id_header),
            execution_submit_token_file=_env("ORCHESTRATOR_EXECUTION_SUBMIT_TOKEN_FILE"),
            v2_enabled=_bool("ORCHESTRATOR_V2_ENABLED", False),
            v2_agent_allowed_hosts=tuple(
                item.lower() for item in _csv("ORCHESTRATOR_V2_AGENT_ALLOWED_HOSTS")
            ),
            v2_allowed_private_cidrs=_csv("ORCHESTRATOR_V2_ALLOWED_PRIVATE_CIDRS"),
            reasoner_deadline_seconds=_float(
                "ORCHESTRATOR_REASONER_DEADLINE_SECONDS", 300.0, minimum=0.1
            ),
            planning_worker_poll_seconds=_float(
                "ORCHESTRATOR_PLANNING_WORKER_POLL_SECONDS", 1.0, minimum=0.01
            ),
            rank_cost_weight=_int("ORCHESTRATOR_RANK_COST_WEIGHT", 1, minimum=0),
            rank_latency_weight=_int("ORCHESTRATOR_RANK_LATENCY_WEIGHT", 1, minimum=0),
            n8n_base_url=_env("N8N_BASE_URL"),
            n8n_api_key_file=_env("N8N_API_KEY_FILE"),
            n8n_gateway_credential_ref=_env(
                "N8N_GATEWAY_CREDENTIAL_REF", cls.n8n_gateway_credential_ref
            ),
            n8n_webhook_credential_ref=_env(
                "N8N_WEBHOOK_CREDENTIAL_REF", cls.n8n_webhook_credential_ref
            ),
            n8n_webhook_path_secret_file=_env("N8N_WEBHOOK_PATH_SECRET_FILE"),
            n8n_webhook_auth_header=_env(
                "N8N_WEBHOOK_AUTH_HEADER", cls.n8n_webhook_auth_header
            ),
            n8n_webhook_auth_value_file=_env("N8N_WEBHOOK_AUTH_VALUE_FILE"),
            monitoring_endpoint=_env(
                "ORCHESTRATOR_MONITORING_ENDPOINT", cls.monitoring_endpoint
            ),
            task_orchestration_endpoint=_env(
                "ORCHESTRATOR_TASK_ORCHESTRATION_ENDPOINT",
                cls.task_orchestration_endpoint,
            ),
            gateway_mutation_timeout_seconds=_float(
                "ORCHESTRATOR_GATEWAY_MUTATION_TIMEOUT_SECONDS", 180.0, minimum=0.1
            ),
            self_test_max_repair_attempts=_int(
                "ORCHESTRATOR_SELF_TEST_MAX_REPAIR_ATTEMPTS", 3, minimum=0
            ),
            minio_endpoint=_env("ORCHESTRATOR_MINIO_ENDPOINT"),
            minio_bucket=_env(
                "ORCHESTRATOR_MINIO_BUCKET", cls.minio_bucket
            ),
            minio_access_key_file=_env("ORCHESTRATOR_MINIO_ACCESS_KEY_FILE"),
            minio_secret_key_file=_env("ORCHESTRATOR_MINIO_SECRET_KEY_FILE"),
            minio_secure=_bool("ORCHESTRATOR_MINIO_SECURE", True),
            planning_agent_base_url=_env("ORCHESTRATOR_PLANNING_AGENT_BASE_URL"),
            planning_agent_hmac_secret_file=_env(
                "ORCHESTRATOR_PLANNING_AGENT_HMAC_SECRET_FILE"
            ),
            n8n_hmac_secret_file=_env("ORCHESTRATOR_N8N_HMAC_SECRET_FILE"),
            workflow_llm_base_url=_env("ORCHESTRATOR_WORKFLOW_LLM_BASE_URL"),
            workflow_llm_api_key_file=_env("ORCHESTRATOR_WORKFLOW_LLM_API_KEY_FILE"),
            workflow_llm_model=_env(
                "ORCHESTRATOR_WORKFLOW_LLM_MODEL", cls.workflow_llm_model
            ),
            workflow_llm_timeout_seconds=_float(
                "ORCHESTRATOR_WORKFLOW_LLM_TIMEOUT_SECONDS", 120.0, minimum=0.1
            ),
            workflow_llm_network_attempts=_int(
                "ORCHESTRATOR_WORKFLOW_LLM_NETWORK_ATTEMPTS", 3, minimum=1
            ),
        )
