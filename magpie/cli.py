import pathlib
from typing import Optional

import typer

from magpie.db import DB_PATH
from magpie.indexer import INDEX_COMMIT_INTERVAL, run_index
from magpie.drives import drives_app
from magpie.dupes import dupes_app
from magpie.gdrive_cli import gdrive_app
from magpie.dropbox_cli import dropbox_app

faces_app = typer.Typer(
    name="faces",
    help="Face detection output: labeling helpers and automated matching.",
    no_args_is_help=True,
)

app = typer.Typer(
    name="magpie",
    help="Magpie — index, analyze, and prune files across drives.",
    no_args_is_help=True,
)

analyze_app = typer.Typer(
    name="analyze",
    help="Analyze file content — local offline mode (Sumy / Ollama).",
    no_args_is_help=True,
)

app.add_typer(drives_app, name="drives")
app.add_typer(dupes_app, name="dupes")
app.add_typer(analyze_app, name="analyze")
app.add_typer(faces_app, name="faces")
app.add_typer(gdrive_app, name="gdrive")
app.add_typer(dropbox_app, name="dropbox")


@app.callback()
def main() -> None:
    pass


@app.command()
def version() -> None:
    """Print the current version."""
    typer.echo("magpie 0.1.0")


@app.command()
def configure() -> None:
    """Configure AWS S3 credentials for cold storage uploads."""
    from magpie.config import get_config_path, save_aws_config

    typer.echo("AWS S3 Configuration for Cold Storage")
    typer.echo("=" * 50)
    typer.echo()

    profile = typer.prompt("AWS credential profile", default="magpie")
    region = typer.prompt("AWS region", default="us-east-1")
    bucket = typer.prompt("S3 bucket name")

    save_aws_config(profile=profile, region=region, bucket=bucket)
    config_path = get_config_path()
    typer.echo(f"\nConfiguration saved to {config_path}")
    typer.echo()
    typer.echo("Verify AWS credentials are available:")
    typer.echo(f"  aws configure --profile {profile}")
    typer.echo(f"  OR: export AWS_PROFILE={profile}")


@app.command()
def index(
    path: pathlib.Path = typer.Argument(..., help="Directory to index."),
    drive_id: Optional[str] = typer.Option(
        None, "--drive-id", "-d",
        help="Drive ID to associate files with. Auto-detected if omitted.",
    ),
    db: pathlib.Path = typer.Option(DB_PATH, help="Path to the magpie database."),
    no_thumbs: bool = typer.Option(
        False, "--no-thumbs",
        help="Skip generating 256px thumbnails for image files.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Recompute pHash and regenerate thumbnails even if already present.",
    ),
    no_prescan: bool = typer.Option(
        False, "--no-prescan",
        help="Skip the initial tree walk that counts files and total size (faster start, progress shows ? for totals).",
    ),
    commit_every: int = typer.Option(
        INDEX_COMMIT_INTERVAL,
        "--commit-every",
        min=0,
        help="Commit to SQLite after this many files (skip or full index). 0 = only commit at end of the run. Lower values survive interrupts with less lost work; slightly more I/O.",
    ),
    keep_mac_junk: bool = typer.Option(
        False, "--keep-mac-junk",
        help="Skip auto-labeling macOS junk files (resource forks, .DS_Store, etc.) as trash. By default they are labeled trash automatically.",
    ),
    prune_missing: bool = typer.Option(
        False, "--prune-missing",
        help="Delete records for files marked missing (not seen in this scan) from the database.",
    ),
    partial_hash_above_mib: int = typer.Option(
        64,
        "--partial-hash-above-mib",
        min=0,
        help=(
            "Files larger than this many MiB use a fast partial BLAKE3 fingerprint (b3p:); "
            "smaller files use full-file BLAKE3 (b3f:). Use 0 to always hash the entire file."
        ),
    ),
) -> None:
    """Walk PATH and populate the files table. Safe to re-run (idempotent)."""
    run_index(
        path,
        drive_id,
        db,
        with_thumbs=not no_thumbs,
        force=force,
        no_prescan=no_prescan,
        commit_every=commit_every,
        label_mac_junk=not keep_mac_junk,
        prune_missing=prune_missing,
        partial_hash_above_mib=partial_hash_above_mib,
    )


