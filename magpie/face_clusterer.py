"""Face clustering: group unlabeled faces by similarity and compute suggestions for labeled people."""

from __future__ import annotations

import pathlib
import statistics
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import numpy as np
from rich.console import Console
from rich.table import Table

from magpie.db import get_connection

console = Console()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def blob_to_embedding(blob: bytes) -> np.ndarray:
    """Decode face_labels.embedding BLOB to float32 vector (512,)."""
    return np.frombuffer(blob, dtype=np.float32).reshape(512).copy()


@dataclass
class FaceClusterResult:
    clusters_created: int
    suggestions_created: int
    unlabeled_count: int


def run_face_cluster(
    db_path: pathlib.Path,
    *,
    match_threshold: float = 1.0,
    suggest_threshold: float = 1.4,
    cluster_threshold: float = 1.0,
    force: bool = False,
) -> FaceClusterResult:
    """Cluster unlabeled faces and compute suggestions for labeled people.

    1. Load all labeled face embeddings, compute per-person centroids.
    2. For each unlabeled face, find nearest person centroid:
       - distance <= match_threshold: skip (already matched by faces match)
       - match_threshold < distance <= suggest_threshold: add to face_suggestions
       - distance > suggest_threshold: go to clustering step
    3. Single-linkage cluster remaining unlabeled faces by distance <= cluster_threshold.
    4. Filter out single-face clusters; only clusters with 2+ members are kept.

    Args:
        db_path: Path to magpie.db
        match_threshold: Skip faces within this distance (already matched by faces match)
        suggest_threshold: Create suggestions for faces within this distance
        cluster_threshold: Cluster unlabeled faces within this distance
        force: If True, clear old cluster_id and face_suggestions before re-running
    """
    if match_threshold <= 0 or suggest_threshold <= 0 or cluster_threshold <= 0:
        raise ValueError("all thresholds must be positive")

    with get_connection(db_path) as conn:
        # Clear old data if force=True
        if force:
            conn.execute("DELETE FROM face_suggestions")
            conn.execute("UPDATE face_labels SET cluster_id = NULL WHERE person_id IS NULL")
            conn.commit()

        # Sanity check: clear cluster_id from any face that has a person_id
        # (labeled faces should never be in a cluster)
        conn.execute("UPDATE face_labels SET cluster_id = NULL WHERE person_id IS NOT NULL AND cluster_id IS NOT NULL")
        conn.commit()

        # Load labeled faces grouped by person_id
        labeled = conn.execute(
            """
            SELECT fl.file_id, fl.face_index, fl.person_id, fl.embedding
            FROM face_labels fl
            WHERE fl.person_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM labels l
                  WHERE l.file_id = fl.file_id AND l.action = 'trash'
              )
            """
        ).fetchall()

        # Load unlabeled faces
        unlabeled = conn.execute(
            """
            SELECT fl.file_id, fl.face_index, fl.id, fl.embedding
            FROM face_labels fl
            WHERE fl.person_id IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM labels l
                  WHERE l.file_id = fl.file_id AND l.action = 'trash'
              )
            """
        ).fetchall()

        if not unlabeled:
            return FaceClusterResult(
                clusters_created=0,
                suggestions_created=0,
                unlabeled_count=0,
            )

        # Group labeled embeddings by person_id and compute centroids
        person_centroids = {}
        if labeled:
            by_person = {}
            for row in labeled:
                pid = str(row["person_id"])
                if pid not in by_person:
                    by_person[pid] = []
                by_person[pid].append(blob_to_embedding(row["embedding"]))

            for pid, embeddings in by_person.items():
                person_centroids[pid] = np.mean(embeddings, axis=0)

        # Step 1: Find suggestions; ALL non-matched faces go to clustering
        now = _now_iso()
        suggestions_to_insert = []
        remaining_unlabeled = []

        for row in unlabeled:
            file_id = str(row["file_id"])
            face_index = int(row["face_index"])
            face_label_id = str(row["id"])
            emb = blob_to_embedding(row["embedding"])

            # All unlabeled faces go to clustering pool
            remaining_unlabeled.append((file_id, face_index, face_label_id, emb))

            if not person_centroids:
                # No labeled faces to suggest against
                continue

            # Find nearest person centroid for suggestions
            dists = [
                np.linalg.norm(emb - centroid)
                for centroid in person_centroids.values()
            ]
            min_dist = min(dists)
            nearest_pid = list(person_centroids.keys())[np.argmin(dists)]

            if min_dist <= match_threshold:
                # Already matched, skip suggestion
                pass
            elif min_dist <= suggest_threshold:
                # Create suggestion (but still cluster against other unlabeled faces)
                suggestions_to_insert.append((file_id, face_index, nearest_pid, min_dist, now))

        # Insert suggestions
        for file_id, face_index, person_id, distance, created_at in suggestions_to_insert:
            conn.execute(
                """
                INSERT OR IGNORE INTO face_suggestions (file_id, face_index, person_id, distance, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (file_id, face_index, person_id, distance, created_at),
            )

        # Step 2: Single-linkage clustering of remaining unlabeled faces
        clusters_created = 0

        if remaining_unlabeled:
            # Build pairwise distance matrix
            embeddings = np.stack([emb for _, _, _, emb in remaining_unlabeled], axis=0)
            num_faces = len(remaining_unlabeled)

            # Single-linkage clustering: greedily assign to existing cluster
            cluster_ids = {}  # row_idx -> cluster_uuid
            cluster_members = {}  # cluster_uuid -> [row_indices]

            for i in range(num_faces):
                assigned = False

                # Try to assign to an existing cluster
                for cluster_id, members in cluster_members.items():
                    # Check if this face is within threshold of any member
                    for member_idx in members:
                        dist = float(np.linalg.norm(embeddings[i] - embeddings[member_idx]))
                        if dist <= cluster_threshold:
                            cluster_ids[i] = cluster_id
                            members.append(i)
                            assigned = True
                            break

                    if assigned:
                        break

                # If not assigned, create new cluster
                if not assigned:
                    new_cluster_id = str(uuid.uuid4())
                    cluster_ids[i] = new_cluster_id
                    cluster_members[new_cluster_id] = [i]
                    clusters_created += 1

            # Filter out single-face clusters; only keep clusters with 2+ members
            valid_cluster_ids = {
                cluster_id for cluster_id, members in cluster_members.items()
                if len(members) >= 2
            }

            # Write cluster_id back to face_labels (only for valid clusters)
            clusters_created = len(valid_cluster_ids)
            for i, (file_id, face_index, face_label_id, _) in enumerate(remaining_unlabeled):
                cluster_id = cluster_ids[i]
                if cluster_id in valid_cluster_ids:
                    conn.execute(
                        """
                        UPDATE face_labels
                        SET cluster_id = ?
                        WHERE id = ?
                        """,
                        (cluster_id, face_label_id),
                    )

        conn.commit()

    return FaceClusterResult(
        clusters_created=clusters_created,
        suggestions_created=len(suggestions_to_insert),
        unlabeled_count=len(unlabeled),
    )


def _print_summary(result: FaceClusterResult) -> None:
    table = Table(title="Face cluster summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")

    table.add_row("Unlabeled faces processed", str(result.unlabeled_count))
    table.add_row("Clusters created", str(result.clusters_created))
    table.add_row("Suggestions created", str(result.suggestions_created))

    console.print(table)


def run_face_cluster_cli(
    db_path: pathlib.Path,
    match_threshold: float = 1.0,
    suggest_threshold: float = 1.4,
    cluster_threshold: float = 1.0,
    force: bool = False,
    *,
    line_print: Optional[Callable[[str], None]] = None,
) -> int:
    """CLI entry: run clustering and print summary. Returns process exit code."""
    line = line_print or (lambda s: console.print(s))

    try:
        result = run_face_cluster(
            db_path,
            match_threshold=match_threshold,
            suggest_threshold=suggest_threshold,
            cluster_threshold=cluster_threshold,
            force=force,
        )
    except ValueError as exc:
        line(str(exc))
        return 2

    _print_summary(result)
    return 0


__all__ = [
    "FaceClusterResult",
    "blob_to_embedding",
    "run_face_cluster",
    "run_face_cluster_cli",
]
