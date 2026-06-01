"""Turn a LoadedDocument + its extracted images into a list of Chunks ready for the DB.

Chunk kinds produced:
  procedure      one per Heading 3 (the meat — Steps, when_to_use, callouts, attached images)
  index_entry    one per "How do I…?" bullet in the Quick Index section
  glossary       one per term in the Glossary section
  section_intro  prose under Heading 1 / Heading 2 *before* any Heading 3 in that section

Each procedure chunk pulls these fields out of its element range:
  title          the H3 text
  when_to_use    the italic "When to use:" line if present
  content        Steps + Notes/Cautions/intro paragraphs, joined with newlines
  notes          concatenation of "Note: ..." callout paragraphs
  cautions       concatenation of "Caution: ..." callout paragraphs
  image_captions concatenation of attached images' captions, " || " separated
  images         JSON-able list of {path, caption, order} for the chunk's screenshots
  ui_labels      space-separated bold runs from every paragraph in the chunk
  section_path   breadcrumb like "Section 1: Patient registration > Queued Patients tab > Close a clinic session"
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

from app.ingestion.images import ImageInfo
from app.ingestion.load import Element, LoadedDocument

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Chunk model
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    kind: str               # "procedure" | "index_entry" | "glossary" | "section_intro"
    section_path: str
    title: str
    when_to_use: str | None
    content: str
    image_captions: str = ""
    images: list[dict] = field(default_factory=list)  # serializable to jsonb
    ui_labels: str = ""
    cautions: str = ""
    notes: str = ""
    page_start: int | None = None
    page_end: int | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_QUICK_INDEX_HEADING = re.compile(r"^quick\s+index", re.IGNORECASE)
_GLOSSARY_HEADING = re.compile(r"^glossary\s*$", re.IGNORECASE)

# Split a quick-index bullet on the rightward arrow that maps a phrasing to
# a procedure name. Handles "→" (most likely), "->", and similar.
_INDEX_ARROW = re.compile(r"\s*[→➜]\s*|\s*->\s*")

# Split a glossary entry on an em-dash or hyphen separating term and definition.
_GLOSSARY_SEP = re.compile(r"\s*[—–-]\s+")

# Prefixes we strip from callout text so the body of the callout is what
# gets stored.
_NOTE_STRIP = re.compile(r"^\s*Note\s*:\s*", re.IGNORECASE)
_CAUTION_STRIP = re.compile(r"^\s*Caution\s*:\s*", re.IGNORECASE)
_WHEN_STRIP = re.compile(r"^\s*When\s+to\s+use\s*:\s*", re.IGNORECASE)


def _strip_when_to_use(text: str) -> str:
    return _WHEN_STRIP.sub("", text).strip()


def _strip_callout_prefix(text: str) -> str:
    return _CAUTION_STRIP.sub("", _NOTE_STRIP.sub("", text)).strip()


# ---------------------------------------------------------------------------
# Section-aware walker
# ---------------------------------------------------------------------------

@dataclass
class _SectionState:
    """Tracks current H1 / H2 titles while iterating elements, so we can
    build a breadcrumb section_path for every chunk."""
    h1: str = ""
    h2: str = ""

    def breadcrumb_for_h3(self, h3_title: str) -> str:
        parts = [p for p in (self.h1, self.h2, h3_title) if p]
        return " > ".join(parts)

    def breadcrumb_for_intro(self) -> str:
        parts = [p for p in (self.h1, self.h2) if p]
        return " > ".join(parts)


def _is_within_quick_index(state: _SectionState) -> bool:
    return bool(_QUICK_INDEX_HEADING.search(state.h1))


def _is_within_glossary(state: _SectionState) -> bool:
    return bool(_GLOSSARY_HEADING.match(state.h1))


# ---------------------------------------------------------------------------
# Procedure chunk builder
# ---------------------------------------------------------------------------

def _build_procedure_chunk(
    *,
    h3_title: str,
    section_path: str,
    body_elements: Iterable[Element],
    attached_images: list[ImageInfo],
) -> Chunk:
    """Aggregate the body elements between this H3 and the next heading
    into a single procedure chunk."""

    when_to_use_text: str | None = None
    content_lines: list[str] = []
    notes_lines: list[str] = []
    cautions_lines: list[str] = []
    ui_label_runs: list[str] = []

    for elem in body_elements:
        # Collect bold UI labels everywhere in the procedure body.
        for run in elem.bold_runs:
            if run not in ui_label_runs:
                ui_label_runs.append(run)

        if elem.kind == "when_to_use":
            when_to_use_text = _strip_when_to_use(elem.text)
            continue

        if elem.kind == "callout":
            stripped = _strip_callout_prefix(elem.text)
            if elem.callout_type == "caution":
                cautions_lines.append(stripped)
            else:
                notes_lines.append(stripped)
            # Callouts also appear in the body so the generator can quote them.
            content_lines.append(elem.text)
            continue

        if elem.kind == "figure_caption":
            # Captions go into image_captions (handled via attached_images),
            # not into the prose body. Skip here.
            continue

        if elem.kind == "image":
            # Images are tracked via attached_images, not as text.
            continue

        # Headings inside the body shouldn't reach us — the slicing in the
        # caller stops at the next heading. Defensive: skip if we see one.
        if elem.kind == "heading":
            continue

        if elem.text:
            content_lines.append(elem.text)

    # image_captions: " || " separated, matches the schema doc.
    image_captions = " || ".join(img.caption for img in attached_images if img.caption)

    images_json = [
        {"path": str(img.path), "caption": img.caption, "order": img.order}
        for img in attached_images
    ]

    return Chunk(
        kind="procedure",
        section_path=section_path,
        title=h3_title,
        when_to_use=when_to_use_text,
        content="\n".join(content_lines).strip(),
        image_captions=image_captions,
        images=images_json,
        ui_labels=" ".join(ui_label_runs),
        cautions="\n".join(cautions_lines),
        notes="\n".join(notes_lines),
    )


# ---------------------------------------------------------------------------
# Special-section builders (Quick Index, Glossary, Section intros)
# ---------------------------------------------------------------------------

def _build_index_entry(text: str, h1_title: str) -> Chunk | None:
    """Split a 'How do I X?  →  Procedure Name' bullet into a chunk."""
    parts = _INDEX_ARROW.split(text, maxsplit=1)
    if len(parts) != 2:
        return None
    question, procedure_name = (p.strip() for p in parts)
    if not question or not procedure_name:
        return None
    return Chunk(
        kind="index_entry",
        section_path=h1_title,
        title=procedure_name,
        when_to_use=None,
        content=question,
    )


def _build_glossary_entry(text: str, h1_title: str) -> Chunk | None:
    """Split a glossary bullet 'Term — Definition' into a chunk."""
    parts = _GLOSSARY_SEP.split(text, maxsplit=1)
    if len(parts) != 2:
        return None
    term, definition = (p.strip() for p in parts)
    if not term or not definition:
        return None
    return Chunk(
        kind="glossary",
        section_path=h1_title,
        title=term,
        when_to_use=None,
        content=definition,
    )


def _build_section_intro(
    *,
    section_path: str,
    intro_elements: list[Element],
) -> Chunk | None:
    """Produce a section_intro chunk from prose paragraphs directly under H1/H2,
    before any H3 in that section. Returns None if there's no meaningful prose."""
    lines = [e.text for e in intro_elements if e.text]
    if not lines:
        return None
    content = "\n".join(lines).strip()
    if len(content) < 30:
        # Don't bother with one-line intros — they're noise.
        return None
    return Chunk(
        kind="section_intro",
        section_path=section_path,
        title=section_path.split(" > ")[-1] + " (overview)",
        when_to_use=None,
        content=content,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def chunk_document(doc: LoadedDocument, images: list[ImageInfo]) -> list[Chunk]:
    """Walk the loaded document's element list and emit Chunks.

    Strategy:
      Iterate once with a state machine. When we hit a Heading 3, we open
      a procedure-chunk window; when we hit the next heading (any level),
      we close the window and emit the chunk. Headings 1 and 2 update the
      breadcrumb. The Quick Index and Glossary H1 sections are detected
      and their list items are turned into index_entry / glossary chunks
      instead of being treated as section content. Section intros (prose
      under H1/H2 before any H3) become their own small chunks.
    """
    chunks: list[Chunk] = []
    state = _SectionState()
    elements = doc.elements

    # Index images by their element_index for fast lookup within a procedure window.
    images_by_idx = {img.element_index: img for img in images}

    # Walk indices so we can slice element ranges easily.
    i = 0
    n = len(elements)

    # Buffer of intro elements (under H1/H2, no H3 seen yet in this section).
    intro_buffer: list[Element] = []
    intro_section_path = ""

    while i < n:
        e = elements[i]

        # --- Headings drive the chunking ----------------------------------
        if e.kind == "heading":
            # First, flush any pending section intro.
            if intro_buffer and intro_section_path:
                intro_chunk = _build_section_intro(
                    section_path=intro_section_path,
                    intro_elements=intro_buffer,
                )
                if intro_chunk is not None:
                    chunks.append(intro_chunk)
            intro_buffer = []
            intro_section_path = ""

            if e.level == 1:
                state.h1 = e.text
                state.h2 = ""
                intro_section_path = state.breadcrumb_for_intro()
                i += 1
                continue

            if e.level == 2:
                state.h2 = e.text
                intro_section_path = state.breadcrumb_for_intro()
                i += 1
                continue

            if e.level == 3:
                # Open a procedure chunk window. Slice elements from i+1
                # until the next heading.
                h3_title = e.text
                section_path = state.breadcrumb_for_h3(h3_title)

                j = i + 1
                while j < n and elements[j].kind != "heading":
                    j += 1

                window = elements[i + 1 : j]

                # Images attached to this procedure: those whose element_index
                # falls within (i, j).
                attached = [
                    images_by_idx[k]
                    for k in range(i + 1, j)
                    if k in images_by_idx
                ]

                chunk = _build_procedure_chunk(
                    h3_title=h3_title,
                    section_path=section_path,
                    body_elements=window,
                    attached_images=attached,
                )
                # Procedure chunks also get image-caption text concatenated
                # into ui_labels-adjacent searchability through image_captions.
                # The TSV trigger in schema.sql already includes image_captions
                # in the full-text index, so we don't need to add it to ui_labels.
                chunks.append(chunk)
                i = j
                continue

            # Unknown heading level — just advance.
            i += 1
            continue

        # --- Quick Index special handling --------------------------------
        if _is_within_quick_index(state) and e.kind == "list_item":
            ie = _build_index_entry(e.text, state.h1)
            if ie is not None:
                chunks.append(ie)
            i += 1
            continue

        # --- Glossary special handling -----------------------------------
        if _is_within_glossary(state) and e.kind == "list_item":
            ge = _build_glossary_entry(e.text, state.h1)
            if ge is not None:
                chunks.append(ge)
            i += 1
            continue

        # --- Everything else: candidate for section_intro ----------------
        # Only "paragraph" elements (intro prose) count as intro content.
        if e.kind == "paragraph" and (state.h1 or state.h2):
            intro_buffer.append(e)

        i += 1

    # Flush final intro if any.
    if intro_buffer and intro_section_path:
        intro_chunk = _build_section_intro(
            section_path=intro_section_path,
            intro_elements=intro_buffer,
        )
        if intro_chunk is not None:
            chunks.append(intro_chunk)

    log.info(
        "Chunked %s: %d total (%d procedure, %d index_entry, %d glossary, %d section_intro)",
        doc.doc_slug,
        len(chunks),
        sum(1 for c in chunks if c.kind == "procedure"),
        sum(1 for c in chunks if c.kind == "index_entry"),
        sum(1 for c in chunks if c.kind == "glossary"),
        sum(1 for c in chunks if c.kind == "section_intro"),
    )
    return chunks