@app.command()
def thumbs(
    path: pathlib.Path = typer.Argument(..., help="Directory of already-indexed files."),
    db: pathlib.Path = typer.Option(DB_PATH, help="Path to the magpie database."),
    no_thumbs: bool = typer.Option(
        False, "--no-thumbs",
        help="Only compute pHashes; skip thumbnail generation.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Recompute pHash and regenerate thumbnails even if already present.",
    ),
) -> None:
    """Compute pHashes and generate thumbnails for already-indexed images under PATH."""
    from magpie.thumbs import run_thumbs
    run_thumbs(path, db, with_thumbs=not no_thumbs, force=force)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind host"),
    port: int = typer.Option(8000, help="Bind port"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes"),
) -> None:
    """Start the web UI at http://localhost:8000/."""
    import uvicorn

    typer.echo(f"Starting Magpie UI at http://{host}:{port}/")
    uvicorn.run(
        "magpie.ui.app:app",
        host=host,
        port=port,
        reload=reload,
    )


@app.command("execute")
def execute(
    path: Optional[pathlib.Path] = typer.Argument(
        None,
        help="Directory path to limit executor to that drive. If omitted, processes all drives.",
    ),
    action: str = typer.Option(
        ...,
        "--action",
        "-a",
        help="Queue to process: trash | hot | cold (matches approved labels).",
    ),
    dropbox_path: Optional[pathlib.Path] = typer.Option(
        None,
        "--dropbox-path",
        help="Local Dropbox sync root (e.g. ~/Dropbox). Required to upload Dropbox-sourced files.",
    ),
    source: Optional[str] = typer.Option(
        None,
        "--source",
        help="Limit to a specific drive source: local | dropbox.",
    ),
    db: pathlib.Path = typer.Option(DB_PATH, "--db", help="Path to the magpie database."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run/--no-dry-run",
        help="Dry-run: report planned work without executing. (default: execute)",
    ),
) -> None:
    """Run the executor on approved labels for one destination (explicit user action)."""
    from magpie.executor import run_execute

    def _line(msg: str) -> None:
        typer.echo(msg)

    code = run_execute(db_path=db, cli_action=action, path=path, dry_run=dry_run, line_print=_line, dropbox_root=dropbox_path, source=source)
    raise typer.Exit(code)


@analyze_app.command("docs")
def analyze_docs(
    path: pathlib.Path = typer.Argument(..., help="Directory containing documents to analyze."),
    db: pathlib.Path = typer.Option(DB_PATH, help="Path to the magpie database."),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Print what would be analyzed without writing to the database.",
    ),
    limit: Optional[int] = typer.Option(
        None, "--limit", "-n",
        help="Maximum number of files to process.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Re-analyze files that already have analysis results.",
    ),
    sumy: bool = typer.Option(
        False, "--sumy",
        help="Use Sumy LSA summarizer (analyzer=local-sumy). Offline, extractive, no Ollama needed.",
    ),
    ollama_model: Optional[str] = typer.Option(
        None, "--ollama-model",
        help="Ollama text model for local analysis (default: llama2). Ignored with --sumy.",
    ),
) -> None:
    """Analyze documents (PDF, DOCX, TXT, MD) under PATH.

    Default: Ollama + Llama2 (analyzer=local-ollama). Offline, requires running Ollama daemon.

    With --sumy: Sumy LSA extractive summary + keyword tags (analyzer=local-sumy), offline, no Ollama needed.

    With --ollama-model MODEL: Use a different Ollama model (e.g., mistral, neural-chat, openhermes).
    """
    from magpie.analyzer import run_analyze_docs

    run_analyze_docs(
        path,
        db_path=db,
        dry_run=dry_run,
        limit=limit,
        force=force,
        sumy=sumy,
        ollama_model=ollama_model,
    )


