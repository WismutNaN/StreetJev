"""Профили и размеченные раунды. Чтение датасета никогда не изменяет исходную БД."""

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from streetjev.application.calibration import LabeledRound
from streetjev.domain.calibration import CalibrationParameters, CalibrationProfile, adjust_imajev
from streetjev.domain.clues import apply_clues
from streetjev.domain.evidence import Frame
from streetjev.domain.fusion import fuse_evidence
from streetjev.domain.ground_truth import GroundTruth
from streetjev.domain.round import GeoRound
from streetjev.infrastructure.chipoint import ChipointExpert
from streetjev.infrastructure.clues import load_clue_facts
from streetjev.infrastructure.imajev import ImajevExpert

SCHEMA = "streetjev.calibration.v1"


def catalog_hash(catalog):
    codes = sorted(catalog.countries) + sorted(f"{r.code}:{r.country_code}" for r in catalog.regions.values())
    return hashlib.sha256("\n".join(codes).encode()).hexdigest()


def profile_data(profile, catalog):
    return {
        "schema": SCHEMA,
        "catalog_hash": catalog_hash(catalog),
        "parameters": asdict(profile.parameters),
        "models": {"imajev": list(profile.imajev_models), "chipoint": list(profile.chipoint_models)},
    }


def read_profile(path, catalog):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data["schema"] != SCHEMA or data["catalog_hash"] != catalog_hash(catalog):
        raise ValueError("Профиль не соответствует алгоритму или каталогу карты")
    models = data["models"]
    for key in ("imajev", "chipoint"):
        if not isinstance(models[key], list) or any(not isinstance(m, str) or not m for m in models[key]):
            raise ValueError("Неверные версии моделей в профиле")
    if not models["imajev"]:
        raise ValueError("В профиле отсутствует версия imajev")
    return CalibrationProfile(
        CalibrationParameters(**data["parameters"]), tuple(models["imajev"]), tuple(models["chipoint"])
    )


def write_json(path, data):
    """Атомарная замена: прерванная запись не оставляет половину профиля."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def read_labels(database):
    connection = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "ground_truth" not in tables:
            return []
        samples = []
        for row in connection.execute("SELECT * FROM ground_truth ORDER BY round_id"):
            observations = []
            for item in connection.execute(
                "SELECT * FROM observations WHERE round_id=? ORDER BY rowid", (row["round_id"],)
            ):
                secondary = None
                if "supplementary_observations" in tables:
                    result = connection.execute(
                        "SELECT * FROM supplementary_observations WHERE frame_id=? AND expert='chipoint' AND active=1",
                        (item["frame_id"],),
                    ).fetchone()
                    secondary = dict(result) if result else None
                observations.append({**dict(item), "secondary": secondary})
            samples.append(
                {
                    "id": row["round_id"],
                    "truth": GroundTruth(row["latitude"], row["longitude"], row["country"], row["region"]),
                    "observations": observations,
                }
            )
        return samples
    finally:
        connection.close()


class CalibrationReplay:
    def __init__(self, catalog, geography):
        self.catalog = catalog
        self.facts = load_clue_facts(geography)
        self.imajev = ImajevExpert(catalog)
        self.chipoint = ChipointExpert(catalog, geography)
        self.imajev_models = set()
        self.chipoint_models = set()
        self._chip_cache = {}

    def prepare(self, rows, imajev_model=None, chipoint_model=None):
        samples, skipped = [], []
        for row in rows:
            try:
                truth = row["truth"]
                if truth.country not in self.catalog.countries or (
                    truth.region is not None
                    and (
                        truth.region not in self.catalog.regions
                        or self.catalog.regions[truth.region].country_code != truth.country
                    )
                ):
                    raise ValueError("Ответ не соответствует каталогу карты")
                observations, primary_models, secondary_models = [], set(), set()
                for item in row["observations"]:
                    if item["expert"] != "imajev_hierarchical":
                        raise ValueError("Раунд содержит демо или неподдерживаемого эксперта")
                    evidence = self.imajev.decode(item["raw_json"], item["elapsed_ms"])
                    if evidence.model_version != item["model_version"]:
                        raise ValueError("Версия imajev не соответствует исходному ответу")
                    primary_models.add(evidence.model_version)
                    secondary = item["secondary"]
                    payload = json.loads(secondary["raw_json"]) if secondary else None
                    if payload:
                        decoded = self.chipoint.convert(payload, 0)  # Проверка контракта до поиска.
                        if decoded.model_version != secondary["model_version"]:
                            raise ValueError("Версия Chipoint не соответствует исходному ответу")
                        secondary_models.add(decoded.model_version)
                    frame = Frame(
                        item["frame_id"], b"", item["frame_id"].encode(), item["captured_at"], item["source"]
                    )
                    observations.append((frame, item["novelty"], evidence, payload))
                if not observations:
                    raise ValueError("Нет наблюдений")
                samples.append(LabeledRound(row["id"], truth, tuple(observations)))
                self.imajev_models.update(primary_models)
                self.chipoint_models.update(secondary_models)
            except (ValueError, KeyError, TypeError) as exc:
                skipped.append({"round": row["id"], "reason": str(exc)})

        def choose(source, requested, primary_version=None):
            counts = Counter()
            for sample in samples:
                if primary_version and any(obs[2].model_version != primary_version for obs in sample.observations):
                    continue
                counts.update(
                    {
                        (obs[2].model_version if source == "imajev" else obs[3]["model"])
                        for obs in sample.observations
                        if source == "imajev" or obs[3]
                    }
                )
            if requested and requested not in counts:
                raise ValueError(f"Нет размеченных раундов для {source}: {requested}")
            return requested or (counts.most_common(1)[0][0] if counts else None)

        primary_version = choose("imajev", imajev_model)
        secondary_version = choose("chipoint", chipoint_model, primary_version)
        selected = []
        for sample in samples:
            if any(
                obs[2].model_version != primary_version or (obs[3] and obs[3]["model"] != secondary_version)
                for obs in sample.observations
            ):
                skipped.append({"round": sample.id, "reason": "Другая версия модели"})
            else:
                selected.append(sample)
        self.imajev_models = {obs[2].model_version for s in selected for obs in s.observations}
        self.chipoint_models = {obs[3]["model"] for s in selected for obs in s.observations if obs[3]}
        return selected, skipped

    def profile(self, parameters):
        return CalibrationProfile(
            parameters, tuple(sorted(self.imajev_models)), tuple(sorted(self.chipoint_models))
        )

    def region_country(self, code):
        return self.catalog.regions[code].country_code

    def __call__(self, sample, parameters):
        state = GeoRound(self.catalog)
        profile = self.profile(parameters)
        self.chipoint.profile = profile
        for frame, novelty, primary, payload in sample.observations:
            evidence = adjust_imajev(primary, parameters, self.facts)
            if payload:
                key = (frame.id, parameters.chipoint_temperature, parameters.coverage_penalty)
                if key not in self._chip_cache:
                    # Ограничение памяти для длинных серий; вычисления полностью локальные.
                    if len(self._chip_cache) >= 2048:
                        self._chip_cache.clear()
                    self._chip_cache[key] = self.chipoint.convert(payload, 0)
                secondary = apply_clues(
                    self._chip_cache[key],
                    primary.clues,
                    self.facts,
                    parameters.driving_strength,
                    parameters.hemisphere_strength,
                )
                evidence = fuse_evidence(evidence, secondary, parameters.secondary_weight)
            state.accept(state.id, frame, evidence, novelty)
        return state.snapshot()
