import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from streetjev.domain.evidence import Frame, GeoEvidence
from streetjev.domain.ground_truth import GroundTruth
from streetjev.domain.round import RoundSnapshot


def snapshot_json(snapshot: RoundSnapshot) -> str:
    data = asdict(snapshot)
    data["observed_regions"] = sorted(snapshot.observed_regions)
    data["last_observed_regions"] = sorted(snapshot.last_observed_regions)
    return json.dumps(data, ensure_ascii=False)


def evidence_json(evidence: GeoEvidence) -> str:
    return json.dumps(
        {
            "countries": dict(evidence.countries.probabilities),
            "unknown": evidence.countries.unknown,
            "regions": {
                k: {"probabilities": dict(v.probabilities), "unknown": v.unknown}
                for k, v in evidence.regions.items()
            },
            "clues": {
                k: {"probabilities": dict(v.probabilities), "unknown": v.unknown}
                for k, v in evidence.clues.items()
            },
            "cells": [asdict(cell) for cell in evidence.cells],
            "points": [asdict(point) for point in evidence.points],
        },
        ensure_ascii=False,
    )


class SqliteRoundRepository:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS rounds (
                id TEXT PRIMARY KEY, started_at TEXT NOT NULL, snapshot TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS observations (
                frame_id TEXT PRIMARY KEY, round_id TEXT NOT NULL REFERENCES rounds(id),
                captured_at TEXT NOT NULL, source TEXT NOT NULL, expert TEXT NOT NULL,
                model_version TEXT NOT NULL, novelty REAL NOT NULL, elapsed_ms REAL NOT NULL,
                evidence TEXT NOT NULL, raw_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS observations_round ON observations(round_id);
            CREATE TABLE IF NOT EXISTS supplementary_observations (
                frame_id TEXT NOT NULL REFERENCES observations(frame_id), expert TEXT NOT NULL,
                model_version TEXT NOT NULL, elapsed_ms REAL NOT NULL, evidence TEXT NOT NULL,
                raw_json TEXT NOT NULL, weight REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (frame_id, expert)
            );
            CREATE TABLE IF NOT EXISTS ground_truth (
                round_id TEXT PRIMARY KEY REFERENCES rounds(id),
                latitude REAL NOT NULL, longitude REAL NOT NULL,
                country TEXT NOT NULL, region TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
        """)

    def start(self, snapshot: RoundSnapshot) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO rounds VALUES (?, ?, ?)",
                (snapshot.id, datetime.now(UTC).isoformat(), snapshot_json(snapshot)),
            )

    def append(self, snapshot: RoundSnapshot, frame: Frame, evidence: GeoEvidence, novelty: float) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    frame.id,
                    snapshot.id,
                    frame.captured_at,
                    frame.source,
                    evidence.source,
                    evidence.model_version,
                    novelty,
                    evidence.elapsed_ms,
                    evidence_json(evidence),
                    evidence.raw_json,
                ),
            )
            self.connection.execute(
                "UPDATE rounds SET snapshot = ? WHERE id = ?", (snapshot_json(snapshot), snapshot.id)
            )

    def close(self):
        self.connection.close()

    def label(self, round_id: str, truth: GroundTruth) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connection:
            self.connection.execute(
                "INSERT INTO ground_truth VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(round_id) DO UPDATE SET latitude=excluded.latitude, longitude=excluded.longitude, "
                "country=excluded.country, region=excluded.region, updated_at=excluded.updated_at",
                (round_id, truth.latitude, truth.longitude, truth.country, truth.region, now, now),
            )

    def refine(self, snapshot: RoundSnapshot, frame: Frame, evidence: GeoEvidence) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE observations SET evidence=?, raw_json=?, elapsed_ms=? WHERE frame_id=? AND round_id=?",
                (evidence_json(evidence), evidence.raw_json, evidence.elapsed_ms, frame.id, snapshot.id),
            )
            self.connection.execute(
                "UPDATE rounds SET snapshot=? WHERE id=?", (snapshot_json(snapshot), snapshot.id)
            )

    def supplement(self, snapshot: RoundSnapshot, frame: Frame, evidence: GeoEvidence, weight: float) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO supplementary_observations VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    frame.id,
                    evidence.source,
                    evidence.model_version,
                    evidence.elapsed_ms,
                    evidence_json(evidence),
                    evidence.raw_json,
                    weight,
                ),
            )
            self.connection.execute(
                "UPDATE rounds SET snapshot = ? WHERE id = ?", (snapshot_json(snapshot), snapshot.id)
            )

    def disable_supplements(self, snapshot: RoundSnapshot) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE supplementary_observations SET active = 0 WHERE frame_id IN "
                "(SELECT frame_id FROM observations WHERE round_id = ?)",
                (snapshot.id,),
            )
            self.connection.execute(
                "UPDATE rounds SET snapshot = ? WHERE id = ?", (snapshot_json(snapshot), snapshot.id)
            )