@analyze_app.command("photos")
def analyze_photos(
    path: pathlib.Path = typer.Argument(..., help="Directory containing images to analyze (e.g., /gdrive/My Drive or /local/path)."),
    db: pathlib.Path = typer.Option(DB_PATH, help="Path to the magpie database."),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="List images that would be analyzed without writing to the database or calling any model.",
    ),
    limit: Optional[int] = typer.Option(
        None, "--limit", "-n",
        help="Maximum number of files to process.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Re-analyze files that already have results.",
    ),
    ollama_model: str = typer.Option(
        "llava", "--ollama-model",
        help="Ollama model for local vision analysis (e.g. llava, llava:13b).",
    ),
    drive_id: Optional[str] = typer.Option(
        None, "--drive-id",
        help="Limit analysis to a specific drive UUID (optional).",
    ),
) -> None:
    """Caption and tag images under PATH (JPEG, PNG, WebP, HEIC, RAW, etc.).

    Uses Ollama + LLaVA-class model (analyzer=local-llava). Images are preprocessed to JPEG.
    Requires a running Ollama daemon unless --dry-run.

    For Google Drive paths (/gdrive/...): files are downloaded to local cache before analysis.
    """
    from magpie.photo_analyzer import run_analyze_photos

    gdrive_service = None
    if str(path).startswith("/gdrive/"):
        from magpie.gdrive_auth import get_credentials
        from googleapiclient.discovery import build
        try:
            creds = get_credentials()
            gdrive_service = build("drive", "v3", credentials=creds)
        except Exception as e:
            typer.echo(f"Error: Failed to authenticate with Google Drive: {e}", err=True)
            raise SystemExit(1)

    run_analyze_photos(
        path,
        db_path=db,
        dry_run=dry_run,
        limit=limit,
        force=force,
        ollama_model=ollama_model,
        drive_id=drive_id,
        gdrive_service=gdrive_service,
    )


@analyze_app.command("faces")
def analyze_faces(
    path: pathlib.Path = typer.Argument(..., help="Directory of indexed image files."),
    db: pathlib.Path = typer.Option(DB_PATH, help="Path to the magpie database."),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="List images that would be processed without running detection or writing.",
    ),
    limit: Optional[int] = typer.Option(
        None, "--limit", "-n",
        help="Maximum number of image files to process.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Re-run face detection even if this file was already processed.",
    ),
) -> None:
    """Detect faces and store 512-d embeddings (InsightFace buffalo_l / ONNX). Skips processed files unless --force.

    Uses InsightFace buffalo_l with CoreML execution provider on Apple Silicon (Metal GPU).
    The model (~300 MB) is downloaded automatically on first run to ~/.insightface/.
    """
    from magpie.face_analyzer import run_analyze_faces

    run_analyze_faces(path, db_path=db, dry_run=dry_run, limit=limit, force=force)


@analyze_app.command("audio")
def analyze_audio(
    path: pathlib.Path = typer.Argument(..., help="Directory of already-indexed audio files."),
    db: pathlib.Path = typer.Option(DB_PATH, help="Path to the magpie database."),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="List files that would be processed without writing to the database.",
    ),
    limit: Optional[int] = typer.Option(
        None, "--limit", "-n",
        help="Maximum number of files to process.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Re-extract metadata even for files already processed.",
    ),
) -> None:
    """Extract metadata (title, artist, album, duration, etc.) from indexed audio files under PATH.

    Reads already-indexed audio files (MP3, M4A, FLAC, WAV, AAC, OGG, AIFF) and stores
    metadata in the file_metadata table. Embedded cover art is saved as a thumbnail.
    Skips files already processed unless --force.
    """
    from magpie.audio_analyzer import run_analyze_audio

    run_analyze_audio(path, db_path=db, dry_run=dry_run, limit=limit, force=force)


