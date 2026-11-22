"""Explicit operator-selected settings shared by intake and the diode worker."""
from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class ExecutionSettings:
    cycles: int
    top_mfs: int
    top_entries: int
    qrs_threshold: int
    duration_sec: float
    count: int

    def __post_init__(self):
        for name in ("cycles", "top_mfs", "top_entries", "count"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError("Execution counts must be positive integers")
        if type(self.qrs_threshold) is not int:
            raise ValueError("QRS threshold must be an integer")
        if type(self.duration_sec) not in (int, float) or not math.isfinite(self.duration_sec) or self.duration_sec <= 0:
            raise ValueError("Duration must be finite and positive")

    def as_dict(self):
        return asdict(self)
