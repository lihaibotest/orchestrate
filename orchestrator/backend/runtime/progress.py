from __future__ import annotations


class ProgressDetector:
    def __init__(self, *, minimum_delta: float, patience: int) -> None:
        if minimum_delta <= 0 or patience < 1:
            raise ValueError("progress policy requires positive minimum_delta and patience")
        self.minimum_delta = minimum_delta
        self.patience = patience
        self._seen: set[str] = set()
        self._previous: float | None = None
        self._low_gain = 0

    def observe(self, sample_id: str, value: float) -> str:
        if sample_id in self._seen:
            return "CONTINUE"
        self._seen.add(sample_id)
        if self._previous is None or value - self._previous >= self.minimum_delta:
            self._low_gain = 0
        else:
            self._low_gain += 1
        self._previous = value
        if self._low_gain >= self.patience:
            self._low_gain = 0
            return "REPLAN"
        return "CONTINUE"

