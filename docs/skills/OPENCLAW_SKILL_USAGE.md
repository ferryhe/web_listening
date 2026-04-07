# OpenClaw Skill Usage

- Workspace skill path: `skills/web-listening-tree-monitor`
- Main file: `skills/web-listening-tree-monitor/SKILL.md`
- UI metadata: `skills/web-listening-tree-monitor/agents/openai.yaml`
- Current design reference: `docs/design/AGENT_SCOPE_PLANNING_DESIGN.md`
- Current roadmap reference: `docs/roadmap/AGENT_SITE_MONITORING_MASTER_PLAN.md`

This workspace skill mirrors the repo's tree-monitoring workflow so OpenClaw can load it directly from the repository workspace and use the same bootstrap/run commands as Codex.

The intended OpenClaw exchange pattern is now:

- YAML or config files for step-to-step planning handoff
- SQLite and blob storage for evidence
- Markdown reports for human review
