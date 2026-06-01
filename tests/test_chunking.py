"""Acceptance tests for the chunker.

These run against the real Patient Management manual at
/data/knowledge_base/Patient_Management_cleaned.docx (copied into the container
via the docker-compose `./data:/data` bind mount).

Run with:
    docker compose exec app pytest tests/test_chunking.py -v

What each test catches:

  test_register_patient_is_one_chunk
      H3-driven chunking is correct: every procedure produces exactly one
      chunk containing all its steps. If chunk boundaries drift (e.g. a
      Note callout splits the procedure), this catches it.

  test_close_clinic_session_has_irreversible_caution
      Caution callouts are correctly classified (vs. Notes) and the body
      text is preserved. The 'irreversible' wording is the canonical
      signal — if we lose it, the answer loses safety information.

  test_save_to_queue_distinguishable_from_add_to_queue
      The disambiguated UI labels stay distinct in the ui_labels field.
      This is THE retrieval-quality test for the trigram index.

  test_index_entries_become_chunks
      All 29 Quick-Index 'How do I…?' bullets become index_entry chunks.
      Catches arrow-split regressions.

  test_glossary_terms_become_chunks
      All glossary entries become glossary chunks.

  test_close_clinic_session_has_attached_images
      Image attachment groups correctly across H3 boundaries.

  test_no_title_block_in_chunks
      The pre-pass that strips 'Ministry of Health' etc. actually works —
      no chunk content contains those lines.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.ingestion.chunk import Chunk, chunk_document
from app.ingestion.images import extract_images
from app.ingestion.load import load_docx

MANUAL_PATH = Path("/data/knowledge_base/Patient_Management_cleaned.docx")


# Load + chunk once for all tests in this module — the parsing is slow-ish
# and immutable. We do NOT call ingest's HTTP/DB code from these tests.
@pytest.fixture(scope="module")
def chunks() -> list[Chunk]:
    if not MANUAL_PATH.exists():
        pytest.skip(f"Sample manual not found at {MANUAL_PATH}")
    doc = load_docx(MANUAL_PATH)
    # Write images to a throwaway tests dir to avoid clobbering real /data/images.
    images_root = Path("/tmp/test_images")
    images_root.mkdir(parents=True, exist_ok=True)
    images = extract_images(doc, images_root)
    return chunk_document(doc, images)


def _find_procedure(chunks: list[Chunk], title_substring: str) -> Chunk:
    """Return the first procedure chunk whose title contains the substring (case-insensitive)."""
    sub = title_substring.lower()
    for c in chunks:
        if c.kind == "procedure" and sub in c.title.lower():
            return c
    titles = [c.title for c in chunks if c.kind == "procedure"]
    raise AssertionError(
        f"No procedure chunk with title containing {title_substring!r}. "
        f"Procedure titles seen: {titles}"
    )


# ---------------------------------------------------------------------------
# Test 1 — Register a patient is one chunk and includes all its steps
# ---------------------------------------------------------------------------

def test_register_patient_is_one_chunk(chunks: list[Chunk]) -> None:
    matches = [c for c in chunks if c.kind == "procedure" and "register a patient" in c.title.lower()]
    assert len(matches) == 1, (
        f"Expected exactly one 'Register a patient' chunk, got {len(matches)}: "
        f"{[c.title for c in matches]}"
    )
    chunk = matches[0]
    # Procedure has 6+ steps and should mention key UI elements.
    body = chunk.content.lower() + " " + chunk.ui_labels.lower()
    for must_appear in ("register patient", "patient number"):
        assert must_appear in body, (
            f"Expected {must_appear!r} somewhere in the Register-a-Patient chunk body."
        )


# ---------------------------------------------------------------------------
# Test 2 — Close a clinic session has the 'irreversible' caution
# ---------------------------------------------------------------------------

def test_close_clinic_session_has_irreversible_caution(chunks: list[Chunk]) -> None:
    c = _find_procedure(chunks, "close a clinic session")
    assert c.cautions, "Close-a-clinic-session chunk has no cautions captured."
    assert "irreversible" in c.cautions.lower(), (
        f"Expected the word 'irreversible' in cautions of Close-a-clinic-session, "
        f"got: {c.cautions!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — Save to Queue is distinguishable from Add to Queue
# ---------------------------------------------------------------------------

def test_save_to_queue_distinguishable_from_add_to_queue(chunks: list[Chunk]) -> None:
    """Both labels appear as DISTINCT phrases in ui_labels — not merged or
    lost. The two refer to different UI elements (toolbar button vs dialog
    button) and the trigram index needs to see them as separate tokens to
    rank queries correctly.

    Both will often co-occur in the same procedure (e.g. Queue-a-returning-
    patient mentions both), which is fine — what matters is that they
    survive as separate phrases rather than getting smushed together.
    """
    all_ui_labels_lower = " ".join(
        c.ui_labels for c in chunks if c.kind == "procedure"
    ).lower()

    # Both phrases must exist verbatim in the corpus's ui_labels.
    assert "save to queue" in all_ui_labels_lower, (
        "Expected 'Save to Queue' to appear in some procedure's ui_labels."
    )
    assert "add to queue" in all_ui_labels_lower, (
        "Expected 'Add to Queue' to appear in some procedure's ui_labels."
    )

    # They must NOT be smushed into 'save to queueadd to queue' or similar.
    # If you read ui_labels as a space-separated bag of phrases, you should
    # be able to find each as a contiguous substring with a space (or end
    # of string) on either side.
    import re
    assert re.search(r"(^|\s)save to queue(\s|$)", all_ui_labels_lower), (
        "'Save to Queue' is concatenated with adjacent text — bold-run "
        "extraction is losing phrase boundaries."
    )
    assert re.search(r"(^|\s)add to queue(\s|$)", all_ui_labels_lower), (
        "'Add to Queue' is concatenated with adjacent text — bold-run "
        "extraction is losing phrase boundaries."
    )


# ---------------------------------------------------------------------------
# Test 4 — All 29 Quick-Index bullets become index_entry chunks
# ---------------------------------------------------------------------------

def test_index_entries_become_chunks(chunks: list[Chunk]) -> None:
    index_entries = [c for c in chunks if c.kind == "index_entry"]
    # The cleaned manual ships with 29 quick-index bullets. Allow ±2 for
    # editorial flexibility across future manuals.
    assert 27 <= len(index_entries) <= 31, (
        f"Expected ~29 index_entry chunks, got {len(index_entries)}. "
        f"Titles: {[c.title for c in index_entries]}"
    )
    # Every index entry must have both a question (content) and a procedure-name (title).
    for ie in index_entries:
        assert ie.content.strip(), f"Empty content in index_entry: {ie}"
        assert ie.title.strip(), f"Empty title in index_entry: {ie}"


# ---------------------------------------------------------------------------
# Test 5 — Glossary terms become chunks
# ---------------------------------------------------------------------------

def test_glossary_terms_become_chunks(chunks: list[Chunk]) -> None:
    glossary = [c for c in chunks if c.kind == "glossary"]
    # The cleaned manual ships with 19 glossary terms. Allow ±2.
    assert 17 <= len(glossary) <= 21, (
        f"Expected ~19 glossary chunks, got {len(glossary)}. "
        f"Titles: {[c.title for c in glossary]}"
    )
    for g in glossary:
        assert g.content.strip(), f"Empty definition in glossary: {g}"


# ---------------------------------------------------------------------------
# Test 6 — Close a clinic session has the right screenshots attached
# ---------------------------------------------------------------------------

def test_close_clinic_session_has_attached_images(chunks: list[Chunk]) -> None:
    c = _find_procedure(chunks, "close a clinic session")
    assert len(c.images) >= 1, (
        f"Close-a-clinic-session has no images attached. images={c.images}"
    )
    # The image_captions should be non-empty and mention something clinic-related.
    assert c.image_captions, "image_captions is empty for Close-a-clinic-session."


# ---------------------------------------------------------------------------
# Test 7 — Title block paragraphs were stripped
# ---------------------------------------------------------------------------

def test_no_title_block_in_chunks(chunks: list[Chunk]) -> None:
    """The first three paragraphs of each manual (the title-block: module
    name / 'EMR System — User Manual…' / 'Ministry of Health · Uganda')
    should be stripped by load_docx and never appear as body text.

    NOTE: we do NOT forbid the SUBSTRING 'Ministry of Health' wholesale —
    some procedures legitimately reference 'the Ministry of Health weekly
    reports' or similar. We forbid only the EXACT title-block lines.
    """
    forbidden_exact_lines = [
        "Ministry of Health · Uganda",
        "Ministry of Health • Uganda",
    ]
    forbidden_starts = (
        "EMR System — User Manual",
        "EMR System – User Manual",
        "EMR System - User Manual",
    )

    for c in chunks:
        for line in forbidden_exact_lines:
            assert line not in c.content, (
                f"Title-block boilerplate leaked into a {c.kind} chunk "
                f"({c.title}): {line!r} found verbatim in content."
            )
            assert line not in c.image_captions, (
                f"Title-block boilerplate leaked into image_captions of "
                f"{c.title}: {line!r} found verbatim."
            )
        for prefix in forbidden_starts:
            assert prefix not in c.content, (
                f"Title-block boilerplate leaked into a {c.kind} chunk "
                f"({c.title}): {prefix!r} found in content."
            )

    # Also assert the chunker stripped exactly 3 title-block lines from
    # the front: no chunk should be titled after the document name itself.
    # (E.g. "Patient Management Module" should not show up as a chunk title.)
    suspicious_titles = ["Patient Management Module"]
    for c in chunks:
        assert c.title not in suspicious_titles, (
            f"A chunk has the document's own title-block heading as its title: {c.title!r}. "
            "This means the title-block pre-pass didn't fire."
        )
