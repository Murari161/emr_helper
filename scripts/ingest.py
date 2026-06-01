"""CLI entry point for ingesting .docx user manuals into Postgres.

Usage (from inside the app container):

    # Ingest one file:
    python -m scripts.ingest /data/knowledge_base/Patient_Management_cleaned.docx

    # Ingest every .docx in a folder:
    python -m scripts.ingest /data/knowledge_base/

    # Pass an explicit manual version (default: "v1"):
    python -m scripts.ingest --version v2 /data/knowledge_base/Doctor_Module.docx

Behavior:
  - Accepts either a single .docx path or a directory containing them.
  - For each file: load → extract images → chunk → embed → insert.
  - Idempotent for the same (doc_title, manual_version): old chunks are
    marked active=false before the new ones are inserted (history preserved).
  - On error: the document's transaction rolls back; the script exits non-zero.
  - Progress is logged to stderr so stdout stays clean for piping.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from app.config import settings
from app.ingestion.chunk import chunk_document
from app.ingestion.images import extract_images
from app.ingestion.index import index_chunks
from app.ingestion.load import load_docx


def _setup_logging() -> None:
    logging.basicConfig(
        level=settings.app_log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


async def ingest_one(path: Path, manual_version: str) -> None:
    """Ingest a single .docx file end-to-end."""
    logging.info("=== Ingesting %s ===", path)
    doc = load_docx(path)
    images = extract_images(doc, settings.images_dir)
    chunks = chunk_document(doc, images)

    if not chunks:
        logging.warning("No chunks produced for %s — nothing to index", path)
        return

    doc_id = await index_chunks(
        chunks,
        doc_title=doc.doc_title,
        manual_version=manual_version,
        source_path=str(path),
    )
    logging.info("Done %s -> document %s (%d chunks)", path.name, doc_id, len(chunks))


def _discover(path: Path) -> list[Path]:
    """Resolve `path` to a sorted list of .docx files."""
    if path.is_file():
        if path.suffix.lower() != ".docx":
            raise SystemExit(f"Expected a .docx file, got: {path}")
        return [path]
    if path.is_dir():
        files = sorted(path.glob("*.docx"))
        if not files:
            raise SystemExit(f"No .docx files found under: {path}")
        return files
    raise SystemExit(f"Path does not exist: {path}")


async def _main_async(paths: list[Path], manual_version: str) -> int:
    failures: list[Path] = []
    for p in paths:
        try:
            await ingest_one(p, manual_version)
        except Exception:  # noqa: BLE001 — top-level safety net
            logging.exception("Failed to ingest %s", p)
            failures.append(p)
    if failures:
        logging.error("Ingestion finished with %d failure(s): %s", len(failures), failures)
        return 1
    logging.info("All %d document(s) ingested successfully.", len(paths))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest .docx user manuals into Postgres.")
    parser.add_argument("path", type=Path, help="A .docx file or a folder containing .docx files.")
    parser.add_argument(
        "--version",
        default="v1",
        help="Manual version tag stored in documents.manual_version (default: v1).",
    )
    args = parser.parse_args()

    _setup_logging()
    paths = _discover(args.path)
    logging.info("Discovered %d .docx file(s) to ingest", len(paths))
    rc = asyncio.run(_main_async(paths, args.version))
    sys.exit(rc)


if __name__ == "__main__":
    main()
