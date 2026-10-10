from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
from typing import Callable, Iterable
from urllib.parse import urlsplit

from pydantic import ConfigDict

from capabilities.models import AgentDescriptorV2, CapabilityRegistryV2, FrozenStrictModel


class RegistryValidationError(ValueError):
    """Registry data cannot safely participate in deterministic binding."""


Address = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str], Iterable[Address]]


def system_resolver(hostname: str) -> list[Address]:
    addresses = {
        ipaddress.ip_address(item[4][0])
        for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    }
    return sorted(addresses, key=lambda address: (address.version, int(address)))


class EndpointPolicy:
    def __init__(
        self,
        *,
        allowed_hosts: set[str],
        allowed_ports: set[int] | None = None,
        allowed_private_cidrs: tuple[str, ...] = (),
        resolver: Resolver = system_resolver,
    ) -> None:
        self.allowed_hosts = {host.rstrip(".").lower() for host in allowed_hosts}
        self.allowed_ports = allowed_ports
        self.allowed_private_networks = tuple(ipaddress.ip_network(value) for value in allowed_private_cidrs)
        self.resolver = resolver

    def _private_address_is_allowed(self, address: Address) -> bool:
        return any(address in network for network in self.allowed_private_networks)

    def _reject_prohibited_address(self, address: Address) -> None:
        prohibited = (
            address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
            or address.is_private
        )
        if prohibited and not self._private_address_is_allowed(address):
            raise RegistryValidationError(f"endpoint resolves to prohibited address: {address}")

    def validate(self, endpoint: str) -> str:
        try:
            parsed = urlsplit(endpoint)
            port = parsed.port
        except ValueError as error:
            raise RegistryValidationError(f"invalid endpoint URL: {error}") from error
        if parsed.scheme not in {"http", "https"}:
            raise RegistryValidationError("endpoint scheme must be http or https")
        if parsed.username is not None or parsed.password is not None:
            raise RegistryValidationError("endpoint userinfo is forbidden")
        if not parsed.hostname:
            raise RegistryValidationError("endpoint hostname is required")
        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith(".localhost"):
            raise RegistryValidationError("localhost endpoint is forbidden")
        if host not in self.allowed_hosts:
            raise RegistryValidationError(f"endpoint host is not allowlisted: {host}")
        effective_port = port if port is not None else (443 if parsed.scheme == "https" else 80)
        if self.allowed_ports is not None and effective_port not in self.allowed_ports:
            raise RegistryValidationError(f"endpoint port is not allowlisted: {effective_port}")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            try:
                addresses = list(self.resolver(host))
            except OSError as error:
                raise RegistryValidationError(f"endpoint DNS resolution failed: {host}") from error
            if not addresses:
                raise RegistryValidationError(f"endpoint DNS returned no addresses: {host}")
        else:
            addresses = [literal]
        for address in addresses:
            self._reject_prohibited_address(address)
        return endpoint


class RegistrySnapshot(FrozenStrictModel):
    snapshot_id: str
    snapshot_version: int
    snapshot_hash: str
    candidates: tuple[AgentDescriptorV2, ...]

    def default_evaluator(self, metric: str) -> AgentDescriptorV2:
        evaluators = [
            candidate
            for candidate in self.candidates
            if candidate.role == "evaluator"
            and candidate.health == "healthy"
            and "evaluate-workflow" in candidate.capabilities
        ]
        if len(evaluators) != 1:
            raise RegistryValidationError("registry must contain exactly one healthy default evaluator")
        evaluator = evaluators[0]
        properties = evaluator.output_schema.get("properties", {})
        if not isinstance(properties, dict) or metric not in properties:
            raise RegistryValidationError(f"default evaluator does not declare metric: {metric}")
        return evaluator


def build_registry_snapshot(
    registry: CapabilityRegistryV2,
    *,
    endpoint_policy: EndpointPolicy,
) -> RegistrySnapshot:
    candidates = tuple(sorted(registry.candidates, key=lambda candidate: candidate.agent_id))
    agent_ids = [candidate.agent_id for candidate in candidates]
    if len(agent_ids) != len(set(agent_ids)):
        raise RegistryValidationError("duplicate agent_id in registry snapshot")
    for candidate in candidates:
        endpoint_policy.validate(candidate.endpoint)
    canonical = json.dumps(
        {
            "schema_version": registry.schema_version,
            "snapshot_id": registry.snapshot_id,
            "snapshot_version": registry.snapshot_version,
            "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return RegistrySnapshot(
        snapshot_id=registry.snapshot_id,
        snapshot_version=registry.snapshot_version,
        snapshot_hash=hashlib.sha256(canonical).hexdigest(),
        candidates=candidates,
    )
