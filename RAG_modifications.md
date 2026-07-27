# RAG modifications

A living list of changes the RAG pipeline needs, discovered while cleaning the EMR manuals.
Add to it as we find more — keep each item with: what, where (file:line), why, effort, how to
validate, and status. File/line references point at the state of the code when the item was logged;
re-check before editing.

**Status legend:** 🔵 proposed · 🟡 in progress · ✅ done · 🧊 deferred

---

## Operational notes (read before applying any ranking change)

- **`tsv` is a STORED generated column** (`app/db/schema.sql`). You cannot edit it live; either
  `ALTER TABLE chunks DROP COLUMN tsv;` then re-add it with the new expression (recomputes for all
  rows), or drop the `pgdata` volume and re-init (steps documented at the top of `schema.sql`).
- **Changing embedding input requires a full re-ingest** (every chunk must be re-embedded).
- `tests/test_db.py` references `tsv` / `section_path` — re-run and update it after schema changes.
- Validation needs the stack up (Postgres + Ollama via docker-compose); these changes can't be
  verified from the document-cleanup workspace.

---

## M1 — Add `section_path` to the embedding text
**Status:** 🔵 proposed
**Where:** `app/ingestion/index.py` → `_embedding_text()` (~line 62)
**What:** Today the embedded string is `title + when_to_use + content + image_captions`. Prepend the
breadcrumb so it carries point-of-service context:
```python
parts = [chunk.section_path, chunk.title]   # was: [chunk.title]
```
**Why:** Near-twin procedures (e.g. "Administer a family planning method" in Family Health Queue vs
"Administer an ancillary care service" in Critical Care Queue) differ mainly by their section. The
breadcrumb is currently display-only and never reaches the vector, so semantic search can't use it.
**Effort:** 1 line. **Validation:** re-ingest, then query a twin and confirm the right one ranks top.
**Caveat:** requires re-ingest to take effect.

## M2 — Add `section_path` to the BM25 full-text index
**Status:** 🔵 proposed
**Where:** `app/db/schema.sql` → the `tsv` generated column (~line 132)
**What:** Add a weighted slice (B or C):
```sql
setweight(to_tsvector('english', coalesce(section_path, '')), 'B') ||
```
**Why:** Same reason as M1, for the lexical lane. The search SQL needs no change — `_BM25_SQL`
(`app/db/queries.py` ~line 182) already ranks on `tsv`, so the new tokens are searched automatically.
**Effort:** 1 line + column redefine. **Validation:** redefine the column (see Operational notes),
re-ingest, run a twin query.

