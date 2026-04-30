"""Tests for face clustering."""

from __future__ import annotations

import pathlib
import struct
import uuid

import pytest
import sqlite3

from db.migrate import migrate
from magpie.face_clusterer import run_face_cluster


def _dummy_embedding_blob() -> bytes:
    """Create a dummy 512-float32 embedding."""
    return struct.pack("<512f", *([0.1] * 512))


def _embedding_blob_offset(offset: float) -> bytes:
    """Create a dummy embedding with specified offset in first dimension."""
    emb = [0.1] * 512
    emb[0] += offset
    return struct.pack(f"<512f", *emb)


@pytest.fixture()
def tmp_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db_path = tmp_path / "magpie.db"
    migrate(db_path)
    return db_path


def test_cluster_only_creates_2plus_member_clusters(tmp_db: pathlib.Path) -> None:
    """Test that clustering only creates clusters with 2+ members."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys=ON")

    # Create 3 files with unlabeled faces
    file_ids = [str(uuid.uuid4()) for _ in range(3)]
    for fid in file_ids:
        conn.execute(
            """
            INSERT INTO files (id, path, indexed_at, mime_type, extension, status, size_bytes)
            VALUES (?, ?, datetime('now'), 'image/jpeg', 'jpg', 'present', 100)
            """,
            (fid, f"/file_{fid}.jpg"),
        )

    # Create face labels: 2 similar faces + 1 very different face
    # Face 0 and Face 1 are similar (within threshold), Face 2 is far away
    conn.execute(
        """
        INSERT INTO face_labels (
            id, file_id, person_id, face_index, bbox, embedding, source, confidence, labeled_at
        ) VALUES (?, ?, NULL, 0, '[0,0,10,10]', ?, 'auto', NULL, datetime('now'))
        """,
        (str(uuid.uuid4()), file_ids[0], _embedding_blob_offset(0.0)),
    )
    conn.execute(
        """
        INSERT INTO face_labels (
            id, file_id, person_id, face_index, bbox, embedding, source, confidence, labeled_at
        ) VALUES (?, ?, NULL, 0, '[0,0,10,10]', ?, 'auto', NULL, datetime('now'))
        """,
        (str(uuid.uuid4()), file_ids[1], _embedding_blob_offset(0.05)),  # Similar to face 0
    )
    conn.execute(
        """
        INSERT INTO face_labels (
            id, file_id, person_id, face_index, bbox, embedding, source, confidence, labeled_at
        ) VALUES (?, ?, NULL, 0, '[0,0,10,10]', ?, 'auto', NULL, datetime('now'))
        """,
        (str(uuid.uuid4()), file_ids[2], _embedding_blob_offset(5.0)),  # Very different
    )
    conn.commit()
    conn.close()

    # Run clustering with a tight threshold
    result = run_face_cluster(tmp_db, cluster_threshold=0.5)

    # Verify: should have 1 cluster (the 2 similar faces), not 3 single-face clusters
    conn2 = sqlite3.connect(tmp_db)
    clusters = conn2.execute(
        "SELECT DISTINCT cluster_id FROM face_labels WHERE cluster_id IS NOT NULL"
    ).fetchall()
    conn2.close()

    # Should have exactly 1 cluster
    assert len(clusters) == 1
    assert result.clusters_created == 1


def test_cluster_filters_out_single_face_clusters(tmp_db: pathlib.Path) -> None:
    """Test that single-face clusters are not created."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys=ON")

    # Create 5 files with unlabeled faces, all very far apart
    file_ids = [str(uuid.uuid4()) for _ in range(5)]
    for i, fid in enumerate(file_ids):
        conn.execute(
            """
            INSERT INTO files (id, path, indexed_at, mime_type, extension, status, size_bytes)
            VALUES (?, ?, datetime('now'), 'image/jpeg', 'jpg', 'present', 100)
            """,
            (fid, f"/file_{i}.jpg"),
        )
        # Create very different embeddings so none cluster together
        conn.execute(
            """
            INSERT INTO face_labels (
                id, file_id, person_id, face_index, bbox, embedding, source, confidence, labeled_at
            ) VALUES (?, ?, NULL, 0, '[0,0,10,10]', ?, 'auto', NULL, datetime('now'))
            """,
            (str(uuid.uuid4()), fid, _embedding_blob_offset(float(i * 10))),
        )
    conn.commit()
    conn.close()

    # Run clustering with tight threshold - nothing should cluster
    result = run_face_cluster(tmp_db, cluster_threshold=0.5)

    # Verify: should have 0 clusters (all are isolated)
    conn2 = sqlite3.connect(tmp_db)
    clusters = conn2.execute(
        "SELECT COUNT(DISTINCT cluster_id) FROM face_labels WHERE cluster_id IS NOT NULL"
    ).fetchone()
    conn2.close()

    # Should have no clusters
    assert clusters[0] == 0
    assert result.clusters_created == 0
