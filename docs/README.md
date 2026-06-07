# tmux-skills Docs

This directory is the LLM wiki for tmux-skills. It is the canonical place for detailed contracts, operational runbooks, and validation notes that are too large for the root README.

## Where to look first

| Task | Start here |
| --- | --- |
| Start as an LLM agent | [`../llms.txt`](../llms.txt) |
| Understand the repo quickly | [`../README.md`](../README.md) |
| Operate tmux safely as an agent | [`../SKILL.md`](../SKILL.md) |
| Maintain this LLM wiki | [`llm-wiki-style-guide.md`](llm-wiki-style-guide.md) |
| Choose the right workflow or feature | [`workflows-and-features.md`](workflows-and-features.md) |
| Work on managed `watch` or queue behavior | [`managed-workers.md`](managed-workers.md) |
| Validate real tmux lifecycle behavior | [`real-use-e2e.md`](real-use-e2e.md) |
| Copy examples only | [`../references/WORKFLOWS.md`](../references/WORKFLOWS.md) |

## Maintenance rules

- `README.md` is for project orientation, requirements, file map, and verification entry points.
- `SKILL.md` is the agent operating guide: target selection, safe command execution, and quick helper commands.
- `llms.txt` is the LLM entry point and document map.
- `docs/workflows-and-features.md` is canonical for the desired operating flow and feature map.
- `docs/llm-wiki-style-guide.md` is canonical for writing and maintaining this LLM wiki.
- `docs/managed-workers.md` is canonical for managed worker contracts.
- `docs/real-use-e2e.md` is canonical for scenario test intent and coverage.
- `references/` is for copyable snippets and examples only; detailed contracts stay in canonical docs.
- Avoid duplicating long command contracts across files. Link to the canonical doc instead.
