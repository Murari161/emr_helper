"""Tests for swapping a summary procedure for its full version (app/rag/canonical.py).

Pure functions only — no database:
    docker compose exec app pytest tests/test_canonical.py -v

What each test catches:

  test_module_key
      Manual names in "See module:" markers and in documents.title line up
      ("Patient Management" vs "Patient Management Module").

  test_referenced_modules
      Every "See module:" marker is read, in order; "See:" (same-manual)
      markers are ignored.

  test_summary_by_its_own_wording
      The Nursing summary of "Register a patient" says it is summarised there,
      so the Patient Management procedure replaces it even though the summary
      has MORE (cut-out) screenshots.

  test_summary_without_screenshots
      A summary with no screenshots gives way to a same-titled procedure that
      has them, even without the wording.

  test_related_procedure_is_not_a_summary
      HR's "Approve a leave request" points to the Employee Portal's procedure
      of the same title, but neither says it is a summary and neither has
      screenshots the other lacks — it must NOT be redirected.

  test_never_swap_in_a_placeholder
      A "not yet documented" stub is never a full version.
"""
from __future__ import annotations

from app.rag import canonical

NURSING_REGISTER = {
    "id": 1, "kind": "procedure", "title": "Register a patient",
    "content": "Registration is performed in the Patient Management module; it is summarised here "
               "because outpatient nursing often starts with a new patient.\n"
               "See module: Patient Management\nSteps:\nOpen the Patient Management module.",
    "images": [{"path": "a"}, {"path": "b"}, {"path": "c"}, {"path": "d"}, {"path": "e"}],
}
PM_REGISTER = {
    "id": 2, "kind": "procedure", "title": "Register a patient",
    "content": "Steps:\nClick on the Register Patient tab.\nClick on the +New button.",
    "images": [{"path": "x"}, {"path": "y"}, {"path": "z"}],
}


def test_module_key() -> None:
    assert canonical.module_key("Patient Management") == "patient management"
    assert canonical.module_key("Patient Management Module") == "patient management"
    assert canonical.module_key("  Direct  Service Workflows. ") == "direct service workflows"
    assert canonical.module_key("Imaging and Radiology Management Module") == "imaging and radiology management"


def test_referenced_modules() -> None:
    chunk = {"content": "See module: Employee Portal\nSee module: User and Facility Management\n"
                        "See: Add an employee\nSteps:\nGo to Leave Management."}
    assert canonical.referenced_modules(chunk) == ["employee portal", "user and facility management"]
    assert canonical.referenced_modules({"content": "Steps:\nClick Save."}) == []


def test_summary_by_its_own_wording() -> None:
    assert canonical.is_summary_of(NURSING_REGISTER, PM_REGISTER)
    assert not canonical.is_summary_of(NURSING_REGISTER, NURSING_REGISTER)      # never itself


def test_summary_without_screenshots() -> None:
    summary = {"id": 3, "title": "Admit directly to the morgue", "images": [],
               "content": "See module: Direct Service Workflows\nSteps:\nRegister the individual, then choose Morgue Admission."}
    full = {"id": 4, "title": "Admit directly to the morgue", "images": [{"path": "m"}],
            "content": "Steps:\nOn the Register Patient tab, search for the individual."}
    assert canonical.is_summary_of(summary, full)


def test_related_procedure_is_not_a_summary() -> None:
    hr = {"id": 5, "title": "Approve a leave request", "images": [],
          "content": "See module: Employee Portal\nSteps:\nGo to Leave Management and click the Leave Request tab.\n"
                     "Note: Employees raise the request in the Employee Portal module."}
    portal = {"id": 6, "title": "Approve a leave request", "images": [],
              "content": "Steps:\nClick the Leave Requests tab."}
    assert not canonical.is_summary_of(hr, portal)


def test_never_swap_in_a_placeholder() -> None:
    stub = {"id": 7, "title": "Register a patient (not yet documented)", "images": [{"path": "s"}],
            "content": "Note: This procedure is not yet documented in this manual."}
    assert not canonical.is_summary_of(NURSING_REGISTER, stub)
