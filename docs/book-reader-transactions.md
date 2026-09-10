# Book reader transactions

Book navigation, presentation and reviewed prose updates use the existing compiler transaction. They preserve immutable source and claim payloads; a changed translation is a new source revision. The compiler owns the page H1, so source bodies must not include it.

## Navigation and empty wrappers

`CompiledWikiTransaction.book_navigation_rebindings` carries exact manifest and output pins through `KnowledgeService.apply_compiled_wiki_transaction`. Retiring an inspected empty wrapper requires its existing page retirement proof. A display-only rebinding preserves every canonical owner, source inventory, order and progress field. It changes only the declared delivery fields and verifies the resulting root links against real private Markdown files and headings.

Book-root H2/H3 links can point directly to private reader bodies. Missing files, ambiguous headings and symlink escapes fail validation. A virtual chapter scope remains bounded by the complete chapter node set in the pinned source structure; a path prefix alone does not establish ownership. Navigation changes do not mark source coverage complete.

The renderer collapses a repeated single-leaf section label into one link, preserves source order and keeps content-bearing section links. Optional `reader_leaf_navigation` on the book root derives previous/next links from actual content leaves. These generated links remain outside source payload hashes.

## Personal notes and prose corrections

A source-body renderer can attach separately sourced `supplemental_claim_ids` and stable `personal_footnotes`. Notes anchor to one exact source span and render below the source body. The renderer rejects ambiguous anchors, edits inside protected code or links, and colliding IDs. Original and supplemental runnable evidence remain separate and pinned.

Use a scoped coverage transaction for supplemental notes. Prose-only note revisions declare the existing note IDs, preserve retained anchors and order, and create successor claims. This path cannot alter or remove code-containing notes.

`BookCoverageManifestUpdate.korean_prose_edits` is consumed by the verified-book update service. It pins the source record, page specification, current reader and exact before/after body hashes. Only declared prose and corresponding delivery spans may change; personal notes, code, source identities and unrelated metadata remain intact. Preflight reproduces the reader and copies only required pinned evidence into an isolated Vault.

## Validation and recovery

The service checks revisions under the existing repository lock and snapshots affected compiler inputs, coverage files, outputs and generated navigation. A write, final validation or search-index failure restores the previous state. Preflight does not run book examples or modify the live Vault.

Verified-book updates default to `audit_scope="all"`. The existing `"affected"` option retains pre-existing audit errors in its report, rejects new errors, and separately verifies affected receipts and scoped coverage. It does not repair or hide unrelated provenance errors.

The `knowledge apply-compiled-transaction` CLI accepts book `coverage_manifest`, `expected_catalog_revision` and `expected_page_spec_sha256` fields. The book coverage parser also accepts explicit `runnable_support_corrections`; exact prose and note revision requests use their service API contracts. A correction requires pinned source and execution evidence and does not constitute a new code run.