## M3 — Follow a cross-reference and inject the named chunk
**Status:** 🟡 in progress — **cleanup side done**, RAG side pending.
**Marker contract (DECIDED & implemented in cleanup):** every cleaned docx emits parseable lines
directly under a procedure heading:
- `See: <Exact Procedure Title>` — links to a procedure in any manual (match against chunk `title`).
- `See module: <Module Name>` — links to a whole manual.
Parse with `^See(?: module)?:\s*(.+)$`. The builder (`scratch/build/builder.js`, `seeAlso` field) emits
these for all manuals; the Nursing Module already carries 6 such markers. The RAG side below is still to do.
**Why:** Manuals reference other procedures/modules (e.g. "see Register a patient", "see the Morgue
Management module"). The bot should retrieve and present that section, not deflect the user. Corpus-wide
retrieval already ranks referenced chunks if all manuals are ingested; M3 makes it *guaranteed* rather
than rank-dependent.
**Where:**
1. `app/db/queries.py` — add `get_chunk_by_title(title, manual_versions)` near `get_chunks_by_ids`
   (~line 247): fetch the procedure chunk whose `title` matches the referenced name.
2. `app/rag/retriever.py` — in `retrieve()` after hydrate/rerank (~line 152), scan returned chunks for
   cross-reference markers, fetch the named chunks, append de-duped. (Or expand at prompt-build time in
   `app/rag/generator.py`.)
**Dependency — marker contract:** cleanup must emit a parseable marker. Proposed: a dedicated line
`See: <Exact Procedure Title>` (and `See module: <Module Name>`) in the cleaned docx, which the
ingester can lift into a structured field or the retriever can regex. **Decision needed**, then the
cleanup builder (`scratch/build/builder.js`) emits it for all manuals.
**Effort:** medium (query + retriever logic + tests + marker contract).

---

## Candidates / discovered as we go

- **C1 — Reranker is a pass-through.** `app/rag/reranker.py` `rerank()` currently returns chunks
  unchanged (~line 91/109). Wire up the real cross-encoder (README mentions bge-reranker-v2-m3) for the
  top-K_TO_RERANK candidates. 🔵
- **C2 — Image serving to the UI.** Confirm the chat UI renders the `images` JSON (path + caption +
  order) so flow screenshots appear in order with answers. Trace `images` from `get_chunks_by_ids`
  → generator → Chainlit. 🔵
- **C3 — Caption/markers in retrieval.** Captions are embedded (good). Consider whether red-marker text
  ("marker 4 = …") helps or hurts BM25; revisit if captions over-rank. 🧊
- **C4 — `ui_labels` weighting.** Trigram-only today. If exact-button queries underperform, consider a
  weighted tsv slice for `ui_labels`. 🧊

---

## Change log
- 2026-06-23 — created during Nursing Module cleanup; logged M1, M2, M3, C1–C4.

---

## M4 — Render screenshots inline, captioned, in order (✅ done 2026-06)
**Status:** ✅ done
**Where:** `app/main.py` — added a `/images` static mount; replaced `_build_image_elements()`
with `_image_url()` + `_build_figures_markdown()`; rewrote the post-stream section of `on_message()`.
**What:** Previously all of a procedure's screenshots were attached as `cl.Image(display="inline")`
elements, which Chainlit renders as a bottom gallery of thumbnails labelled only by a small `name`.
Users couldn't tell which screenshot went with which step. Now each image is embedded as captioned
Markdown — `*<Figure caption>*` then `![...](/images/<slug>/fig_NN.png)` — appended to the answer in
document `order`, under a `**Screenshots**` divider. Images are served via a `StaticFiles` mount at
`settings.images_url_prefix` (`/images`), which was defined in config but never wired up.
**Why:** The stored chunk data already carries each image's caption + order; this surfaces them so a
reader can map a screenshot to the instruction it illustrates.
**Effort:** ~one file, no re-clean / no re-ingest (uses existing `images` jsonb).
**Validation:** restart the `app` container; ask "How do I change a patient's bed?" — screenshots
should appear each under its caption, in order. Persisted content keeps the Markdown image URLs, so
conversation history re-renders them too.
**Follow-up (not done):** true per-step interleaving (figure placed immediately after the step it
illustrates) — option ②(a): have the model drop a figure token after the relevant step; or ②(b):
tag each figure with its step number in the source content (needs re-clean + re-ingest).

**M4 fix-up (route order):** the first cut used `app.mount("/images", …)`, which *appends* the route
**after** Chainlit's SPA catch-all → `/images/*` returned the app HTML and screenshots showed as
broken-image icons (captions + order were already correct). Fixed by inserting a `starlette.routing.Mount`
at index 0 of `chainlit.server.app.router.routes` so the static files take priority over the catch-all.

---

## M5 — Clickable cross-reference buttons + Section_path leak fix (✅ done 2026-06)
**Status:** ✅ done
**Where:** `app/main.py` (new `_reference_actions()`, refactored `_respond()`, new
`@cl.action_callback("open_reference")`); `app/rag/prompts.py` (`build_user_prompt`).
**What:**
  1. **Cross-reference buttons (the user-driven form of M3).** Procedures carry
     "See:"/"See module:" markers. The answer now renders a clickable
     **"📄 Open: <target>"** button per unique marker found in the chunks it drew on.
     Clicking re-runs retrieval for that target and posts the linked workflow (with
     its own screenshots + buttons). `on_message` was refactored into a shared
     `_respond(query, conv_id)` so the click path and the type path behave identically.
  2. **Section_path leak fix.** The user-prompt told the model to "end with a citation
     line using the section_path" while the system prompt said the app appends the
     citation — the 3B model resolved the contradiction by emitting a literal
     "Section_path: …" line into the answer. Removed/inverted that instruction.
**Why:** users expected the "See module: X" reference to be actionable, not plain text;
and internal metadata (`Section_path:`) was leaking into user-facing answers.
**Effort:** ~one file + one prompt line. No re-clean / no re-ingest.
**Validation:** restart `app`; ask "How do I admit a patient directly to a ward?" or the
Ward "direct admission" question → an Open button should appear; clicking it loads the
Direct Service Workflows steps. Confirm no "Section_path:" line shows in answers.
**Note on `query` for module-level refs:** clicking "See module: Direct Service Workflows"
retrieves with the module *name* as the query, which may land on that module's overview
rather than the exact sibling procedure. Good enough for v1; could be sharpened later by
passing the originating procedure title alongside the module.
