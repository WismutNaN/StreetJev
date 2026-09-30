"""Подбор постобработки на размеченных раундах; holdout не участвует в поиске."""

import math
import random
from dataclasses import asdict, dataclass, replace

from streetjev.application.spatial_metrics import has_coordinates, point_error, summarize_distances
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


def split_rounds(samples, seed=17, validation_pool=None):
    groups = sorted({sample.group for sample in samples})
    if len(groups) < 12:
        raise ValueError(
            f"Нужно минимум 12 различных ячеек с размеченными раундами; сейчас {len(groups)}. "
            "Для устойчивой калибровки желательно 50–100 разнообразных мест."
        )
    if validation_pool is not None:
        # Новая разметка рядом со старым раундом не является независимой проверкой.
        used_groups = {sample.group for sample in samples if sample.id not in validation_pool}
        groups = [group for group in groups if group not in used_groups]
        if len(groups) < 4:
            raise ValueError(
                f"Для независимой проверки нужно минимум 4 новые географические ячейки; сейчас {len(groups)}"
            )
    random.Random(seed).shuffle(groups)
    held_out = set(groups[: max(4, math.ceil(len(groups) * 0.25))])
    return ([s for s in samples if s.group not in held_out], [s for s in samples if s.group in held_out])


def measure(samples, parameters, predict, objective="categorical", details=None):
    if objective not in ("categorical", "geographic"):
        raise ValueError("Неизвестная цель калибровки")
    country_losses, region_losses, hits, region_hits, confidence = [], [], [], [], []
    supported = []
    brier = []
    country_top5, region_top5 = [], []
    distances = []
    for sample in samples:
        snapshot = predict(sample, parameters)
        truth = sample.truth
        spatial = point_error(snapshot, truth) if has_coordinates(sample) else None
        if spatial is not None:
            distances.append(spatial)
        if details is not None:
            details[sample.id] = {"truth": asdict(truth), "spatial": spatial}
        scores = snapshot.countries
        best = max(scores, key=scores.get)
        # Не выдаём полностью неизвестный ответ за успешное угадывание первого кода.
        hit = snapshot.known_frames > 0 and best == truth.country
        hits.append(hit)
        country_top5.append(
            snapshot.known_frames > 0 and truth.country in sorted(scores, key=scores.get, reverse=True)[:5]
        )
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
            region_top5.append(observed and truth.region in sorted(available, key=available.get, reverse=True)[:5])
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
    classification_loss = country_loss if region_loss is None else (country_loss + region_loss) / 2
    spatial_metrics = summarize_distances(distances)
    loss = classification_loss
    if objective == "geographic" and spatial_metrics["point_log_distance"] is not None:
        loss = (classification_loss + spatial_metrics["point_log_distance"]) / 2
    return {
        "rounds": len(samples),
        "country_accuracy": mean(hits),
        "region_accuracy": mean(region_hits),
        "country_top5_accuracy": mean(country_top5),
        "region_top5_accuracy": mean(region_top5),
        "country_log_loss": country_loss,
        "region_log_loss": region_loss,
        "classification_loss": classification_loss,
        "loss": loss,
        "country_brier": mean(brier),
        "country_ece_10_bins": ece,
        "region_labels": len(region_losses),
        "region_branch_coverage": mean(supported),
        **spatial_metrics,
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
        and all(
            candidate.get(name) is None or baseline.get(name) is None or candidate[name] >= baseline[name]
            for name in ("country_top5_accuracy", "region_top5_accuracy")
        )
        and (
            not baseline.get("point_rounds")
            or (
                candidate.get("point_rounds") == baseline["point_rounds"]
                and candidate["point_prediction_coverage"] >= baseline["point_prediction_coverage"]
                and all(
                    baseline[name] is None
                    or candidate[name] is not None
                    and candidate[name] <= baseline[name] + 1e-8
                    for name in ("point_log_distance", "point_distance_mean_km", "point_distance_median_km")
                )
            )
        )
    )


def fit(samples, predict, initial=None, seed=17, progress=None, validation_pool=None, objective="categorical"):
    initial = initial or CalibrationParameters()
    training, validation = split_rounds(samples, seed, validation_pool)
    base_train = measure(training, initial, predict, objective)
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
                metrics = measure(training, candidate, predict, objective)
                evaluated += 1
                if metrics["loss"] < best_metrics["loss"] - 1e-5:
                    best, best_metrics = candidate, metrics
            if progress:
                progress(f"Проход {cycle + 1}: {name} = {getattr(best, name):g}; loss {best_metrics['loss']:.4f}")
        if previous == best:
            break
    base_details, candidate_details = {}, {}
    base_validation = measure(validation, initial, predict, objective, base_details)
    candidate_validation = measure(validation, best, predict, objective, candidate_details)
    report = {
        "algorithm": "coordinate_grid_v1",
        "acceptance_policy": "objective_improves_top1_top5_distance_non_decreasing_v3",
        "objective": objective,
        "distance_definition": {
            "selection": "highest-mass point in the leading country, same as the map ranking",
            "distance": "great-circle kilometres on a sphere, radius 6371.0088 km",
            "geographic_objective": "(classification log loss + mean log1p(selected distance / 25 km)) / 2",
            "no_coordinate_fallback": "classification loss only",
            "missing_prediction": "half Earth circumference penalty, not zero distance",
            "no_coordinate_source": "excluded from distance metrics; classification remains evaluated",
            "top5": "nearest of five points with greatest final mass, diagnostic only",
            "nearest_candidate": "nearest final snapshot point; uses truth for evaluation only, never for selection",
            "summary": "mean/median/p90 over available predictions; radius hits over all coordinate-eligible rounds",
        },
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
        "validation_details": {
            sample.id: {"baseline": base_details[sample.id], "candidate": candidate_details[sample.id]}
            for sample in validation
        },
        "limitations": [
            "Postprocessing only; no neural weight training.",
            "Recorded region branches and retrieval candidates stay fixed.",
            "Validation is a small holdout, not proof of general accuracy improvement.",
            "Repeated tuning on the same holdout requires a fresh independent evaluation.",
            "Distances evaluate the leading country's strongest point, not a user-selected guess or area centroid.",
        ],
    }
    return best, report
