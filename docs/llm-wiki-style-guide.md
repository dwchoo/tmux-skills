# LLM Wiki Style Guide

This guide defines how to write and maintain the tmux-skills LLM wiki.

## Purpose

The wiki should help a future agent or maintainer recover project intent quickly without reading every source file. It should explain the desired workflow, the public contracts, and the tests that prove those contracts.

Write for two readers:

- Humans who need a concise operational reference.
- LLM agents that need canonical facts, stable links, and low-ambiguity task routing.

## Canonical Ownership

Keep each type of information in one primary place:

| Information | Canonical file |
| --- | --- |
| Project overview and verification entry points | `README.md` |
| Agent operating rules and quick commands | `SKILL.md` |
| LLM entry point and document map | `llms.txt` |
| Wiki navigation and maintenance rules | `docs/README.md` |
| Desired operating flow and feature map | `docs/workflows-and-features.md` |
| Managed worker contracts | `docs/managed-workers.md` |
| Real-use E2E scenario intent and coverage | `docs/real-use-e2e.md` |
| Copyable workflow snippets | `references/` |

Do not duplicate long contracts in README or SKILL. Link to the canonical doc instead.

## Page Shape

Each wiki page should start with:

1. A clear H1 title.
2. A short statement of what the page is canonical for.
3. Task-oriented sections with stable headings.

Prefer these section types:

- `Overview`: why the feature or workflow exists.
- `Commands`: copyable commands with only necessary flags.
- `Contract`: public behavior, output shape, state model, or invariants.
- `Workflow`: ordered operational steps and expected outcomes.
- `Scenario Matrix`: setup, action, and assertions for tests.
- `Maintenance`: what to update when behavior changes.

Keep headings stable because LLM agents may navigate by heading names.

## Writing Rules

- Use English for repo docs unless a maintainer explicitly chooses another language.
- Use short paragraphs and tables for scan-heavy content.
- Use exact command names, status strings, exit codes, and JSON fields in backticks.
- State whether a behavior is canonical, illustrative, or optional.
- Prefer relative Markdown links.
- Keep examples minimal and realistic.
- Document negative assertions when they are important, such as “command was not submitted” or “output file is absent.”
- Do not include implementation speculation that is not true in the current code.
- Do not copy long sections between docs. Summarize and link.

## Code Fact Checks

Before writing or changing docs that mention code facts, inspect the source of truth.

Required checks:

- Scenario names and counts: inspect `scripts/e2e_real_use.py`.
- Exit codes: inspect the script or CLI path that defines them.
- Status strings: inspect `scripts/tmux_state.py` and worker code.
- JSON output shape: inspect the command implementation and tests.
- CLI flags: inspect `scripts/tmux_control.py` parser definitions.

When documenting current facts, use wording like “Current code facts from ...” if the facts are likely to change.

## Scenario Documentation Rules

Every real-use E2E scenario should document:

- Scenario name exactly as implemented.
- Scenario group membership: `smoke`, full-only, or individual-only.
- Workflow or feature it protects.
- Setup and preconditions.
- Action taken by the harness.
- Required assertions.
- Negative assertions, when relevant.

Scenario docs should prove real user risk, not just describe code branches. Prefer user-facing failure language such as “duplicate queue starts twice” or “missing pane silently waits forever.”

## `llms.txt` Rules

`llms.txt` is a directory, not a full manual.

Keep it short:

- One H1 title.
- One blockquote summary.
- One short instruction paragraph.
- Link sections with relative Markdown links and one-sentence descriptions.

Required sections:

- `## Core`
- `## Detailed docs`
- `## References`
- `## Optional`

Only put a page under `## Optional` when it is useful but not needed for normal agent execution.

Do not add `llms-full.txt` unless the repo has a generation workflow that prevents duplicated stale content.

## Maintenance Checklist

Use this checklist when changing wiki docs:

- The canonical owner for the information is clear.
- README and SKILL did not grow long duplicate contract sections.
- `llms.txt` links any new canonical doc.
- `docs/README.md` routes readers to the new or changed doc.
- Code facts were checked against source before being documented.
- E2E scenarios include setup, action, and required assertions.
- Any scenario that exists in parser choices but not in `smoke` or `all` is documented as individual-only or intentionally hidden.
- `git diff --check` passes.
- No `__pycache__` or `.pyc` artifacts remain.
