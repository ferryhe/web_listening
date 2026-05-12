# Project Status

- Date: 2026-05-12
- Project: web_listening
- Branch: codex/pr1-acquisition-contracts
- Run type: PR1 scoped implementation plus remote review fixes
- Scope: acquisition profile and capture attempt contract only; no crawler, staged workflow, manifest/report integration, or CloakBrowser dependency.
- Code changes: added `web_listening/blocks/acquisition_profile.py` with Pydantic models, YAML load/render helpers, default profile builder, adapter recommendation helper, safety validation for `cloakbrowser`, explicit stealth authorization handling, disabled-adapter skipping, strict top-level field validation, clean non-mapping YAML root errors, and typed next-adapter recommendations.
- Tests/docs: added focused unit tests, contract documentation, sample YAML fixture, light README/docs index mentions, and review-fix regressions for authorization, disabled adapters, unknown fields, invalid YAML roots, invalid recommended next adapters, and allowed-domain error wording.
- Verification: `.venv\Scripts\python -m pytest tests\test_acquisition_profile.py -q` passed with 14 tests; `.venv\Scripts\python -m pytest tests -q` passed with 203 tests; `git diff --check` exited 0 with only line-ending warnings on touched PR1 files.
- Current conclusion: the fixed workflow remains unchanged; acquisition variability is now represented as a standalone control artifact.
- Next recommended action: after PR1 review, implement the next PR that consumes these contracts from the acquisition/capture layer without changing staged workflow outputs.
