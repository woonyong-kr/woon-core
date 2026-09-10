import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from test_wiki_restructure import _compiled_service, _v2_record, _write_page

import woon_core.knowledge.recording_titles as recording_titles
from woon_core.errors import WoonError
from woon_core.knowledge.compiled_wiki import CompilationAudit
from woon_core.knowledge.recording_titles import RecordingTitleBundle
from woon_core.knowledge.service import ManualWikiWrite
from woon_core.knowledge.wiki_restructure import _v2_compiler_transaction


def _write(vault: Path, path: str, before: bytes, after: bytes, target=None) -> ManualWikiWrite:
    file = vault / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(before)
    file.chmod(0o600)
    return ManualWikiWrite(
        path,
        hashlib.sha256(before).hexdigest(),
        target or path,
        hashlib.sha256(after).hexdigest(),
        after,
    )


def _fixture(vault: Path):
    archive = "private/knowledge/voice-memos/"
    intake = archive + "intake-20260909/"
    old, new = archive + "recordings/2026/old.md", archive + "recordings/2026/new.md"
    private = "access: local-only\npublication_state: private\npublish: false\n"
    reader = (
        "---\ntitle: old\nrecord_kind: recording\nrecording_id: recording-one\n"
        + private
        + "recorded_at: 2026-05-16\naudio_verified: false\n---\n\n"
        "# old\n\nOriginal transcript.\n"
    ).encode()
    read = _write(
        vault,
        old,
        reader,
        reader.replace(b"title: old", b"title: new").replace(b"# old", b"# new"),
        new,
    )
    row = {
        "recording_id": "recording-one",
        "intake_record": 1,
        "intake_path": intake.rstrip("/"),
        "title": "old",
        "reader_path": old,
        "audio_sha256": "original-audio-hash",
        "previous_reader_path": "historical.md",
    }
    catalog = {"access": "local-only", "publication_state": "private", "records": [row]}
    revised = json.loads(json.dumps(catalog))
    revised["records"][0].update(title="new", reader_path=new)
    catalog_write = _write(
        vault,
        archive + "archive-catalog.json",
        json.dumps(catalog).encode(),
        json.dumps(revised).encode(),
    )
    correction = (
        '---\ntitle: "old 교정 기록"\n' + private + "---\n\n# old 교정 기록\n\n"
        f"[[{old[:-3]}|return]]\nPreserve correction anchors.\n"
    ).encode()
    revised_correction = correction.replace(old[:-3].encode(), new[:-3].encode())
    revised_correction = revised_correction.replace(b"old ", b"new ")
    correction_write = _write(
        vault, intake + "clovanote-corrections-local/record-01.md", correction, revised_correction
    )
    redirect_path = "private/novel/vault-source/projects/writing/interview-transcript-1.md"
    redirect = (
        "---\ntitle: Original pointer\n"
        + private
        + "role: transcript-redirect\nstatus: Superseded\n---\n\n# Original pointer\n\n"
        f"[[{old[:-3]}|old alias]]\nHistorical receipt.md remains.\n"
    ).encode()
    redirect_write = _write(
        vault, redirect_path, redirect, redirect.replace(old[:-3].encode(), new[:-3].encode())
    )
    owner = {
        "source_id": "source://raw-archive/" + redirect_path.removeprefix("private/"),
        "locator": redirect_path.removeprefix("private/"),
        "target": redirect_path,
        "role": "retired-transcript-pointer",
        "state": "canonical",
        "privacy": "private/local-only",
        "sha256": "immutable-original-hash",
        "size": 1234,
        "target_sha256": "previously-recorded-pointer-hash",
        "replaced_by": "historical.md",
    }
    owners = {"version": 1, "records": [owner]}
    original_owners = yaml.safe_dump(owners, sort_keys=False).encode()
    owner.update(target_sha256=redirect_write.target_sha256, replaced_by=new)
    owner_write = _write(
        vault,
        "catalog/sources/raw-archive-ownership.yaml",
        original_owners,
        yaml.safe_dump(owners, sort_keys=False).encode(),
    )
    producer = _write(
        vault,
        intake + "processing/render_clovanote_local.py",
        b"def main():\n    return 'old'\n",
        b"def main():\n    return 'catalog'\n",
    )
    table = (
        "---\ntitle: History\n" + private + "---\n\n# History\n\n"
        f"| old event | [[{old[:-3]}\\|old alias]] |\n"
    ).encode()
    new_table = table.replace(
        f"[[{old[:-3]}\\|old alias]]".encode(), f"[[{new[:-3]}\\|new]]".encode()
    )
    table_write = _write(vault, intake + "clovanote-transcript-replacement.md", table, new_table)
    bundle = RecordingTitleBundle(
        (read,),
        catalog_write,
        (correction_write, redirect_write, table_write),
        producer,
        owner_write,
    )
    _write_page(vault, "wiki/README.md", "README")
    _write_page(vault, "wiki/legacy/topic.md", "legacy/topic")
    compiler, service = _compiled_service(vault)
    manual = _write_page(vault, "wiki/manual/notes.md", "manual/notes")
    manual_before = manual.read_bytes() + f"\n[[{old[:-3]}|recording]]\n".encode()
    native_after = manual_before.replace(old[:-3].encode(), new[:-3].encode()).replace(
        b"parent: '[[wiki/README|Vault]]'",
        b"parent: '[[wiki/moved/topic|topic]]'",
    )
    native = _write(vault, "wiki/manual/notes.md", manual_before, native_after)
    service.reindex()
    payload = {
        "version": 2,
        "records": [
            _v2_record(vault, "wiki/README.md", action="keep"),
            _v2_record(
                vault,
                "wiki/legacy/topic.md",
                action="move",
                target_path="wiki/moved/topic.md",
                target_parent="wiki/README.md",
                target_parent_canonical_id="README",
                target_sequence=1,
            ),
        ],
    }
    transaction, _, _ = _v2_compiler_transaction(vault, payload)
    return compiler, service, transaction, native, bundle


