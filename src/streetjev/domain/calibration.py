"""Параметры постобработки оценок; веса нейросетей не изменяются."""

import math
from dataclasses import asdict, dataclass, replace

from streetjev.domain.clues import apply_clues
from streetjev.domain.evidence import Distribution


@dataclass(frozen=True)
class CalibrationParameters:
    imajev_country_temperature: float = 1.0
    imajev_region_temperature: float = 1.0
    chipoint_temperature: float = 2.0
    secondary_weight: float = 0.35
    driving_strength: float = 0.45
    hemisphere_strength: float = 0.25
    coverage_penalty: float = 0.15

    def __post_init__(self):
        for name, value in asdict(self).items():
            low, high = (0.25, 8) if "temperature" in name else (0, 0.8)
            if name == "secondary_weight":
                low, high = 0.05, 0.95
            if (
                not isinstance(value, (float, int))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or not low <= value <= high
            ):
                raise ValueError(f"Недопустимый параметр калибровки: {name}")


def temperature(distribution: Distribution, value: float) -> Distribution:
    if value == 1:
        return distribution
    logs = {key: math.log(p) / value for key, p in distribution.probabilities.items() if p > 0}
    maximum = max(logs.values())
    return Distribution(
        {key: math.exp(logs[key] - maximum) if key in logs else 0 for key in distribution.probabilities},
        distribution.unknown,
    )


def adjust_imajev(evidence, parameters, facts):
    result = replace(
        evidence,
        countries=temperature(evidence.countries, parameters.imajev_country_temperature),
        regions={k: temperature(v, parameters.imajev_region_temperature) for k, v in evidence.regions.items()},
    )
    return apply_clues(result, evidence.clues, facts, parameters.driving_strength, parameters.hemisphere_strength)


@dataclass(frozen=True)
class CalibrationProfile:
    parameters: CalibrationParameters = CalibrationParameters()
    imajev_models: tuple[str, ...] = ()
    chipoint_models: tuple[str, ...] = ()

    def for_model(self, source, model):
        expected = self.imajev_models if source == "imajev" else self.chipoint_models
        return self.parameters if model in expected else CalibrationParameters()

    def fusion_weight(self, primary, secondary):
        if primary in self.imajev_models and secondary in self.chipoint_models:
            return self.parameters.secondary_weight
        return 0.35
