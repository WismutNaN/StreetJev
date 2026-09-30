import math
from dataclasses import dataclass


@dataclass(frozen=True)
class GroundTruth:
    latitude: float
    longitude: float
    country: str
    region: str | None = None

    def __post_init__(self):
        if (
            not math.isfinite(self.latitude)
            or not math.isfinite(self.longitude)
            or not -90 <= self.latitude <= 90
            or not -180 <= self.longitude <= 180
            or not self.country
        ):
            raise ValueError("Некорректное правильное место")
