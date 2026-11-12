# Resources

`resources/<kind>/<name>.md` records each external thing a project uses, so the user and Gopher can
find it without reading the whole history.

- Kinds: `tickets/`, `prs/`, `docs/`, `dashboards/`, `explainers/`; add another kind when none fits.
- Name: a short durable id when the thing has one (ticket key, PR number), otherwise a readable
  slug (`design-review.md`).
- Content: first line `# [Title](url)`, then a short blurb on the resource's role in this project
  and its current state if that matters (open, merged, done).
- Add resources as you create or discover them; update the blurb when the role or state changes.
  Do not duplicate: check the kind directory first.
