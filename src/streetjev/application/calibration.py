"""Подбор постобработки на размеченных раундах; holdout не участвует в поиске."""

import math
import random
from dataclasses import asdict, dataclass, replace

from streetjev.domain.calibration import CalibrationParameters
from streetjev.domain.ground_truth import GroundTruth


@dataclass(frozen=True)
class LabeledRound:
    id: str
    truth: GroundTruth
    observations: tuple

    @property
    def group(self):
        # Повторные раунды в одной ячейке ~28 км не попадают по обе стороны split.
        return math.floor(self.truth.latitude * 4), math.floor(self.truth.longitude * 4)


def split_rounds(samples, seed=17):
    groups = sorted({sample.group for sample in samples})
    if len(groups) < 12:
        raise ValueError(
            f"Нужно минимум 12 различных ячеек с размеченными раундами; сейчас {len(groups)}. "
            "Для устойчивой калибровки желательно 50–100 разнообразных мест."
        )
    random.Random(seed).shuffle(groups)
    held_out = set(groups[: max(4, math.ceil(len(groups) * 0.25))])
    return ([s for s in samples if s.group not in held_out], [s for s in samples if s.group in held_out])


def measure(samples, parameters, predict):
    country_losses, region_losses, hits, region_hits, confidence = [], [], [], [], []
    supported = []
    brier = []
    for sample in samples:
        snapshot = predict(sample, parameters)
        truth = sample.truth
        scores = snapshot.countries
        best = max(scores, key=scores.get)
        # Не выдаём полностью неизвестный ответ за успешное угадывание первого кода.
        hit = snapshot.known_frames > 0 and best == truth.country
        hits.append(hit)
        confidence.append(scores[best])
        country_losses.append(-math.log(max(1e-12, scores[truth.country])))
        brier.append(sum((p - (code == truth.country)) ** 2 for code, p in scores.items()))
        if truth.region:
            region_losses.append(-math.log(max(1e-12, snapshot.regions[truth.region])))
            observed = truth.country in snapshot.observed_regions
            supported.append(observed)
            available = {
                code: score
                for code, score in snapshot.regions.items()
                if predict.region_country(code) in snapshot.observed_regions
            }
            region_hits.append(observed and bool(available) and max(available, key=available.get) == truth.region)
    mean = lambda values: sum(values) / len(values) if values else None
    ece = 0.0
    for lower in range(10):
        indices = [i for i, p in enumerate(confidence) if min(9, int(p * 10)) == lower]
        if indices:
            ece += (
                len(indices)
                / len(samples)
                * abs(mean([confidence[i] for i in indices]) - mean([hits[i] for i in indices]))
            )
    country_loss, region_loss = mean(country_losses), mean(region_losses)
    return {
        "rounds": len(samples),
        "country_accuracy": mean(hits),
        "region_accuracy": mean(region_hits),
        "country_log_loss": country_loss,
        "region_log_loss": region_loss,
        "loss": country_loss if region_loss is None else (country_loss + region_loss) / 2,
        "country_brier": mean(brier),
        "country_ece_10_bins": ece,
        "region_labels": len(region_losses),
        "region_branch_coverage": mean(supported),
    }


GRID = {
    "imajev_country_temperature": (0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8),
    "imajev_region_temperature": (0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8),
    "chipoint_temperature": (0.5, 1, 1.5, 2, 3, 4, 6, 8),
    "secondary_weight": (0.1, 0.25, 0.35, 0.5, 0.65, 0.8, 0.9),
    "driving_strength": (0, 0.15, 0.3, 0.45, 0.6),
    "hemisphere_strength": (0, 0.1, 0.25, 0.4),
    "coverage_penalty": (0, 0.05, 0.15, 0.3),
}


def improves(candidate, baseline):
    return (
        candidate["loss"] < baseline["loss"] - 1e-5
        and candidate["country_accuracy"] >= baseline["country_accuracy"]
        and (
            candidate["region_accuracy"] is None
            or baseline["region_accuracy"] is None
            or candidate["region_accuracy"] >= baseline["region_accuracy"]
        )
    )


def fit(samples, predict, initial=None, seed=17, progress=None):
    initial = initial or CalibrationParameters()
    training, validation = split_rounds(samples, seed)
    base_train = measure(training, initial, predict)
    best, best_metrics = initial, base_train
    evaluated = 1
    # Поиск только на training. Равные варианты сохраняют исходные настройки.
    for cycle in range(2):
        previous = best
        for name, grid in GRID.items():
            for value in grid:
                candidate = replace(best, **{name: value})
                if candidate == best:
                    continue
                metrics = measure(training, candidate, predict)
                evaluated += 1
                if metrics["loss"] < best_metrics["loss"] - 1e-5:
                    best, best_metrics = candidate, metrics
            if progress:
                progress(f"Проход {cycle + 1}: {name} = {getattr(best, name):g}; loss {best_metrics['loss']:.4f}")
        if previous == best:
            break
    base_validation = measure(validation, initial, predict)
    candidate_validation = measure(validation, best, predict)
    report = {
        "algorithm": "coordinate_grid_v1",
        "seed": seed,
        "evaluations": evaluated,
        "training_rounds": [s.id for s in training],
        "validation_rounds": [s.id for s in validation],
        "training_groups": len({s.group for s in training}),
        "validation_groups": len({s.group for s in validation}),
        "grouping": "0.25 degree geographic cells; entire rounds kept together",
        "initial_parameters": asdict(initial),
        "parameters": asdict(best),
        "training": {"baseline": base_train, "candidate": best_metrics},
        "validation": {"baseline": base_validation, "candidate": candidate_validation},
        "eligible_to_apply": improves(candidate_validation, base_validation),
        "limitations": [
            "Postprocessing only; no neural weight training.",
            "Recorded region branches and retrieval candidates stay fixed.",
            "Validation is a small holdout, not proof of general accuracy improvement.",
            "Repeated tuning on the same holdout requires a fresh independent evaluation.",
        ],
    }
    return best, report
