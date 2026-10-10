from __future__ import annotations


class BudgetManager:
    def __init__(self, repository: object) -> None:
        self.repository = repository

    def create(self, execution_id: str, *, max_calls: int, max_cost_microusd: int, max_duration_seconds: int) -> None:
        self.repository.create_budget(
            execution_id, max_calls=max_calls, max_cost_microusd=max_cost_microusd,
            max_duration_seconds=max_duration_seconds,
        )

    def authorize(
        self, reservation_id: str, execution_id: str, node_id: str, attempt: int, *,
        calls: int | None, cost_microusd: int | None, duration_seconds: int | None, lease_until: float,
    ) -> bool:
        if calls is None or cost_microusd is None or duration_seconds is None:
            raise ValueError("complete usage telemetry is required for budget authorization")
        return self.repository.reserve_budget(
            reservation_id=reservation_id, execution_id=execution_id, node_id=node_id, attempt=attempt,
            calls=calls, cost_microusd=cost_microusd, duration_seconds=duration_seconds, lease_until=lease_until,
        )

    def release(self, reservation_id: str) -> bool:
        return self.repository.release_reservation(reservation_id)

    def expire(self, *, now: float) -> int:
        return self.repository.expire_reservations(now=now)

    def state(self, execution_id: str) -> dict[str, object]:
        return self.repository.budget_state(execution_id)

