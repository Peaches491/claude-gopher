# Closing a project

Closing ends a project for good. The project directory moves to `{{closed_dir}}/<project>/` and its
checkouts are deleted, after the site's close cleanup runs.

- Propose closing only when the scope is done or abandoned; say which.
- Before proposing, make sure nothing would be lost: uncommitted changes, unpushed commits, open
  pull requests, and anything in tmp that should be in `data/`. Report what you find.
- Bring tickets in `resources/tickets/` to their final state (resolve, comment, or hand off) with
  the user's approval.
- Update `STATUS.md` with the outcome and set `state: closing`.
- `gopher close <project>` does the work: it runs the site's close cleanup, removes the project's
  git worktrees (never with force, and their branches only with `git branch -d`), and moves the
  project to `{{closed_dir}}/`. Plain clones, protected checkouts, shared checkouts, and
  checkouts another project owns are kept, and cleanup does not run in shared ones. Uncommitted changes or commits not on any remote block it.
- Run it without `--yes` first: it prints the plan and changes nothing. Show the user the plan;
  only after they explicitly agree, run `gopher close <project> --yes`. Never delete checkouts or
  move directories yourself.
