from __future__ import annotations

from dataclasses import dataclass
from capabilities.models import AgentDescriptorV2


@dataclass(frozen=True)
class RankingWeights:
    cost: int = 1
    latency: int = 1

    def __post_init__(self) -> None:
        if self.cost < 0 or self.latency < 0:
            raise ValueError("ranking weights must be non-negative")

    @classmethod
    def from_settings(cls, settings: object) -> "RankingWeights":
        return cls(
            cost=int(getattr(settings, "rank_cost_weight", 1)),
            latency=int(getattr(settings, "rank_latency_weight", 1)),
        )

    def key(self, candidate: AgentDescriptorV2) -> tuple[int, str]:
        score = self.cost * candidate.cost_microusd + self.latency * candidate.latency_ms
        return score, candidate.agent_id
