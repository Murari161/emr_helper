"""Swap a summary procedure for the full version it points to.

Some tasks are written up twice. The Nursing manual carries a short summary of
"Register a patient" with small cut-out screenshots and the line "See module:
Patient Management"; the Patient Management manual has the full procedure with
whole-screen, annotated screenshots. Both rank almost equally for "How do I
register a patient?", and the summary can win on an incidental signal while the
full version lands just outside the five chunks the model reads. Stakeholders
then see cut-outs where the manual has a full screen.

So when a retrieved procedure is a SUMMARY of a same-titled procedure in the
module it points to, the full version takes its place.

"Summary" is deliberately narrow. A "See module:" marker alone is not enough:
HR's "Approve a leave request" points to the Employee Portal, which has a
procedure of the same title, but that one is the employee's side of the task,
not a fuller version of the HR one. A procedure counts as a summary only if
  - its own text says so ("summarised here", "the full step-by-step procedure
    … is in …", "is performed in the … module"), or
  - it has no screenshots and the same-titled procedure it points to has some.
Placeholders ("not yet documented") are never swapped in.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.db import queries
from app.rag import fallback

log = logging.getLogger(__name__)

_SEE_MODULE_RE = re.compile(r"^\s*See module:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_SUMMARY_CUE_RE = re.compile(r"summari[sz]ed here|full step-by-step|is performed in the", re.IGNORECASE)


def module_key(name: str) -> str:
    """'Patient Management Module.' -> 'patient management' (how manuals are matched)."""
    key = re.sub(r"\s+", " ", name.strip().rstrip(".").lower())
    return re.sub(r"\s+module$", "", key)


def referenced_modules(chunk: dict[str, Any]) -> list[str]:
    """Module keys named by the chunk's 'See module:' markers, in order."""
    return [module_key(m) for m in _SEE_MODULE_RE.findall(chunk.get("content") or "")]


def is_summary_of(chunk: dict[str, Any], full: dict[str, Any]) -> bool:
    """True if `chunk` is a summary whose full version is `full`."""
    if full.get("id") == chunk.get("id") or fallback.is_stub(full):
        return False
    if _SUMMARY_CUE_RE.search(chunk.get("content") or ""):
        return True
    return not (chunk.get("images") or []) and bool(full.get("images") or [])


async def prefer_full_versions(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace each summary procedure in a ranked list with its full version.

    The full version takes the summary's position; if it was already in the
    list further down, that later copy is dropped. Order is otherwise kept.
    """
    out: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for chunk in chunks:
        chosen = chunk
        if chunk.get("kind") == "procedure":
            for module in referenced_modules(chunk):
                full = await queries.find_procedure_in_module(module, chunk.get("title") or "")
                if full is not None and is_summary_of(chunk, full):
                    log.info(
                        "  full version: %r — using the %s copy instead of the summary",
                        chunk.get("title"), module,
                    )
                    chosen = full
                    break
        if chosen["id"] in seen:
            continue
        seen.add(chosen["id"])
        out.append(chosen)
    return out
