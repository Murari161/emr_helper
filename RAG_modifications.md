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

---

## M6 — Out-of-scope handling, eAFYA Focal Person fallback, outcome log (✅ done 2026-11)
**Status:** ✅ done (stakeholder feedback item 1)
**Where:** new `app/rag/fallback.py` (fixed replies, outcomes) and `app/rag/classifier.py`
(question labelling); `app/rag/retriever.py` (`RetrievalResult`,
`retrieve_scored()`); `app/main.py` (`_respond()`, `_send_fixed_reply()`);
`app/rag/prompts.py`; `app/db/schema.sql` + `app/db/pool.py` (`messages.outcome`,
`question_log` view); `app/config.py` (`match_strong_vector`, `match_strong_bm25`).
**What:**
  1. **Match strength.** Retrieval now reports the best vector similarity and best BM25
     score alongside the chunks. A question is "strong" if vector >= 0.60 **or**
     BM25 >= 0.75; only strong questions reach the generation model.
  2. **Weak questions get a fixed reply, not a generated one.** One short classification
     call labels the question, and the reply is fixed text:
     off-topic → "I only cover how to use eAFYA"; about eAFYA but not in the manuals (or
     any classifier failure) → "contact your facility's **eAFYA Focal Person**";
     clinical and patient-data questions → the existing refusals; questions about the
     assistant → an identity blurb listing the manuals actually loaded.
  3. **Stubs.** When the answer is one of the manuals' "not yet documented" placeholders,
     the model's paraphrase is replaced with fixed wording that names the Focal Person
     (the stubs themselves still say "system administrator").
  4. **No screenshots under a decline.** Figures, the citation and cross-reference buttons
     are attached only when the outcome is `answered`.
  5. **Outcome log.** Every assistant turn is stored with an outcome (`answered`,
     `not_in_manual`, `off_topic`, `clinical`, `patient_data`, `about_assistant`, `filler`,
     `error`). The `question_log` view joins it to the question and the user.
  6. **Rename.** The assistant now calls itself the **eAFYA Assistant** (UI name, greeting,
     prompt, welcome page). Repository, containers and database keep the name `emr_helper`.
**Why:** stakeholders asked that out-of-knowledge-base questions be acknowledged and routed
to the eAFYA Focal Person, who is present in every facility. A 3B model given five loosely
related chunks tends to improvise instead of declining, so the decision is made in code.
**Calibration:** 63 prompts (the 40-prompt sheet plus extra undocumented and off-topic
ones). Vector similarity separates answerable from not-answerable, but does **not**
separate "eAFYA, undocumented" (0.48–0.59) from "off-topic" (0.31–0.52) — hence the
classifier. Thresholds are overridable with env `MATCH_STRONG_VECTOR` / `MATCH_STRONG_BM25`.
**Latency (CPU, measured):** the classification call costs ~4 s when it follows a normal
answer and ~0.4 s when it follows another classification. It deliberately reuses the answer
system prompt as its own system prompt: Ollama caches the last prompt prefix, and a
classifier with a separate system prompt evicted that cache and delayed the NEXT real
answer by ~10 s (first token 25.8 s vs 13–15 s). Sharing the prefix removes that penalty
(15.8 s). A weak question therefore costs ~4 s instead of a full ~20 s generation.
An embedding-based classifier (no model call) was tried and dropped: 28/34, with mistakes
across the safety categories.
**Effort:** code only. No re-clean, no re-ingest. Existing databases gain the new column
and view automatically at app start.
**Validation:** `docker compose exec app pytest tests/test_fallback.py -v`. In the chat:
"What's the weather today?" → off-topic reply; "How do I integrate eAFYA with DHIS2?" →
Focal Person reply; "How do I do a bulk dispensation?" → not-yet-documented + Focal Person;
"what modules do you know about?" → module list. Gap list:
`docker compose exec db psql -U emr emr_helper -c "select asked_at, user_id, question from question_log where outcome = 'not_in_manual' order by asked_at desc;"`
**Known limits:** the classifier is tuned on the same prompts it was checked against, so
expect some misses on real phrasing — every miss still lands on a safe reply. A weak-match
question that the manuals do answer gets the Focal Person reply; the topic menu planned for
vague questions (feedback item 2) is the mitigation.

