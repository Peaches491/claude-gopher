# Reopening a project

Reopening moves an archived or closed project back to `{{projects_dir}}/<project>/`
(`gopher reopen <project>`: plan first, then `--yes` once the user agrees). Checkouts are
not recreated: a closed project's checkouts are gone, and `checkouts.toml` may list paths that no
longer exist. After reopening, read `STATUS.md`, check which checkouts still exist, and fix
`checkouts.toml` with `gopher checkout`.
