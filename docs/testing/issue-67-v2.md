# Issue 67 regression contracts

`build_acquisition_batch_result_v2` projects already-derived dispositions;
`acquisition_batch_result_v2_from_scope_run` derives them from terminal CrawlRun
counters and explicit acquisition classifications. Successful dispositions require
a caller-supplied real run artifact identity. A completed run with no observations
and no changes is failed evidence (`scope.no_observations`), not an in-flight task.

`aggregate_batch_result_v2` (also exported as `aggregate_batch_result`) accepts v2
records. It performs no I/O, preserves inputs, sorts by site key and requested URL,
and unions task identities. Identical records collapse; inconsistent task data,
source-run authority, or evidence counts raise ValueError. Evidence totals are
counted once per distinct source run and task set; they are observation counters,
not requested-site counts. v1 remains available unchanged. No automatic v1 success
upgrade is provided because v1 lacks updated/unchanged change evidence.

The CLI `aggregate-batch-result --input PATH --json` reads a single v2 record or
list and emits canonical JSON. Invalid input exits 2. The CLI's file read is
outside the pure aggregate function. `counts.failed` is exclusive of blocked;
`summary.failed` includes blocked. `summary.checked` excludes unresolved.

Manifest exports bind to the explicitly requested run and its earlier snapshots,
regardless of the current baseline or latest tracked hash/document. The producer
uses `find_new_links` for link differences and `compute_diff` for recorded hash
comparisons. Existing tracking-report bundle helpers rely on mutable latest-run
fields and are deliberately not used for historical classification.

First-scope observations have `status` and `item_state` equal to `existing`.
Subsequent genuinely unseen URLs are `new`. A fetched target with changed hash
is `changed`; a referring list page changing does not imply its linked bodies
changed. Links absent from a re-observed referring page are `missing`; an
unvisited/failed page alone does not prove removal. URLs moving between observed
list pages remain `existing`. Discovery does not assert successful body retrieval.
The vocabulary remains `new`, `changed`, `missing`, `existing`, `removed` (the
producer uses `missing`, not `removed`). Titles without source evidence are null.

FileObservation supplies file links and associated assets. Historical export
passes `include_legacy_fallback=False` to Storage.list_scope_documents so later
tracked documents cannot leak into old observations. Other legacy callers retain
the default fallback. Missing file items have no newly downloaded asset.

The two JSON files under `tests/fixtures/` are regression fixtures, not current
production. The batch fixture derives 33 updated, 9 unchanged, 14 blocked, and 1
failed from synthesized terminal run evidence. The manifest fixture was emitted
by the real producer using temporary Storage and contains three page links plus
one file observation. The local minimal reader verifies the documented handoff
shape; no climate implementation is vendored or modified.

The locked plan additionally requires v2 parity across run-scope, persisted Jobs,
and Job API. Those paths still use v1: Job persistence and API validation are
outside the initial worker's owned files. This criterion remains pending manager
scope authorization and is not claimed complete by these contract tests.

Downloaded document rows themselves are mutable by download URL. Where available,
the producer recovers each observation's SHA-256 and timestamp from its persisted
capture result and resolves that digest through the existing document blob index.
This preserves old assets and changed-file evidence after later same-URL updates.
For legacy observations without a capture digest that share a document row with
a later observation, the historical body cannot be recovered: its discovered URL
is retained without borrowing the later body or inventing a title.
