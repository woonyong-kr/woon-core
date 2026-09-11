from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge import workflow


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def request() -> dict:
    return {
        "job_id": "example-job",
        "target": "notes/example.md",
        "provider": "codex",
        "source": {"text": "새 자료는 3개의 원소를 설명한다."},
        "instruction": "기존 사실을 보존하고 새 자료를 통합해 주세요.",
    }


def prepare(tmp_path: Path, monkeypatch):
    note = tmp_path / "notes/example.md"
    note.parent.mkdir()
    original = (
        '---\ntags: ["study"] # keep this comment\n---\n\n## 기존 설명\n\n원래 문장이다. ^old\n'
    )
    note.write_text(original)
    calls = []

    def model(provider, prompt, schema, **kwargs):
        data = json.loads(prompt.split("\nDATA\n", 1)[1])
        calls.append(data["stage"])
        if data["stage"] == "generate":
            return {
                "body": data["original_body"] + "\n" + data["source"] + "\n",
                "summary": "기존 사실과 새 자료를 함께 설명했다.",
            }
        return {
            "verdict": "passed",
            **data["hashes"],
            "checks": [
                {
                    "criterion": criterion,
                    "passed": True,
                    "reason": "제공된 문장에서 확인했다.",
                    "source_quote": "3개의 원소",
                    "original_quote": "원래 문장",
                    "proposal_quote": "3개의 원소",
                }
                for criterion in workflow.CRITERIA
            ],
        }

    monkeypatch.setattr(workflow, "run_model", model)
    monkeypatch.setattr(workflow, "_obsidian_running", lambda: False)
    return note, original, calls, model


def test_workflow_applies_once_and_undo_preserves_exact_original(tmp_path, monkeypatch):
    note, original, calls, _ = prepare(tmp_path, monkeypatch)
    result = workflow.run_workflow(tmp_path, request())
    assert result["state"] == "applied"
    assert note.read_text().startswith(original)
    assert calls == ["generate", "review"]
    assert workflow.run_workflow(tmp_path, request())["state"] == "applied"
    assert calls == ["generate", "review"]
    assert workflow.undo_workflow(tmp_path, result["job_id"])["state"] == "undone"
    assert note.read_bytes() == original.encode()
    assert workflow.undo_workflow(tmp_path, result["job_id"])["state"] == "undone"


@pytest.mark.parametrize("change", ["edit", "delete", "cancel"])
def test_workflow_does_not_overwrite_a_change_during_review(tmp_path, monkeypatch, change):
    note, original, _, model = prepare(tmp_path, monkeypatch)

    def interleave(*args, **kwargs):
        value = model(*args, **kwargs)
        if "checks" in value:
            if change == "edit":
                note.write_text("직접 작성한 최신 문장")
            elif change == "delete":
                note.unlink()
            else:
                workflow.cancel_workflow(tmp_path, "example-job")
        return value

    monkeypatch.setattr(workflow, "run_model", interleave)
    result = workflow.run_workflow(tmp_path, request())
    assert result["state"] == ("cancelled" if change == "cancel" else "held")
    if change == "delete":
        assert not note.exists()
    else:
        assert note.read_text() == ("직접 작성한 최신 문장" if change == "edit" else original)


def test_undo_does_not_overwrite_later_user_edit(tmp_path, monkeypatch):
    note, _, _, _ = prepare(tmp_path, monkeypatch)
    workflow.run_workflow(tmp_path, request())
    note.write_text("작업 후의 새 편집")
    result = workflow.undo_workflow(tmp_path, "example-job")
    assert result["state"] == "applied"
    assert "changed" in result["undo_error"]
    assert note.read_text() == "작업 후의 새 편집"


def test_review_with_wrong_hash_cannot_authorize_write(tmp_path, monkeypatch):
    note, original, _, model = prepare(tmp_path, monkeypatch)

    def wrong_revision(*args, **kwargs):
        result = model(*args, **kwargs)
        if "checks" in result:
            result["before_sha256"] = "0" * 64
        return result

    monkeypatch.setattr(workflow, "run_model", wrong_revision)
    assert workflow.run_workflow(tmp_path, request())["state"] == "held"
    assert note.read_text() == original


