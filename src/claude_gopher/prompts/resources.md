# Resources

`resources/<kind>/<name>.md` records each external thing a project uses, so the user and Gopher can
find it without reading the whole history.

- Kinds: `tickets/`, `prs/`, `docs/`, `dashboards/`, `explainers/`; add another kind when none fits.
- Name: a short durable id when the thing has one (ticket key, PR number), otherwise a readable
  slug (`design-review.md`).
- Content: optional frontmatter (below), then the line `# [Title](url)`, then a short blurb on the
  resource's role in this project and its current state if that matters.
- Frontmatter, for things with a lifecycle (pull requests, tickets). Dashboards read it, so keep
  it current whenever the state changes:

  ```yaml
  ---
  state: open          # PRs: open | draft | merged | closed; tickets: open | in-progress | blocked | resolved | done | closed
  merged: 2026-11-12   # PRs, once merged: date, or date and time (2026-11-12T15:30)
  closed: 2026-11-12T09:10   # PRs closed without merging: same format
  stack: 622289 622290 622291   # PRs in a stack: every PR in it, bottom to top, the same line in each file
  ---
  ```
- Gopher's `pr-sync` task, when the schedule has one, updates PR `state:`, `merged:`, and `closed:` from the
  host hourly and files an inbox item when it does; set them yourself when you see the change first.
- If the thing is in the reference library (`{{reference_dir}}/`), point instead of copying: the
  first line as usual, then `See {{reference_dir}}/<slug>.md.` and the project-specific role.
- Add resources as you create or discover them; update the blurb when the role or state changes.
  Do not duplicate: check the kind directory first.
