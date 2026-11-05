"""Place a procedure's screenshots inside the generated answer.

Stakeholder request (Oct 2026): show each screenshot right after the step it
illustrates, not in one block after all the steps.

Where a screenshot belongs comes from the manual itself: ingestion records,
for every image, the step it sits under (`step`, `step_text` in chunks.images).
The answer, though, is written by the model, which may renumber, merge or drop
steps — so we don't trust step numbers. For each screenshot we look for the
answer step whose wording best matches the manual step it belongs to, and
insert the screenshot under that step. Anything we can't place confidently
keeps the old behaviour: a "Screenshots" block after the answer.

Pure functions only (no Chainlit), so they are unit-testable.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)

# "1. Click …" / "  2) Click …" — a numbered step line in the answer.
_STEP_LINE_RE = re.compile(r"^(\s*)(\d+)([.)])\s+(\S.*)$")
# A line that continues the step above it: indented text or a sub-bullet.
_CONTINUATION_RE = re.compile(r"^(\s+\S|\s*[-*–•]\s)")

# Minimum share of a manual step's distinctive words that must appear in an
# answer step before we put the screenshot under it.
MATCH_THRESHOLD = 0.5

_STOPWORDS = frozenset(
    "a an the to of in on for and or is are be this that it its at by with from as will "
    "you your then now can has have into all also which their there here".split()
)


@dataclass
class Figure:
    order: int
    caption: str
    url: str
    step: int = 0
    step_text: str = ""


def image_url(raw: str) -> str:
    """Convert a stored image path (…/data/images/<slug>/fig_NN.png) into the
    URL served by the static mount (e.g. /images/<slug>/fig_NN.png)."""
    p = str(raw).replace("\\", "/")
    root = str(settings.images_dir).replace("\\", "/").rstrip("/")
    prefix = settings.images_url_prefix.rstrip("/")
    if p.startswith(root):
        return prefix + p[len(root):]
    # Fallback: keep the last two components (<slug>/<file>).
    parts = [seg for seg in p.split("/") if seg]
    if len(parts) >= 2:
        return f"{prefix}/" + "/".join(parts[-2:])
    return p


def collect_figures(chunks_used: list[dict[str, Any]]) -> list[Figure]:
    """Every screenshot of the chunks the answer drew on, in document order."""
    seen: set[str] = set()
    figures: list[Figure] = []
    for c in chunks_used:
        images = c.get("images") or []
        if isinstance(images, str):
            try:
                images = json.loads(images)
            except Exception:
                log.warning("Chunk %s has unparseable images jsonb; skipping", c.get("id"))
                continue
        for img in images:
            if not isinstance(img, dict):
                continue
            path = img.get("path", "")
            if not path or path in seen:
                continue
            seen.add(path)
            figures.append(Figure(
                order=int(img.get("order", 0) or 0),
                caption=(img.get("caption") or f"Figure {img.get('order', '?')}").strip(),
                url=image_url(path),
                step=int(img.get("step", 0) or 0),
                step_text=(img.get("step_text") or "").strip(),
            ))
    figures.sort(key=lambda f: f.order)
    return figures


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9+]+", text.lower()) if w not in _STOPWORDS and len(w) > 1}


def _match(step_text: str, answer_step: str) -> float:
    """Share of the manual step's distinctive words found in an answer step."""
    want = _tokens(step_text)
    if not want:
        return 0.0
    return len(want & _tokens(answer_step)) / len(want)


def _figure_md(fig: Figure, indent: str = "") -> list[str]:
    """Caption first (so it labels the figure), then the image beneath it."""
    alt = fig.caption.replace("]", ")").replace("[", "(")  # keep Markdown alt-text safe
    return ["", f"{indent}*{fig.caption}*", "", f"{indent}![{alt}]({fig.url})", ""]


def _coverage(answer: str, chunk: dict[str, Any]) -> float:
    """Share of the answer's distinctive words that come from this chunk."""
    said = _tokens(answer)
    if not said:
        return 0.0
    source = _tokens(f"{chunk.get('title') or ''} {chunk.get('content') or ''}")
    return len(said & source) / len(said)


