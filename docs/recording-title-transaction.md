# Local recording title transaction

`KnowledgeService.apply_wiki_restructure_transaction(..., recording_bundle=bundle)`
accepts one `RecordingTitleBundle` alongside the existing compiler transaction and
native `ManualWikiWrite` inputs. `ResourceFileRename` remains limited to unchanged
PDF/HTML bytes. Native-only transactions keep their existing ownership.

The bundle contains existing hash-pinned reader renames, archive catalog, current
references, local renderer source, and optional existing raw-archive source owners.
Readers retain their stable recording IDs, original transcript bytes, dates and
private metadata. Catalog changes retain source hashes, original size, provider
state and history. Corrections permit title/current-link edits; source redirects
permit one current-link target edit and matching owner hash/locator updates.
Index/design prose and renderer source are reviewed exact bytes; the adapter does
not execute the renderer or prove its output. Producer replay is a separate check.

All participants preflight under the existing repository mutation lock, recheck
before writing, and join compiler/index exception recovery. Renamed targets and
restored original paths use exclusive publication. Recovery preserves concurrent
bytes and reports conflicts while attempting every owner. Final readback checks
each recording target's bytes/mode and absence of renamed originals. Successful
reports include `recording_files_written`. Callers retain the reviewed manifests,
before bytes, dependency scope and final report as their operation evidence.

Reusing stale before pins fails without duplicating content. This is an in-process
filesystem recovery boundary for cooperating locked writers, not a durable
multi-file commit or a lock against arbitrary external editors. Process-kill and
power-loss recovery are not guaranteed. A conflict or interrupted result requires
readback and re-planning, never blind replay of historical plans or receipts.