def test_compiler_owned_notes_and_path_escape_are_not_generic_targets(tmp_path, monkeypatch):
    note, original, calls, _ = prepare(tmp_path, monkeypatch)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/canonical-knowledge.yaml").write_text(
        "version: 2\ncanonical:\n  root: notes\n"
    )
    for target in ("notes/example.md", "../outside.md"):
        with pytest.raises(WoonError):
            workflow.run_workflow(tmp_path, {**request(), "target": target})
    assert not calls
    assert note.read_text() == original


@pytest.mark.parametrize("undo", [False, True])
def test_write_is_recovered_when_the_final_receipt_was_not_saved(tmp_path, monkeypatch, undo):
    note, original, calls, _ = prepare(tmp_path, monkeypatch)
    if undo:
        workflow.run_workflow(tmp_path, request())
    save = workflow._save

    class ProcessStopped(BaseException):
        pass

    def stop_after_write(directory, record):
        if record["state"] == ("undone" if undo else "applied"):
            raise ProcessStopped
        return save(directory, record)

    monkeypatch.setattr(workflow, "_save", stop_after_write)
    with pytest.raises(ProcessStopped):
        if undo:
            workflow.undo_workflow(tmp_path, "example-job")
        else:
            workflow.run_workflow(tmp_path, request())
    monkeypatch.setattr(workflow, "_save", save)
    recovered = workflow.workflow_status(tmp_path, "example-job")
    assert recovered["state"] == ("undone" if undo else "applied")
    assert recovered["recovered"]
    assert calls == ["generate", "review"]
    if undo:
        assert note.read_bytes() == original.encode()


def test_open_obsidian_without_bridge_holds_before_writing(tmp_path, monkeypatch):
    note, original, _, _ = prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(workflow, "_obsidian_running", lambda: True)
    assert workflow.run_workflow(tmp_path, request())["state"] == "held"
    assert note.read_text() == original


def test_case_variant_cannot_bypass_the_canonical_writer(tmp_path, monkeypatch):
    note, _, _, _ = prepare(tmp_path, monkeypatch)
    variant = tmp_path / "NOTES/example.md"
    if not variant.exists() or not variant.samefile(note):
        pytest.skip("requires a case-insensitive filesystem")
    (tmp_path / "config").mkdir()
    (tmp_path / "config/canonical-knowledge.yaml").write_text(
        "version: 2\ncanonical:\n  root: notes\n"
    )
    with pytest.raises(WoonError, match="owner writer"):
        workflow.run_workflow(tmp_path, {**request(), "target": "NOTES/example.md"})


def test_successful_bridge_response_requires_a_matching_note(tmp_path, monkeypatch):
    note, original, _, _ = prepare(tmp_path, monkeypatch)
    runtime = tmp_path / workflow.RUNTIME
    runtime.mkdir(parents=True)
    (runtime / "bridge.json").write_text('{"port":1,"token":"test"}')

    class LyingBridge:
        status = 200

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return self

        def read(self, limit):
            proposed = (runtime / "jobs/example-job/after.md").read_bytes()
            return json.dumps({"sha256": hashlib.sha256(proposed).hexdigest()}).encode()

        def close(self):
            pass

    monkeypatch.setattr(workflow.http.client, "HTTPConnection", lambda *a, **k: LyingBridge())
    assert workflow.run_workflow(tmp_path, request())["state"] == "held"
    assert note.read_text() == original


@pytest.mark.parametrize(
    "snippet",
    [
        "~~~python\nold()\n~~~",
        "Use ``old()`` here.",
        "    old()\n",
        "````python\nold()\n````",
    ],
)
def test_all_existing_markdown_code_is_preserved(snippet):
    with pytest.raises(WoonError, match="code"):
        workflow._mechanical(snippet, snippet.replace("old()", "new()"), "new()")


def test_empty_frontmatter_is_a_valid_note():
    assert workflow._split_frontmatter("---\n---\nBody\n") == ("---\n---\n", "Body\n")


@pytest.mark.parametrize(
    ("original", "changed"),
    [
        ("> ~~~python\n> old()\n> ~~~\n", "> ~~~python\n> new()\n> ~~~\n"),
        (
            "    save_original()\n    overwrite_note()\n",
            "    overwrite_note()\n    save_original()\n",
        ),
    ],
)
def test_nested_code_and_statement_order_are_preserved(original, changed):
    with pytest.raises(WoonError, match="code"):
        workflow._mechanical(original, changed, "")


