# Contributing

Thanks for helping. Issues and pull requests are welcome.

## Setup and tests

```bash
uv sync
uv run pytest
```

CI runs the suite on Linux and macOS with Python 3.11 and 3.13, with tmux installed. Please add
or update tests with behaviour changes. [docs/design.md](docs/design.md) records the design and
the reasons behind it; update it when you change a decision it describes.

For sidebar work, the opt-in reloader in the README's Development section restarts every running
sidebar on your new code.

## The demo images

The README's GIF and screenshots come from made-up projects, never a real root:

```bash
scripts/demo-render.sh
```

Needs uv, tmux, a headless-capable Chrome (`google-chrome` or `$CHROME`), ImageMagick
(`convert`), and a colour emoji font (e.g. Noto Color Emoji) so the sidebar's status icons
render. It writes `docs/images/sidebar.png`, `docs/images/dashboard.png`, and
`docs/images/demo.gif`.

- `scripts/demo.py` builds the throwaway root (projects, PRs, inbox items, agents) and captures the
  two screenshots.
- `scripts/demo_tmux.py` records the GIF in a private tmux server (socket `gopher-demo`). Each
  window runs the real sidebar beside a scripted stand-in agent. The walkthrough is the
  `storyboard()` function, and the stand-in agents' text is `TRANSCRIPTS`.

When a change alters the sidebar, the agent layout, or adds a feature worth showing, consider
regenerating the images, and adjust the storyboard or the made-up projects if the new behaviour
deserves a moment in the GIF. This is opportunistic: a pull request does not need new images to
be merged, and a maintainer can refresh them later.

Keep demo content generic: invented project names, `example.com` links, no real company, people,
or ticket systems.

## Style

- Plain, self-explanatory code; comments only where something is not obvious.
- ASCII in source files. Use `\u` escapes for glyphs the UI needs.
- No imports inside functions unless they are required, with a comment saying why.