def _snapshot(vault: Path):
    return {
        path.relative_to(vault): (path.read_bytes(), path.stat().st_mode & 0o777)
        for base in ("wiki", "catalog", "private")
        for path in (vault / base).rglob("*")
        if path.is_file()
    }


def test_recording_bundle_commits_with_native_compiler_and_rejects_stale_replay(tmp_path):
    compiler, service, transaction, native, bundle = _fixture(tmp_path)
    result = service.apply_wiki_restructure_transaction(
        transaction,
        (native,),
        recording_bundle=bundle,
    )
    assert result.recording_files_written == 7
    assert compiler.audit().errors == ()
    assert (tmp_path / native.current_path).read_bytes() == native.content
    for write in (
        *bundle.readers,
        *bundle.references,
        bundle.catalog,
        bundle.producer,
        bundle.source_owners,
    ):
        assert (tmp_path / write.target_path).read_bytes() == write.content
        assert (tmp_path / write.target_path).stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / bundle.readers[0].current_path).exists()
    committed = _snapshot(tmp_path)
    with pytest.raises(WoonError):
        service.apply_wiki_restructure_transaction(transaction, (native,), recording_bundle=bundle)
    assert _snapshot(tmp_path) == committed


@pytest.mark.parametrize(
    "failure",
    [
        "compiler",
        "index",
        "new-target-edit",
        "old-path-edit",
        "verification",
    ],
)
def test_recording_failure_recovers_all_owners_and_preserves_concurrent_bytes(
    tmp_path,
    monkeypatch,
    failure,
):
    compiler, service, transaction, native, bundle = _fixture(tmp_path)
    before = _snapshot(tmp_path)
    old, new = (
        tmp_path / path for path in (bundle.readers[0].current_path, bundle.readers[0].target_path)
    )
    calls = 0
    index_failure = failure in {"index", "verification"}
    original = service._reindex_unlocked if index_failure else compiler.audit

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure in {"new-target-edit", "verification"}:
                new.write_bytes(b"concurrent target edit")
            elif failure == "old-path-edit":
                old.write_bytes(b"concurrent original path")
            if failure == "index":
                raise WoonError("injected index failure")
            if failure == "verification":
                return original(*args, **kwargs)
            return CompilationAudit(0, 0, ("injected compiler failure",))
        return original(*args, **kwargs)

    monkeypatch.setattr(
        service if index_failure else compiler,
        "_reindex_unlocked" if index_failure else "audit",
        fail_once,
    )
    with pytest.raises(WoonError, match="injected|verification failed"):
        service.apply_wiki_restructure_transaction(transaction, (native,), recording_bundle=bundle)
    current = _snapshot(tmp_path)
    if failure in {"compiler", "index"}:
        assert current == before
    else:
        changed = old if failure == "old-path-edit" else new
        assert changed.read_bytes().startswith(b"concurrent")
        for path, content in before.items():
            if tmp_path / path != changed:
                assert current[path] == content
    # No successful compiler receipt/checkpoint survives the failed mixed transaction.
    assert (tmp_path / "catalog/llm-wiki/receipts.yaml").read_bytes() == before[
        Path("catalog/llm-wiki/receipts.yaml")
    ][0]