def test_explicit_retry_reuses_the_verified_proposal_without_another_model_call(
    tmp_path,
    monkeypatch,
):
    note, _, calls, _ = prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(workflow, "_obsidian_running", lambda: True)
    assert workflow.run_workflow(tmp_path, request())["state"] == "held"
    monkeypatch.setattr(workflow, "_obsidian_running", lambda: False)
    assert workflow.run_workflow(tmp_path, request())["state"] == "applied"
    assert calls == ["generate", "review"]
    assert "error" not in workflow.workflow_status(tmp_path, "example-job")
    assert "3개의 원소" in note.read_text()


@pytest.mark.parametrize("undo", [False, True])
@pytest.mark.parametrize("outcome", ["late_write", "stopped", "unknown", "crash", "arrived"])
def test_lost_bridge_response_cannot_confirm_cancellation_before_the_writer_stops(
    tmp_path,
    monkeypatch,
    undo,
    outcome,
):
    note, _, calls, _ = prepare(tmp_path, monkeypatch)
    if undo:
        workflow.run_workflow(tmp_path, request())
    unchanged = note.read_bytes()
    active = "undoing" if undo else "applying"
    runtime = tmp_path / workflow.RUNTIME
    runtime.mkdir(parents=True, exist_ok=True)
    bridge = runtime / "bridge.json"
    if outcome == "arrived":
        save = workflow._save

        def bridge_after_journal(directory, record):
            save(directory, record)
            if record["state"] == active and not bridge.exists():
                bridge.write_text('{"port":1,"token":"test"}')

        monkeypatch.setattr(workflow, "_save", bridge_after_journal)
    else:
        bridge.write_text('{"port":1,"token":"test"}')

    class ProcessStopped(BaseException):
        pass

    class DelayedBridge:
        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            record = json.loads((runtime / "jobs/example-job/job.json").read_bytes())
            assert record["state"] == active and record["bridge_pending"]
            if outcome == "crash":
                raise ProcessStopped
            raise TimeoutError("the request is still running inside Obsidian")

        def close(self):
            pass

    monkeypatch.setattr(workflow.http.client, "HTTPConnection", lambda *a, **k: DelayedBridge())

    def app_state():
        if outcome == "unknown":
            raise WoonError("process status is unavailable")
        return True

    monkeypatch.setattr(workflow, "_obsidian_running", app_state)

    def run():
        return (
            workflow.undo_workflow(tmp_path, "example-job")
            if undo
            else workflow.run_workflow(tmp_path, request())
        )

    if outcome == "crash":
        with pytest.raises(ProcessStopped):
            run()
    else:
        assert run()["state"] == active
    assert workflow.workflow_status(tmp_path, "example-job")["state"] == active
    assert workflow.cancel_workflow(tmp_path, "example-job")["state"] == active
    assert note.read_bytes() == unchanged
    if outcome == "stopped":
        monkeypatch.setattr(workflow, "_obsidian_running", lambda: False)
        recovered = workflow.workflow_status(tmp_path, "example-job")
        assert recovered["state"] == ("applied" if undo else "held")
        assert "bridge_pending" not in recovered
        assert workflow.cancel_workflow(tmp_path, "example-job")["state"] == (
            "applied" if undo else "cancelled"
        )
        assert note.read_bytes() == unchanged
    else:
        desired = "before.md" if undo else "after.md"
        note.write_bytes((runtime / "jobs/example-job" / desired).read_bytes())
        recovered = workflow.workflow_status(tmp_path, "example-job")
        assert recovered["state"] == ("undone" if undo else "applied")
        assert "bridge_pending" not in recovered
    assert calls == ["generate", "review"]


def test_process_probe_timeout_is_an_unknown_app_state(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("pgrep", 5)

    monkeypatch.setattr(workflow.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(workflow.subprocess, "run", timeout)
    with pytest.raises(WoonError, match="could not determine"):
        workflow._obsidian_running()


def test_markdown_reference_destinations_cannot_change():
    original = "[reference][docs]\n\n  [docs]: https://example.com/original\n"
    changed = original.replace("/original", "/different")
    with pytest.raises(WoonError, match="link"):
        workflow._mechanical(original, changed, "")


def test_a_link_from_extracted_html_can_be_rendered_as_markdown():
    workflow._mechanical(
        "## Source\n",
        "## Source\n\n[reference](https://example.com/docs)\n",
        "(https://example.com/docs) reference",
    )
    with pytest.raises(WoonError, match="link"):
        workflow._mechanical(
            "## Source\n",
            "## Source\n\n[reference](https://example.com/docs)\n",
            "(https://example.com/docs-unrelated) reference",
        )
