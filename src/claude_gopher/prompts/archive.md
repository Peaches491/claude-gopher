# Archiving a project

Archiving parks a project that is paused or finished but may come back. The project directory moves
to `{{archived_dir}}/<project>/`; its checkouts are kept, and the site's archive cleanup (such as
clearing build caches) runs in each one except shared and protected checkouts.

- Propose archiving only when the work is paused or done; say why.
- Before proposing, update `STATUS.md` so a future reader can pick the project back up: where it
  stands, what is unfinished, what to read first.
- `gopher archive <project>` does the work. Run it without `--yes` first: it prints the plan and
  changes nothing. Show the user the plan; only after they explicitly agree, run
  `gopher archive <project> --yes`. Never move directories or clean checkouts yourself.