def answer_with_figures(answer: str, chunks_used: list[dict[str, Any]]) -> str:
    """Add the screenshots of the chunks an answer drew on.

    Only the procedure the answer actually walks through has its screenshots
    placed under steps. An answer can also name a second procedure in passing
    ("...otherwise follow *Queue a returning patient*"); that one's screenshots
    share wording with the main steps ("Click the Register Patient tab") and
    would land under them, so they are kept in the block after the answer.
    """
    if not chunks_used:
        return answer
    main_idx = max(range(len(chunks_used)), key=lambda i: (_coverage(answer, chunks_used[i]), -i))
    main = collect_figures([chunks_used[main_idx]])
    shown = {f.url for f in main}
    others = [
        f for f in collect_figures([c for i, c in enumerate(chunks_used) if i != main_idx])
        if f.url not in shown
    ]
    return place_figures(answer, main, extra=others)


def place_figures(answer: str, figures: list[Figure], extra: list[Figure] | None = None) -> str:
    """Return the answer with each screenshot under the step it illustrates.

    Screenshots that can't be tied to a step of the answer — and every
    screenshot in `extra` — are appended in a "Screenshots" block, as before.
    """
    extra = list(extra or [])
    if not figures and not extra:
        return answer

    lines = answer.split("\n")
    # Numbered steps of the answer: (line index, indent for nested content, text).
    steps: list[tuple[int, str, str]] = []
    for i, line in enumerate(lines):
        m = _STEP_LINE_RE.match(line)
        if m:
            nested_indent = " " * (len(m.group(1)) + len(m.group(2)) + 2)
            steps.append((i, nested_indent, m.group(4)))

    # Text of each answer step = its line plus continuation lines up to the next step.
    step_texts: list[str] = []
    for n, (start, _, _) in enumerate(steps):
        end = steps[n + 1][0] if n + 1 < len(steps) else len(lines)
        step_texts.append(" ".join(lines[start:end]))

    under: dict[int, list[Figure]] = {}
    leftover: list[Figure] = []
    for fig in figures:
        best, best_score = -1, 0.0
        if fig.step_text and steps:
            for n, text in enumerate(step_texts):
                score = _match(fig.step_text, text)
                # Near-ties (repeated wording such as "Click Update") go to
                # the step with the same number as in the manual.
                if score and n + 1 == fig.step:
                    score += 0.05
                if score > best_score:
                    best, best_score = n, score
        if best >= 0 and best_score >= MATCH_THRESHOLD:
            under.setdefault(best, []).append(fig)
        else:
            leftover.append(fig)
    leftover.extend(extra)

    if under:
        out: list[str] = []
        for n, (start, indent, _) in enumerate(steps):
            if n == 0:
                out.extend(lines[:start])
            next_start = steps[n + 1][0] if n + 1 < len(steps) else len(lines)
            if n + 1 < len(steps):
                block_end = next_start
            else:
                # Last step: it ends where its continuation lines end; what
                # follows (a note, a closing sentence) stays after the figure.
                block_end = start + 1
                while block_end < len(lines) and _CONTINUATION_RE.match(lines[block_end]):
                    block_end += 1
            block = lines[start:block_end]
            while block and not block[-1].strip():
                block.pop()
            out.extend(block)
            for fig in under.get(n, []):
                out.extend(_figure_md(fig, indent))
            if not under.get(n) and n + 1 < len(steps) and block_end - start > len(block):
                out.append("")  # keep the blank line the model put between steps
            if n + 1 == len(steps):
                rest = lines[block_end:]
                if rest and rest[0].strip() and out and out[-1].strip():
                    out.append("")
                out.extend(rest)
        answer = "\n".join(out)

    if leftover:
        tail = ["\n\n---\n\n**Screenshots**\n"]
        for fig in leftover:
            tail.append("\n".join(_figure_md(fig)))
        answer = answer.rstrip() + "\n".join(tail)

    log.info(
        "Figures: %d placed under steps, %d listed after the answer (%d answer step(s))",
        sum(len(v) for v in under.values()), len(leftover), len(steps),
    )
    return answer