@analyze_app.command("compare")
def analyze_compare(
    path: pathlib.Path = typer.Argument(..., help="Indexed directory to sample files from."),
    db: pathlib.Path = typer.Option(DB_PATH, help="Path to the magpie database."),
    limit: int = typer.Option(
        10, "--limit", "-n",
        help="Number of files to compare (same files run through both pipelines).",
    ),
    photos: bool = typer.Option(
        False, "--photos",
        help="Compare image captioning between two Ollama vision models (--photos) instead of documents (Ollama vs Sumy).",
    ),
    model1: str = typer.Option(
        "llava:7b", "--model1",
        help="First Ollama model for photo comparison (photos mode only).",
    ),
    model2: str = typer.Option(
        "gemma4:e4b", "--model2",
        help="Second Ollama model for photo comparison (photos mode only).",
    ),
    min_size_mb: float = typer.Option(
        0.0, "--min-size-mb",
        help="(Photos only) Only compare images larger than this size in MB. Useful to filter out icons/thumbnails.",
    ),
) -> None:
    """Run analysis on the same files with two different models and print side-by-side results.

    Does not write to the database.

    Document mode (default): Ollama vs Sumy LSA. Requires Ollama running.

    Photo mode (--photos): Compares two Ollama vision models side-by-side.
    Requires Ollama running with the specified models (e.g. llava, llava:13b).
    """
    from magpie.analyzer import run_analyze_compare

    run_analyze_compare(
        path,
        db_path=db,
        limit=limit,
        photos=photos,
        model1=model1,
        model2=model2,
        min_size_mb=min_size_mb,
    )


@faces_app.command("match")
def faces_match(
    db: pathlib.Path = typer.Option(DB_PATH, "--db", help="Path to the magpie database."),
    threshold: float = typer.Option(
        1.0,
        "--threshold",
        help=(
            "Maximum Euclidean distance (512-d InsightFace embedding) for an auto-match. "
            "Values between 0.8–1.2 are reasonable starting points."
        ),
    ),
) -> None:
    """Assign person_id to unidentified faces from the nearest labeled prototype."""
    from magpie.face_matcher import run_face_match_cli

    def _line(msg: str) -> None:
        typer.echo(msg)

    code = run_face_match_cli(db_path=db, threshold=threshold, line_print=_line)
    raise typer.Exit(code)


@faces_app.command("cluster")
def faces_cluster(
    db: pathlib.Path = typer.Option(DB_PATH, "--db", help="Path to the magpie database."),
    match_threshold: float = typer.Option(
        1.0,
        "--match-threshold",
        help="Skip faces within this distance (already matched by faces match). Default: 1.0",
    ),
    suggest_threshold: float = typer.Option(
        1.4,
        "--suggest-threshold",
        help="Create suggestions for faces within this distance from a labeled person. Default: 1.4",
    ),
    cluster_threshold: float = typer.Option(
        1.0,
        "--cluster-threshold",
        help="Group unlabeled faces within this distance into clusters. Default: 1.0",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Clear old cluster_id and face_suggestions before re-running.",
    ),
) -> None:
    """Group unlabeled faces by similarity and compute suggestions for labeled people.

    1. Computes per-person centroids from labeled faces.
    2. For each unlabeled face, finds nearest person centroid:
       - distance <= match_threshold: skip (already matched by faces match)
       - match_threshold < distance <= suggest_threshold: stores as a suggestion
       - distance > suggest_threshold: goes to clustering
    3. Single-linkage clusters remaining unlabeled faces by distance <= cluster_threshold.
    """
    from magpie.face_clusterer import run_face_cluster_cli

    def _line(msg: str) -> None:
        typer.echo(msg)

    code = run_face_cluster_cli(
        db_path=db,
        match_threshold=match_threshold,
        suggest_threshold=suggest_threshold,
        cluster_threshold=cluster_threshold,
        force=force,
        line_print=_line,
    )
    raise typer.Exit(code)


if __name__ == "__main__":
    app()
