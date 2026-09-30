"""Офлайн-калибровка оценок по правильным ответам. GPU и запущенные сервисы не нужны."""

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from streetjev.application.calibration import fit
from streetjev.domain.calibration import CalibrationParameters
from streetjev.infrastructure.calibration import (
    CalibrationReplay,
    profile_data,
    read_labels,
    read_profile,
    write_json,
)
from streetjev.infrastructure.catalog import load_geography


def calibrate(database, output, apply=False, seed=17, progress=print, imajev_model=None, chipoint_model=None):
    database, output = Path(database).resolve(), Path(output).resolve()
    active = database.parent / "calibration.json"
    destinations = (output / "report.json", output / "profile.json")
    if database in destinations or active in destinations or output == database:
        raise ValueError("Отчёт не может заменять БД или действующий профиль")
    catalog, geography = load_geography()
    replay = CalibrationReplay(catalog, geography)
    samples, skipped = replay.prepare(read_labels(database), imajev_model, chipoint_model)
    if progress:
        progress(
            f"Размеченных раундов: {len(samples)}; исключено: {len(skipped)}. "
            f"imajev: {', '.join(replay.imajev_models)}; Chipoint: {', '.join(replay.chipoint_models) or 'нет'}"
        )
    initial = CalibrationParameters()
    initial_source = "defaults"
    if active.exists():
        current = read_profile(active, catalog)
        expected = replay.profile(current.parameters)
        if current == expected:
            initial = current.parameters
            initial_source = str(active)
    parameters, report = fit(samples, replay, initial=initial, seed=seed, progress=progress)
    profile = profile_data(replay.profile(parameters), catalog)
    report.update(
        {
            "created_at": datetime.now(UTC).isoformat(),
            "database": str(database),
            "initial_source": initial_source,
            "skipped": skipped,
            "models": profile["models"],
            "applied": bool(apply and report["eligible_to_apply"]),
        }
    )
    profile["validation"] = report["validation"]
    profile["created_at"] = report["created_at"]
    profile["validation_rounds"] = report["validation_rounds"]
    write_json(destinations[1], profile)
    if report["applied"]:
        if active.exists():
            # Предыдущая версия остаётся рядом для отката.
            backup = active.with_name("calibration.previous.json")
            write_json(backup, json.loads(active.read_text(encoding="utf-8")))
        write_json(active, profile)
    write_json(destinations[0], report)
    if progress:
        before, after = report["validation"]["baseline"], report["validation"]["candidate"]
        progress(
            f"Проверка на {after['rounds']} раундах: loss {before['loss']:.4f} → {after['loss']:.4f}; "
            f"страна {before['country_accuracy']:.1%} → {after['country_accuracy']:.1%}"
        )
        progress(
            "Профиль применён. Перезапустите приложение."
            if report["applied"]
            else "Профиль не применён. Отчёт и кандидат сохранены в " + str(output)
        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/rounds.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/calibration"))
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--imajev-model", help="точная версия; по умолчанию самая представленная в разметке")
    parser.add_argument("--chipoint-model", help="точная версия; по умолчанию самая представленная в разметке")
    parser.add_argument(
        "--apply", action="store_true", help="применить только при улучшении holdout без снижения accuracy"
    )
    args = parser.parse_args()
    try:
        calibrate(
            args.database,
            args.output,
            args.apply,
            args.seed,
            imajev_model=args.imajev_model,
            chipoint_model=args.chipoint_model,
        )
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        parser.exit(2, f"Калибровка не выполнена: {exc}\n")


if __name__ == "__main__":
    main()
