"""Automated face matching: nearest labeled prototype by Euclidean distance on 512-d embeddings."""

from __future__ import annotations

import pathlib
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import numpy as np
from rich.console import Console
from rich.table import Table

from magpie.db import get_connection

console = Console()

NO_LABELED_MSG = "No labeled faces found — label some faces in the UI first"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def blob_to_embedding(blob: bytes) -> np.ndarray:
    """Decode face_labels.embedding BLOB to float32 vector (512,)."""
    return np.frombuffer(blob, dtype=np.float32).reshape(512).copy()


@dataclass
class FaceMatchResult:
    matched: int
    left_unidentified: int
    confidences: list[float]
    no_labeled_prototypes: bool


def run_face_match(
    db_path: pathlib.Path,
    *,
    threshold: float = 1.0,
) -> FaceMatchResult:
    """Match unidentified faces to the nearest labeled face within *threshold* (Euclidean).

    Updates matching rows: person_id, source='auto', confidence=1 - distance/threshold,
    labeled_at=now.

    Threshold is Euclidean distance in 512-d InsightFace embedding space.
    Values between 0.8–1.2 are reasonable starting points; default is 1.0.
    """
    if threshold <= 0:
        raise ValueError("threshold must be positive")

    with get_connection(db_path) as conn:
        labeled = conn.execute(
            """
            SELECT fl.id, fl.person_id, fl.embedding
            FROM face_labels fl
            WHERE fl.person_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM labels l
                  WHERE l.file_id = fl.file_id AND l.action = 'trash'
              )
            """
        ).fetchall()

        if not labeled:
            return FaceMatchResult(
                matched=0,
                left_unidentified=0,
                confidences=[],
                no_labeled_prototypes=True,
            )

        unlabeled = conn.execute(
            """
            SELECT fl.id, fl.embedding
            FROM face_labels fl
            WHERE fl.person_id IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM labels l
                  WHERE l.file_id = fl.file_id AND l.action = 'trash'
              )
            """
        ).fetchall()

        if not unlabeled:
            return FaceMatchResult(
                matched=0,
                left_unidentified=0,
                confidences=[],
                no_labeled_prototypes=False,
            )

        L = np.stack([blob_to_embedding(r["embedding"]) for r in labeled], axis=0)
        U = np.stack([blob_to_embedding(r["embedding"]) for r in unlabeled], axis=0)
        person_ids = [str(r["person_id"]) for r in labeled]
        unlabeled_ids = [str(r["id"]) for r in unlabeled]

        # (num_unlabeled, num_labeled)
        dists = np.linalg.norm(U[:, None, :] - L[None, :, :], axis=2)
        best_j = np.argmin(dists, axis=1)
        best_d = dists[np.arange(U.shape[0]), best_j]

        now = _now_iso()
        confidences: list[float] = []
        matched = 0

        for i in range(U.shape[0]):
            d = float(best_d[i])
            if d <= threshold:
                pid = person_ids[int(best_j[i])]
                conf = 1.0 - (d / threshold)
                conn.execute(
                    """
                    UPDATE face_labels
                    SET person_id = ?, source = 'auto', confidence = ?, labeled_at = ?
                    WHERE id = ?
                    """,
                    (pid, conf, now, unlabeled_ids[i]),
                )
                confidences.append(conf)
                matched += 1

        conn.commit()

    left = len(unlabeled) - matched
    return FaceMatchResult(
        matched=matched,
        left_unidentified=left,
        confidences=confidences,
        no_labeled_prototypes=False,
    )


def _print_summary(result: FaceMatchResult) -> None:
    table = Table(title="Face match summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")

    table.add_row("Faces matched", str(result.matched))
    table.add_row("Faces left unidentified", str(result.left_unidentified))

    conf = result.confidences
    if conf:
        table.add_row("Confidence min", f"{min(conf):.4f}")
        table.add_row("Confidence mean", f"{statistics.mean(conf):.4f}")
        table.add_row("Confidence max", f"{max(conf):.4f}")
    else:
        table.add_row("Confidence min", "—")
        table.add_row("Confidence mean", "—")
        table.add_row("Confidence max", "—")

    console.print(table)


def run_face_match_cli(
    db_path: pathlib.Path,
    threshold: float = 1.0,
    *,
    line_print: Optional[Callable[[str], None]] = None,
) -> int:
    """CLI entry: run matching and print summary. Returns process exit code."""
    line = line_print or (lambda s: console.print(s))

    try:
        result = run_face_match(db_path, threshold=threshold)
    except ValueError as exc:
        line(str(exc))
        return 2

    if result.no_labeled_prototypes:
        line(NO_LABELED_MSG)
        return 0

    _print_summary(result)
    return 0


__all__ = [
    "NO_LABELED_MSG",
    "FaceMatchResult",
    "blob_to_embedding",
    "run_face_match",
    "run_face_match_cli",
]
