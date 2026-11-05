"""Tests for placing screenshots under the step they illustrate (app/ui/figures.py).

Pure functions — no database, no Ollama, no Chainlit:
    docker compose exec app pytest tests/test_figures.py -v

What each test catches:

  test_each_figure_lands_under_its_step
      The basic contract: a screenshot recorded under manual step N appears
      directly after the matching step of the answer, before the next step.

  test_figure_is_nested_in_the_list_item
      The inserted Markdown is indented to the step's text, so the numbered
      list is not broken and later steps keep their numbers.

  test_matching_follows_wording_not_numbers
      The model merged two manual steps into one, so numbers shifted. The
      screenshot must follow the step with the matching wording.

  test_unmatched_figures_fall_back_to_the_screenshots_block
      A screenshot whose step the answer doesn't contain, or that has no
      step at all, is still shown — after the answer, as before.

  test_answer_without_numbered_steps_is_unchanged_up_to_the_block
      A prose answer (e.g. a glossary definition) keeps every figure in the
      trailing block; nothing is inserted mid-text.

  test_text_after_the_last_step_stays_after_its_figure
      A closing note the model wrote after the steps is not swallowed into
      the last step: the figure sits between the last step and the note.

  test_no_figures_returns_answer_untouched

  test_only_the_main_procedure_is_placed_under_steps
      An answer that names a second procedure in passing pulls that chunk in
      too. Its screenshots share wording with the main steps; they must go to
      the trailing block, not under the main procedure's steps (seen live:
      "Queue a returning patient" shots landing in "Register a patient").
"""
from __future__ import annotations

from app.ui.figures import Figure, answer_with_figures, collect_figures, place_figures

ANSWER = (
    "To register a patient:\n"
    "\n"
    "1. Click the **Register Patient** tab.\n"
    "2. Click the **+New** button in the toolbar.\n"
    "3. Enter the patient details in the form.\n"
    "4. Click the **Save** button at the bottom of the form."
)


def fig(order: int, step: int, step_text: str) -> Figure:
    return Figure(order=order, caption=f"Figure: shot {order}", url=f"/images/m/fig_{order:02d}.png",
                  step=step, step_text=step_text)


def test_each_figure_lands_under_its_step() -> None:
    out = place_figures(ANSWER, [
        fig(1, 2, "Click the +New button in the toolbar. This opens the patient registration page."),
        fig(2, 4, "Click the Save button at the bottom of the form to save the record."),
    ])
    assert "**Screenshots**" not in out
    i_step2, i_fig1, i_step3 = out.index("2. Click"), out.index("fig_01.png"), out.index("3. Enter")
    assert i_step2 < i_fig1 < i_step3
    assert out.index("4. Click") < out.index("fig_02.png")


def test_figure_is_nested_in_the_list_item() -> None:
    out = place_figures(ANSWER, [fig(1, 2, "Click the +New button in the toolbar.")])
    lines = out.split("\n")
    img_line = next(l for l in lines if "fig_01.png" in l)
    assert img_line.startswith("   ![")            # indented to the text of "2. "
    assert "3. Enter the patient details" in out     # later steps untouched


def test_matching_follows_wording_not_numbers() -> None:
    merged = (
        "1. Click the **Register Patient** tab, then click **+New** in the toolbar.\n"
        "2. Enter the patient details in the form.\n"
        "3. Click **Save** at the bottom of the form."
    )
    # In the manual this screenshot sits under step 4 (Save); the answer has only 3 steps.
    out = place_figures(merged, [fig(1, 4, "Click the Save button at the bottom of the form.")])
    assert out.index("3. Click **Save**") < out.index("fig_01.png")
    assert "**Screenshots**" not in out


def test_unmatched_figures_fall_back_to_the_screenshots_block() -> None:
    out = place_figures(ANSWER, [
        fig(1, 2, "Click the +New button in the toolbar."),
        fig(2, 9, "Print the wristband and attach it to the patient."),   # not in the answer
        fig(3, 0, ""),                                                    # no step recorded
    ])
    block = out[out.index("**Screenshots**"):]
    assert "fig_02.png" in block and "fig_03.png" in block
    assert "fig_01.png" not in block


def test_answer_without_numbered_steps_is_unchanged_up_to_the_block() -> None:
    prose = "A debtor tracking account records what a client owes."
    out = place_figures(prose, [fig(1, 1, "Open the debtor tracking account.")])
    assert out.startswith(prose)
    assert out.index("**Screenshots**") < out.index("fig_01.png")


def test_text_after_the_last_step_stays_after_its_figure() -> None:
    answer = ANSWER + "\n\nNote: fields marked in red are mandatory."
    out = place_figures(answer, [fig(1, 4, "Click the Save button at the bottom of the form.")])
    assert out.index("4. Click") < out.index("fig_01.png") < out.index("Note: fields marked")


def test_no_figures_returns_answer_untouched() -> None:
    assert place_figures(ANSWER, []) == ANSWER


def test_collect_figures_reads_step_fields_and_dedupes() -> None:
    chunk = {"images": [
        {"path": "/data/images/m/fig_02.png", "caption": "Figure: b", "order": 2, "step": 3, "step_text": "Click Save."},
        {"path": "/data/images/m/fig_01.png", "caption": "Figure: a", "order": 1},          # pre-step-aware ingest
    ]}
    figs = collect_figures([chunk, chunk])
    assert [f.order for f in figs] == [1, 2]
    assert (figs[1].step, figs[1].step_text) == (3, "Click Save.")
    assert (figs[0].step, figs[0].step_text) == (0, "")
    assert figs[0].url.endswith("/m/fig_01.png")


def test_only_the_main_procedure_is_placed_under_steps() -> None:
    main = {
        "title": "Register a patient",
        "content": "Click the Register Patient tab.\nClick the +New button in the toolbar.\n"
                   "Enter the patient details in the form.\nClick the Save button at the bottom of the form.",
        "images": [{"path": "/data/images/a/fig_02.png", "caption": "Figure: form", "order": 2,
                    "step": 3, "step_text": "Enter the patient details in the form."}],
    }
    other = {
        "title": "Queue a returning patient",
        "content": "Click the Register Patient tab.\nSearch by Patient Number.\nClick Add to Queue.",
        "images": [{"path": "/data/images/b/fig_09.png", "caption": "Figure: queue", "order": 9,
                    "step": 1, "step_text": "Click the Register Patient tab."}],
    }
    answer = ANSWER + "\n\nNote: for a returning patient, follow Queue a returning patient instead."
    for chunks in ([main, other], [other, main]):          # retrieval order must not matter
        out = answer_with_figures(answer, chunks)
        assert out.index("3. Enter") < out.index("a/fig_02.png") < out.index("4. Click")
        assert out.index("**Screenshots**") < out.index("b/fig_09.png")
