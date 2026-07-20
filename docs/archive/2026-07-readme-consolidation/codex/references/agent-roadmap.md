# Agent Roadmap

## Active Target Architecture

The current target architecture is:

```text
discover -> classify -> select -> bootstrap -> run -> explain -> convert
```

With the artifact split:

```text
YAML control plane -> SQLite/download evidence plane -> Markdown explanation plane
```

## What Is Already Real

- staged section discovery
- staged section classification
- reviewed section selection
- scope compilation into `monitor_scope.yaml`
- scope-driven tree bootstrap
- scope-driven reruns
- bootstrap summary and document manifest export

## What Comes Next

1. add a first-class `monitor_intent.yaml`
2. improve rerun change bundles so they group changes by selected business branches
3. add conversion-routing outputs for downstream `doc_to_md`
4. expose the staged tree workflow through stable interfaces beyond `tools/*.py`
5. roll the section-aware process out to the 30+ smoke catalog

## Contract Rules

- every agent-facing output should preserve evidence pointers
- `sha256` remains the final file dedupe authority
- `_tracked` should be preferred for browsing, `_blobs` for canonical storage
- planning state should live in files, not only in chat context
