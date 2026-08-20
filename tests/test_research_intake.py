import hashlib
import json
import subprocess
from io import StringIO
from pathlib import Path

import pytest

from woon_core.cli import run
from woon_core.errors import WoonError
from woon_core.knowledge import research_intake
from woon_core.knowledge.research_intake import (
    create_research_intake_plan,
    export_notebooklm_artifact,
)


def test_research_intake_plans_zotero_metadata_and_notebooklm_artifacts(tmp_path: Path) -> None:
    zotero = tmp_path / "library.json"
    zotero.write_text(
        json.dumps(
            [
                {
                    "citationKey": "Attention2017",
                    "title": "Attention Is All You Need",
                    "DOI": "https://doi.org/10.48550/arXiv.1706.03762",
                    "date": "2017-06-12",
                },
                {
                    "id": "GPTQ2022",
                    "title": "GPTQ",
                    "arXiv": "2210.17323",
                },
            ]
        ),
        encoding="utf-8",
    )
    artifact = tmp_path / "report.md"
    artifact.write_text("# Research brief\n\nA grounded synthesis.\n", encoding="utf-8")
    manifest = tmp_path / "notebooklm.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "tool": {"name": "nlm", "revision": "a" * 40},
                "artifacts": [
                    {
                        "artifact_id": "artifact-1",
                        "kind": "report",
                        "path": "report.md",
                        "sha256": _sha256(artifact),
                        "source_refs": ["arxiv:2210.17323"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    plan = create_research_intake_plan(
        purpose="양자화 방법의 학습 자료를 검증 가능한 근거로 정리한다.",
        zotero_export=zotero,
        notebooklm_manifest=manifest,
    )

    assert plan["summary"] == {
        "records": 3,
        "metadata_ready": 2,
        "review_required": 1,
        "duplicate_identities": 0,
    }
    records = plan["records"]
    assert isinstance(records, list)
    assert records[0]["canonical"] is False
    assert records[-1]["state"] == "metadata-ready"
    assert str(tmp_path) not in json.dumps(plan)


def test_research_intake_rejects_changed_notebooklm_markdown(tmp_path: Path) -> None:
    artifact = tmp_path / "report.md"
    artifact.write_text("# First\n", encoding="utf-8")
    manifest = tmp_path / "notebooklm.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "tool": {"name": "nlm", "revision": "b" * 40},
                "artifacts": [
                    {
                        "artifact_id": "artifact-1",
                        "kind": "report",
                        "path": "report.md",
                        "sha256": _sha256(artifact),
                        "source_refs": ["doi:10.1000/example"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    artifact.write_text("# Changed\n", encoding="utf-8")

    with pytest.raises(WoonError, match="hash mismatch"):
        create_research_intake_plan(
            purpose="NotebookLM 출력을 검토한다.", notebooklm_manifest=manifest
        )


def test_research_intake_rejects_video_url_in_notebooklm_output(tmp_path: Path) -> None:
    artifact = tmp_path / "report.md"
    artifact.write_text("https://www.youtube.com/watch?v=example\n", encoding="utf-8")
    manifest = tmp_path / "notebooklm.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "tool": {"name": "nlm", "revision": "c" * 40},
                "artifacts": [
                    {
                        "artifact_id": "artifact-1",
                        "kind": "report",
                        "path": "report.md",
                        "sha256": _sha256(artifact),
                        "source_refs": ["doi:10.1000/example"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match="prohibited video URL"):
        create_research_intake_plan(
            purpose="영상 링크가 섞이지 않게 검토한다.", notebooklm_manifest=manifest
        )


def test_research_intake_rejects_unresolvable_notebooklm_source_ref(tmp_path: Path) -> None:
    artifact = tmp_path / "report.md"
    artifact.write_text("# Research brief\n", encoding="utf-8")
    manifest = tmp_path / "notebooklm.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "tool": {"name": "nlm", "revision": "d" * 40},
                "artifacts": [
                    {
                        "artifact_id": "artifact-1",
                        "kind": "report",
                        "path": "report.md",
                        "sha256": _sha256(artifact),
                        "source_refs": ["doi:not-a-doi"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match="valid doi: or arxiv: IDs"):
        create_research_intake_plan(purpose="출처 식별자를 검토한다.", notebooklm_manifest=manifest)


def test_research_intake_rejects_notebooklm_ref_missing_from_selected_zotero_export(
    tmp_path: Path,
) -> None:
    zotero = tmp_path / "library.json"
    zotero.write_text(
        json.dumps([{"id": "retained", "title": "Retained", "DOI": "10.1000/retained"}]),
        encoding="utf-8",
    )
    artifact = tmp_path / "report.md"
    artifact.write_text("# Research brief\n", encoding="utf-8")
    manifest = tmp_path / "notebooklm.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "tool": {"name": "nlm", "revision": "e" * 40},
                "artifacts": [
                    {
                        "artifact_id": "artifact-1",
                        "kind": "report",
                        "path": "report.md",
                        "sha256": _sha256(artifact),
                        "source_refs": ["doi:10.1000/not-retained"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match="do not match the Zotero export"):
        create_research_intake_plan(
            purpose="NotebookLM 근거가 선택한 문헌에 있는지 확인한다.",
            zotero_export=zotero,
            notebooklm_manifest=manifest,
        )


def test_research_intake_cli_writes_a_deterministic_plan(tmp_path: Path) -> None:
    zotero = tmp_path / "library.json"
    zotero.write_text(
        json.dumps([{"id": "paper", "title": "A Paper", "DOI": "10.1000/example"}]),
        encoding="utf-8",
    )
    output_path = tmp_path / "plan.json"
    output = StringIO()

    run(
        [
            "knowledge",
            "research-intake-plan",
            "--purpose",
            "논문을 검증 가능한 자료로 정리한다.",
            "--zotero",
            str(zotero),
            "--output",
            str(output_path),
        ],
        output,
    )

    assert '"metadata_ready": 1' in output.getvalue()
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["records"][0]["identity"] == "doi:10.1000/example"


def test_research_intake_cli_explains_its_contract() -> None:
    output = StringIO()

    run(["knowledge", "research-intake-plan", "--help"], output)

    assert "Builds an offline review plan" in output.getvalue()
    assert "--zotero <CSL-JSON>" in output.getvalue()


def test_research_intake_cli_verifies_matching_zotero_and_notebooklm_inputs(
    tmp_path: Path,
) -> None:
    zotero = tmp_path / "library.json"
    zotero.write_text(
        json.dumps([{"id": "paper", "title": "A Paper", "arXiv": "2401.00001"}]),
        encoding="utf-8",
    )
    artifact = tmp_path / "study-guide.md"
    artifact.write_text("# Study guide\n\nKeep this as a review candidate.\n", encoding="utf-8")
    manifest = tmp_path / "notebooklm.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "tool": {"name": "nlm", "revision": "f" * 40},
                "artifacts": [
                    {
                        "artifact_id": "guide-1",
                        "kind": "study-guide",
                        "path": "study-guide.md",
                        "sha256": _sha256(artifact),
                        "source_refs": ["arxiv:2401.00001"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output = StringIO()

    run(
        [
            "knowledge",
            "research-intake-plan",
            "--purpose",
            "논문과 학습 보조물을 분리해 검토한다.",
            "--zotero",
            str(zotero),
            "--notebooklm-manifest",
            str(manifest),
        ],
        output,
    )

    plan = json.loads(output.getvalue())
    artifact_record = next(
        record for record in plan["records"] if record["kind"] == "notebooklm-derived"
    )
    assert artifact_record["source_refs_verified_by"] == "zotero-export"


def test_research_intake_requires_at_least_one_input() -> None:
    with pytest.raises(WoonError, match="requires --zotero or --notebooklm-manifest"):
        create_research_intake_plan(purpose="근거를 정리한다.")


def test_notebooklm_export_writes_hashed_manifest_after_one_artifact_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    markdown = tmp_path / "export" / "study-guide.md"
    manifest = tmp_path / "export" / "notebooklm-export.json"
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        Path(command[-1]).write_text("# Study guide\n\nDerived material.\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(research_intake.subprocess, "run", fake_run)

    result = export_notebooklm_artifact(
        artifact_id="artifact-1",
        kind="study-guide",
        source_refs=("doi:10.1000/example", "arxiv:2401.00001"),
        tool_revision="a" * 40,
        output_markdown=markdown,
        manifest_output=manifest,
        nlm_binary="/opt/tools/nlm",
    )

    assert calls == [
        [
            "/opt/tools/nlm",
            "artifact",
            "export",
            "artifact-1",
            "--format",
            "md",
            "--output",
            str(markdown),
        ]
    ]
    written = json.loads(manifest.read_text(encoding="utf-8"))
    assert written["artifacts"][0]["sha256"] == _sha256(markdown)
    assert result["state"] == "derived-review-required"


def test_notebooklm_export_refuses_existing_outputs_before_calling_exporter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    markdown = tmp_path / "report.md"
    markdown.write_text("# Existing\n", encoding="utf-8")

    def unexpected_run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("exporter must not run")

    monkeypatch.setattr(research_intake.subprocess, "run", unexpected_run)

    with pytest.raises(WoonError, match="output already exists"):
        export_notebooklm_artifact(
            artifact_id="artifact-1",
            kind="report",
            source_refs=("doi:10.1000/example",),
            tool_revision="b" * 40,
            output_markdown=markdown,
            manifest_output=tmp_path / "notebooklm.json",
        )


def test_notebooklm_export_cli_explains_its_contract() -> None:
    output = StringIO()

    run(["knowledge", "notebooklm-export", "--help"], output)

    assert "Downloads one already-generated NotebookLM artifact" in output.getvalue()
    assert "--source-ref <doi-or-arxiv>" in output.getvalue()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
