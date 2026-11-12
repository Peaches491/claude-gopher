# Closing a project

Closing ends a project for good. The project directory moves to `{{closed_dir}}/<project>/` and its
checkouts are deleted, after the site's close cleanup runs.

- Propose closing only when the scope is done or abandoned; say which.
- Before proposing, make sure nothing would be lost: uncommitted changes, unpushed commits, open
  pull requests, and anything in tmp that should be in `data/`. Report what you find.
- Bring tickets in `resources/tickets/` to their final state (resolve, comment, or hand off) with
  the user's approval.
- Update `STATUS.md` with the outcome and set `state: closing`.
- The user runs `gopher close <project>`, which shows the full plan and asks for confirmation. Do
  not delete checkouts or move directories yourself. Checkouts owned by another project, and any the
  site marks protected, are never deleted.