---

## M7 — Screenshots under the step they illustrate (✅ done 2026-11)
**Status:** ✅ done (stakeholder feedback item 3)
**Where:** new `app/ui/figures.py`; `app/ingestion/chunk.py`; `app/main.py`. Outside this
repository: the 17 cleaned manuals were rebuilt (tools and mapping in the parent project's
`scratch/figure_steps/`, see its README).
**What:**
  1. **Manuals.** Each screenshot now sits directly under the step it illustrates instead
     of after the whole step list (504 of 637 images sit between steps; the rest belong to
     the last step). Only the order changed: a comparison script confirms identical text,
     identical images and intact image-caption pairs in all 17 manuals.
  2. **Ingestion.** Every image in `chunks.images` now carries `step` (the list item it
     sits under, 0 = none) and `step_text` (that item's text). The position in the .docx is
     the single source of truth, so moving a picture in Word and re-ingesting is enough.
  3. **Display.** `answer_with_figures()` inserts each screenshot under the matching step
     of the generated answer. It matches on wording, not step numbers, because the model
     may merge or renumber steps. A screenshot is placed only if at least half of its manual
     step's distinctive words appear in an answer step; otherwise it is listed in the old
     "Screenshots" block after the answer. Only the procedure the answer walks through is
     interleaved — screenshots of a procedure merely mentioned stay in the trailing block.
**Why:** stakeholders asked for "each step with its screenshot", not steps then screenshots.
**How placements were decided:** result of an action → that action's step; where-to-click
→ the first step that uses the screen (an annotated screenshot whose markers start at
step 1 follows step 1); form or pop-up → the first step that fills it in. Every placement
was checked against the image. 573 are clear-cut, 61 are close calls and 2 fit a different
procedure better — those 63, plus 32 captions that do not describe their image, are in
`EMR_Screenshot_Placement_Review.html` for a super user.
**Effort:** content + code. Requires a re-ingest of all manuals.
**Rollback:** copy `data/knowledge_base_before_figure_arrangement/*.docx` back into
`data/knowledge_base/` and re-ingest. The display code needs no change: images without step
data fall back to the trailing block.
**Validation:** `docker compose exec app pytest tests/test_figures.py -v`; then ask
"How do I dispense drugs to a patient?" and check each screenshot follows its step.
**Known limits:** during streaming the answer is text only; screenshots slot in when the
answer completes. An answer written without numbered steps keeps all screenshots at the end.

---

## M8 — Right source chunk, and full versions over summaries (✅ done 2026-11)
**Status:** ✅ done (follow-up to M7, from stakeholder testing)
**Where:** new `app/rag/attribution.py`, `app/rag/canonical.py`; `app/main.py`
(`_chunks_referenced_by`); `app/rag/retriever.py` (step 5b); `app/ui/figures.py`;
`app/db/queries.py` (`find_procedure_in_module`); `app/config.py` (`prefer_full_procedures`).
**What:**
  1. **Source chunk by content, not title.** The chunk an answer came from is now the one
     whose text the answer reproduces (word coverage >= 0.5). Screenshots, citation and
     cross-reference buttons come from that chunk only. The old title-in-answer heuristic
     remains as a fallback for heavily paraphrased answers.
  2. **Full version over summary.** If a retrieved procedure is a summary of a same-titled
     procedure in the manual it points to ("See module: X"), the full one takes its place
     in the ranked list. A procedure counts as a summary only if its own text says so
     ("summarised here", "full step-by-step … is in …", "is performed in the … module") or
     it has no screenshots while the other has some. Placeholders are never swapped in.
  3. **Placement gaps.** A screenshot whose manual step the model dropped is placed between
     its placed neighbours; a shortened step still matches. Leftovers are headed
     "More screenshots" when others were placed.
**Why (seen in testing):**
  - "How do I send a patient for imaging?" showed Patient Management steps ("click the
    Imaging Investigation button") with the Doctor manual's Investigations > Imaging
    screenshots and citation — the answer quoted the other manual's title.
  - "How do I register a patient?" answered from the Nursing summary, whose screenshots are
    small cut-outs (175x45 px), instead of the Patient Management procedure with full-screen
    annotated screenshots. Both ranked near-equal on meaning and keywords; the Patient
    Management one lost on the UI-label lane (a long label list matches a short question
    less closely) and landed 6th, outside the five chunks the model reads.
  - Screenshots of a procedure only mentioned in a closing note were appended to the answer.
**Scope today:** three summaries are redirected, all in Nursing — Register a patient
(→ Patient Management), Admit a patient directly to a ward and Admit directly to the morgue
(→ Direct Service Workflows). HR's "Approve a leave request" also points to a same-titled
Employee Portal procedure but is not a summary, so it is left alone (covered by a test).
**Effort:** code only. No re-ingest.
**Rollback:** env `PREFER_FULL_PROCEDURES=false` restores the raw ranking.
**Validation:** `docker compose exec app pytest tests/test_attribution.py tests/test_canonical.py tests/test_figures.py -v`

---

## M9 — Vague questions: offer a choice of topics (✅ done 2026-11)
**Status:** ✅ done (stakeholder feedback item 2)
**Where:** new `app/rag/topics.py`; `app/main.py` (`_topic_actions`, `pinned` in `_respond`,
`on_open_topic`); `app/rag/retriever.py` (`candidates`, `vector_score`); `app/db/queries.py`
(`manual` on every chunk); `app/rag/fallback.py` (`CLARIFY`, `is_chitchat`).
**What:**
  1. **Asks back when vague.** One meaningful word ("billing"), or two with several topics
     neck and neck ("patient records"), gets up to four green buttons, each naming the
     topic and its manual, plus a tip on phrasing. Clicking answers from exactly that
     procedure, without searching again.
  2. **Tip only when there is nothing sensible to offer.** No meaningful words ("it is not
     working") or no related topic ("how do I search") gets a request for more detail and
     the eAFYA Focal Person.
  3. **Never asks back on:** definitions, questions that name a topic, button labels
     ("+New"), and words the manuals do not contain ("weather").
  4. **Related topics under "not in the manuals".** The Focal Person reply gains up to two
     buttons when a topic scores at least 0.55 (e.g. a wristband question offers "Print a
     prescription"), so a near miss still leads somewhere.
  5. **Chit-chat by vocabulary, not length.** The old rule treated anything under 12
     characters as a greeting, so "billing", "queue" and "lab test" got "Hello!".
Vague questions are logged with outcome `clarify` in `question_log`.
**Measured on real search results:** 27 of 32 vague questions ask back; 0 of the 72
questions on the two stakeholder sheets do; 0 of 8 short clear questions do.
**The 5 that do not ask back:** "queue", "discharge" and "help with the queue" are answered
from the glossary entry of that name; "how do i save" and "how do i change it" have only one
related topic, so they are answered from it. The last two can be a poor guess.
**Effort:** code only. No re-ingest.
**Validation:** `docker compose exec app pytest tests/test_topics.py -v`; then type "billing"
and click a button.
**Tune later:** the sheets contain few truly vague questions, so the rule is calibrated
mainly on invented ones. Review `select question from question_log where outcome='clarify'`
once real users have tried it.

---

## M10 — Cautions and Notes checked against the manual; Focal Person wording (✅ done 2026-11)
**Status:** ✅ done (found in stakeholder testing)
**Where:** new `app/rag/callouts.py`; `app/main.py` (after the source chunk is chosen).
**What:** after each answer, every Caution / Warning / Note line is compared with the manual
text of the procedure the answer came from (>= 60% of its meaningful words must match):
  - a Caution kept only if it is one of the procedure's cautions; if it matches a note or
    the steps instead it is shown as a Note; otherwise it is removed;
  - a Note kept if it matches a note or the steps; shown as a Caution if it matches a caution;
    otherwise removed.
"System Administrator" in an answer becomes "your facility's eAFYA Focal Person" (the
manuals themselves are unchanged).
**Why:** the prompt forbids invented or relabelled callouts, but the 3B model ignores it.
Seen in testing: "Add a bill allocation" (no caution in the manual) got "⚠️ Caution: Ensure you
have entered accurate billing information…"; the leave-approval Note was shown as a Caution and
told users to contact the System Administrator.
**Replay of all past answers:** 51 callout lines — 21 kept, 8 relabelled, 22 removed. Of the 22,
6 were invented outright and 16 were borrowed from a neighbouring procedure, including a caution
about deleting patient records shown under "Discharge a patient". No caution that is in the
manual was removed.
**Manual callouts always shown:** any caution or note the procedure has but the answer left out
is added at the end, in the manual's words (seen live: the HR leave note vanished because the
model wrote the Employee Portal's wording, which the check removed). A placeholder note is not
added. A "(configured in …)" bracket after "System Administrator" is dropped with it, since it
describes the approval mandate.
**Shown as coloured boxes:** each Caution / Note is its own Markdown blockquote; `public/script.js`
tags it and `public/style.css` colours it like the cleaned manuals (red Caution, amber Note).
`.chainlit/config.toml` now loads both files with `?v=2`: browsers kept serving the old
script and stylesheet from cache even after a hard refresh. Bump `?v=` on every change to them.
**Trade-off:** a true note borrowed from a related procedure (e.g. the Clinic Charge note under
registration) is removed too, because the prompt's rule is one procedure per answer.
**Effort:** code only. No re-ingest.
**Validation:** `docker compose exec app pytest tests/test_callouts.py -v`; ask "How do I add a
bill allocation?" — no Caution line.

---

## M11 — "Something is not working": the eAFYA Focal Person, plus the steps (✅ done 2026-11)
**Status:** ✅ done (first part of stakeholder feedback item 4)
**Principle (from the project lead):** the assistant is a guide, not the final authority. Once a
user has followed the steps and something still does not work — a feature switched off at their
facility, a system fault, anything — the answer is the eAFYA Focal Person. No list of which
features are active where is kept: new features can never be exhausted.
**Where:** `app/rag/topics.py` (`is_trouble`, `troubleshooting`); `app/main.py` (before the
vague-question check); outcome `troubleshoot` in `question_log`.
**What:**
  1. A message saying something is not working ("reminder not working", "patient not saving",
     "error when dispensing", "nothing happens when I click send") gets: "If you have followed
     all the steps and it still does not work, please contact your facility's eAFYA Focal
     Person", plus up to two buttons to re-check that task's steps.
  2. If no task can be identified ("page is not loading"), the reply names the Focal Person and
     asks what the user was trying to do. A clinical message ("my patient can't breathe") is
     first checked by the classifier and gets the clinical reply instead.
  3. No guessing at causes, and no claims about whether a feature is switched on.
**Recognising real phrasing:** apostrophes optional ("dont", "doesnt"), the typo "wroking",
"refuses", "nothing happens", "does nothing", "no response", "hanging", "frozen", "fix", "greyed
out", "access denied". "Error", "missing", "problem", "issue" and "bug" count only outside a
how-to question, because they are ordinary words too ("how do I issue drugs").
**Measured:** 40 of 40 messy, realistic failure messages recognised and sent to the Focal Person
(32 with the right steps to re-check); 0 false alarms on the 72 stakeholder-sheet questions and
17 ordinary how-to questions; 3 of 3 clinical failure messages routed to the clinical reply.
**Limits:** phrase-based, so new wordings will be missed; a missed one gets the normal answer
(the steps), not a wrong one. Review `select question from question_log where outcome =
'troubleshoot'` and the unanswered questions after real use. Steps offered can be imperfect
("registration refusing" offers "Register a deceased individual" first).
**Still to do (item 4, part two):** checks drawn from the manuals per task, such as required
fields, once a super user has verified them.
**Validation:** `docker compose exec app pytest tests/test_topics.py -v`; ask "reminder not working".
