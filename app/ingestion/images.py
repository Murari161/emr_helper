"""Write extracted image blobs to disk and pair them with their captions.

Input: a LoadedDocument from load.py, where image elements carry raw bytes
and figure_caption elements follow images in document order.

Output: a list of ImageInfo records that the chunker will use to attach
images to procedure chunks. Each ImageInfo knows:
  - where the file was written (path under DATA_DIR/images/{slug}/)
  - the verbatim "Figure: ..." caption associated with the image
  - the 1-based figure number (1, 2, 3, ...) in document order
  - the original element index in the loaded document (for chunk attachment)

This module is the ONLY place that writes image files to disk. The chunker
takes the resulting metadata; the .gitignore keeps the extracted images
out of version control.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from app.ingestion.load import LoadedDocument

log = logging.getLogger(__name__)


@dataclass
class ImageInfo:
    # 0-based position in LoadedDocument.elements where the image lives.
    # The chunker uses this to find which procedure (H3 boundary) it falls in.
    element_index: int

    # Absolute path to the file on disk, e.g.
    #   /data/images/patient-management/fig_07.png
    path: Path

    # The verbatim text of the "Figure:" caption paragraph that immediately
    # followed the image in the document. Empty string if no caption was found.
    caption: str

    # 1-based figure index in document order. Useful for filenames and ordering.
    order: int


def extract_images(doc: LoadedDocument, images_root: Path) -> list[ImageInfo]:
    """Write every image element's blob to disk under
    `images_root / doc.doc_slug /` and pair each with the next figure_caption.

    Filenames are zero-padded so they sort naturally:
        fig_01.png, fig_02.png, ..., fig_51.png

    Returns the list in document order.
    """
    out_dir = images_root / doc.doc_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info("Writing images for %s to %s", doc.doc_slug, out_dir)

    results: list[ImageInfo] = []

    # Walk elements in order. When we see an image, write it and remember
    # its element index; the next figure_caption (if any) is its caption.
    n_total_images = sum(1 for e in doc.elements if e.kind == "image")
    width = max(2, len(str(n_total_images)))   # zero-pad width

    pending: ImageInfo | None = None  # image waiting for a caption

    for idx, elem in enumerate(doc.elements):
        if elem.kind == "image":
            order = len(results) + (1 if pending is None else 0) + 1
            # Use the running ordinal as 1, 2, 3, ...
            ordinal = sum(1 for r in results if True) + (1 if pending is not None else 0) + 1
            ordinal = len([e for e in doc.elements[: idx + 1] if e.kind == "image"])

            ext = elem.image_extension or "png"
            filename = f"fig_{ordinal:0{width}d}.{ext}"
            target = out_dir / filename

            if elem.image_blob is None:
                log.warning("Image element at index %d has no blob, skipping", idx)
                continue

            target.write_bytes(elem.image_blob)

            # If there's already a pending image (no caption appeared before
            # the next image), commit it with empty caption.
            if pending is not None:
                results.append(pending)

            pending = ImageInfo(
                element_index=idx,
                path=target,
                caption="",
                order=ordinal,
            )
            continue

        if elem.kind == "figure_caption" and pending is not None:
            # Attach this caption to the most recent unclaimed image.
            pending.caption = elem.text
            results.append(pending)
            pending = None
            continue

        # Any element that isn't an image or caption: if a pending image is
        # waiting and we hit something that definitely isn't its caption
        # (e.g. a heading, another paragraph), commit it as caption-less.
        # We keep the pending alive across regular paragraphs because some
        # documents have a blank paragraph between an image and its Figure: line.
        if pending is not None and elem.kind == "heading":
            results.append(pending)
            pending = None

    # Final flush.
    if pending is not None:
        results.append(pending)

    log.info(
        "Extracted %d images for %s (%d with captions, %d without)",
        len(results),
        doc.doc_slug,
        sum(1 for r in results if r.caption),
        sum(1 for r in results if not r.caption),
    )
    return results
