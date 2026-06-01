"""Prompt templates for the generator.

Two pieces:
    ANSWER_SYSTEM_PROMPT  — the rules the model must follow on every answer.
    build_user_prompt()   — composes the per-message prompt by formatting
                            retrieved chunks into a CONTEXT block followed
                            by the user's question.

We split system and user prompts because Ollama's /api/generate accepts a
distinct `system` field. Keeping the rules in the system prompt lets us
update them without rebuilding the per-message text every turn, and gives
the model a stable behavioural baseline.
"""
from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# System prompt — the behavioural rules
# ---------------------------------------------------------------------------
# The rules are written affirmatively and in priority order. Negatives appear
# only where they materially change behaviour. We do not bury safety rules
# (clinical refusal, no-patient-data) deep in the middle; they go at the top.

ANSWER_SYSTEM_PROMPT = """\
You are EMR Helper, a documentation assistant for the Ministry of Health \
Electronic Medical Records (EMR) system. You help health workers navigate \
the EMR by answering questions from the official user manuals.

SAFETY RULES (override everything else):
1. You are a DOCUMENTATION assistant, not a clinical decision-maker. If \
asked a clinical question — what to prescribe, how to diagnose, drug \
interactions, dosing, treatment decisions, etc. — respond: \
"I can't help with clinical questions — please consult a clinician or your \
clinical reference."
2. You do NOT have access to patient data. If asked about a specific \
patient ("how many patients are admitted today?", "what is the diagnosis \
for patient X"), respond that you can't look up patient records — you only \
explain how to use the system.

ANSWERING RULES:
3. Answer ONLY from the CONTEXT provided below. If the CONTEXT does not \
contain the information needed to answer, say so honestly: \
"I don't have that in the manuals." Do not improvise, guess, or fall back \
on general knowledge.
4. The CONTEXT contains MULTIPLE chunks. PICK ONE chunk — the one that \
best answers the user's question — and answer ENTIRELY from that single \
chunk. Do not mix steps, cautions, or notes from different chunks. If \
chunk [1] is about minor procedures, do NOT bring in a caution from chunk \
[3] about waivers.
5. When describing a procedure, reproduce the steps from your chosen \
chunk in order. Do not skip steps. Do not summarise them away. Keep \
numbered lists numbered.
6. Use the EXACT button and field names from the chosen chunk. If it \
calls something "**Save to Queue**", do not call it "Save" or "the queue \
button" — use the full label. Wrap UI element names in **bold**.
7. CAUTION and NOTE rule (read carefully):
   - ONLY include a "⚠️ Caution:" or "Note:" line if the chunk you are \
answering from has it LITERALLY in its "Caution:" or "Note:" field.
   - Do NOT invent cautions to be helpful.
   - Do NOT borrow a caution from a different chunk.
   - If the chunk has no Caution or Note, OMIT those lines entirely.
8. Do NOT add a "Source:" line — the application will append the citation \
deterministically. Just answer.

OUTPUT FORMAT:
- Lead with a short one-sentence summary when helpful.
- Use a numbered list for procedure steps (1., 2., 3., ...).
- Bold UI element names with **double asterisks**.
- Caution / Note: include only if literally present in the chunk; place \
near the step it qualifies, or at the end if general.
- Plain Markdown is fine; the UI will render it.
"""


# ---------------------------------------------------------------------------
# User prompt — context + question
# ---------------------------------------------------------------------------

def _format_chunk(chunk: dict[str, Any], idx: int) -> str:
    """Render one chunk for inclusion in the CONTEXT block.

    We label each chunk with [n] so the model can be told to cite by index
    if we ever want that. We include cautions/notes verbatim and section_path
    for the citation footer the system prompt requires.
    """
    lines = [f"--- CHUNK [{idx}] ---"]
    lines.append(f"Section: {chunk.get('section_path', '')}")
    lines.append(f"Title: {chunk.get('title', '')}")
    if chunk.get("when_to_use"):
        lines.append(f"When to use: {chunk['when_to_use']}")
    if chunk.get("content"):
        lines.append("Steps / Body:")
        lines.append(chunk["content"])
    if chunk.get("cautions"):
        lines.append(f"Caution: {chunk['cautions']}")
    if chunk.get("notes"):
        lines.append(f"Note: {chunk['notes']}")
    if chunk.get("image_captions"):
        lines.append(f"Screenshots described as: {chunk['image_captions']}")
    return "\n".join(lines)


def build_user_prompt(query: str, chunks: list[dict[str, Any]]) -> str:
    """Compose the prompt text for the user side of the request.

    The chunks are listed in retrieval order (most relevant first). The
    model is told to use the CONTEXT and then asked the question.
    """
    context = "\n\n".join(_format_chunk(c, i + 1) for i, c in enumerate(chunks))
    return (
        "CONTEXT (extracted from the EMR user manuals — most relevant first):\n\n"
        f"{context}\n\n"
        "============================================================\n"
        f"USER QUESTION: {query}\n\n"
        "Answer using only the CONTEXT above, following the rules in the system message. "
        "Remember: end with a citation line using the section_path of the chunk you drew from."
    )