def test_destination_created_after_preflight_is_never_clobbered(tmp_path, monkeypatch):
    _, service, transaction, native, bundle = _fixture(tmp_path)
    before = _snapshot(tmp_path)
    publish = recording_titles._publish_new
    target = tmp_path / bundle.readers[0].target_path

    def competing_publish(path, content, mode):
        if path == target:
            path.write_bytes(b"a concurrently created destination")
        publish(path, content, mode)

    monkeypatch.setattr(recording_titles, "_publish_new", competing_publish)
    with pytest.raises(FileExistsError):
        service.apply_wiki_restructure_transaction(transaction, (native,), recording_bundle=bundle)
    assert target.read_bytes() == b"a concurrently created destination"
    current = _snapshot(tmp_path)
    assert all(current[path] == value for path, value in before.items())


def test_failure_inside_recording_participant_restores_prior_renames(tmp_path, monkeypatch):
    _, service, transaction, native, bundle = _fixture(tmp_path)
    before = _snapshot(tmp_path)
    original = recording_titles.atomic_write
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected reference write failure after reader rename")
        return original(*args, **kwargs)

    monkeypatch.setattr(recording_titles, "atomic_write", fail_once)
    with pytest.raises(OSError, match="injected reference"):
        service.apply_wiki_restructure_transaction(transaction, (native,), recording_bundle=bundle)
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("invalid", ["source-owner-stale", "correction-body", "original-sha"])
def test_invalid_recording_participant_fails_before_any_other_owner_writes(tmp_path, invalid):
    _, service, transaction, native, bundle = _fixture(tmp_path)
    if invalid == "source-owner-stale":
        bundle = replace(
            bundle,
            source_owners=replace(
                bundle.source_owners,
                current_sha256="0" * 64,
            ),
        )
    else:
        write = bundle.references[0] if invalid == "correction-body" else bundle.source_owners
        changed = (
            write.content.replace(b"Preserve correction anchors", b"Edited source evidence")
            if invalid == "correction-body"
            else write.content.replace(b"immutable-original-hash", b"rewritten-original-hash")
        )
        write = replace(write, content=changed, target_sha256=hashlib.sha256(changed).hexdigest())
        bundle = (
            replace(bundle, references=(write, *bundle.references[1:]))
            if invalid == "correction-body"
            else replace(bundle, source_owners=write)
        )
    before = _snapshot(tmp_path)
    with pytest.raises(WoonError):
        service.apply_wiki_restructure_transaction(transaction, (native,), recording_bundle=bundle)
    assert _snapshot(tmp_path) == before
