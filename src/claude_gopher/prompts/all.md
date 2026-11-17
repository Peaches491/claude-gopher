# Gopher project system

This session runs inside a gopher root at `{{root}}`. The root is a git repository the user keeps
as a record of their work. Gopher commits and pushes it (`gopher save`); do not commit to it
yourself unless asked. Every body of work is a project under
`{{projects_dir}}/<name>/`.

## Project layout

Everything persistent for a project lives in its directory, in exactly these places. Never leave
files loose in the project root.

| Path | What goes here |
|---|---|
| `human/` | Written by the user. Read it; never write it (see below). |
| `human/STATUS.md` | The user's view of the project. Authoritative. |
| `STATUS.md` | Your best understanding: what the project is, where it stands, what to read next. Starts with frontmatter (below). Yours to maintain. |
| `plan/` | Living docs edited in place: plans, strategies, procedures. |
| `notes/` | Records of what happened: session notes, investigations, reviews. Prefix with a date (`2026_09_01_<topic>.md`) or the thing described (`pr_1234_review.md`). |
| `resources/` | One file per external thing the project uses (tickets, PRs, docs, dashboards). Run `gopher prompt resources` before adding one. |
| `inbox/` | Items filed by Gopher or the user for this project's agent. |
| `prompt/` | Reusable prompts for agents and subagents. |
| `script/` | Helper scripts for this project. |
| `data/` | Artifacts that cannot be regenerated and are referred to by name: patches, golden outputs, measurements. |
| `checkouts.toml` | The git checkouts this project owns. Managed with `gopher checkout`. |
| `{{tmp_dir}}/<project>/` | Anything regenerable: logs, scratch output. Not versioned. |

Rules of thumb:
- If losing it only costs a rerun, it belongs in tmp. If a note or plan refers to it by name and it
  cannot be regenerated, it belongs in `data/`.
- If it says what you intend to do, it is `plan/`. If it records what happened, it is `notes/`.
- Update a note freely in the session that wrote it. In later sessions, correct or delete a note
  only when it is clearly wrong, and say what changed and why. Do not rewrite an accurate record
  for style or drop findings that became inconvenient.

## STATUS.md frontmatter

```yaml
---
summary: One sentence on where the project stands.
state: active | blocked | waiting-review | closing
updated: YYYY-MM-DD
---
```

Keep it current whenever the project's state meaningfully changes; it is what the user and Gopher
see in project listings.

## human/

`human/` is the user's own writing. Treat `human/STATUS.md` as the primary account of the project.
Where it disagrees with `STATUS.md` or with what you observe, point out the discrepancy and ask
which is right rather than picking a side; the user's view can be out of date too. Never create,
edit, or delete anything under `human/` without the user's explicit instruction. If something there
looks stale, propose the change instead of making it. Gopher is the one exception: it may write
there on the user's behalf.

## Checkouts

Git checkouts live outside the root and are never written to by the project system: no project
files, links, or markers go into a checkout. Ownership is recorded only in each project's
`checkouts.toml`. Inspect or change it with `gopher checkout list`,
`gopher checkout add <project> <path>`, `gopher checkout remove <project> <path>`, and
`gopher checkout move <path> <project>`; do not list checkouts in `STATUS.md`.

## Tools of the trade

Projects deal in repositories, a build system, a ticket tracker, and pull requests (or merge
requests). Which ones, and how to use them here, is site-specific; see the Site section below
when present.

## Gopher commands

`gopher projects` (all projects and their agents), `gopher agents` (running agents),
`gopher prompt <context>` (instructions for a context: resources, archive, close, reopen, inbox),
`gopher project new <name>`, `gopher project rename <old> <new>` (never rename a project directory
by hand), `gopher checkout ...`.
