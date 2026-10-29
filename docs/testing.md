# Testing

```bash
make test          # V=1 lists each test
```

Needs Pillow (`requirements-dev.txt`), plus node and shellcheck for those checks. No network, Connect AI account or psdoom build is needed.

The suite runs offline against fakes: a fake Connect AI MCP server, a fake `claude` CLI that streams a canned run, a tiny stand-in for `freedoom2.wad`, and fake `psdoom`, `zdbsp`, `security`, `op` and `open`.

It covers:

- the MCP client and `brief.py`
- every bridge command, the live lookup and the panel server
- the level, sign and DeHackEd builders
- the panel's script, run in Node with a minimal DOM (`tests/panel_harness.js`)
- `raid.sh` end to end in its `new`, `replay`, `offline` and `brief-only` modes (and their old names)

It also shellchecks both scripts and confirms the psdoom patch matches `vendor/` when that checkout exists. GitHub Actions runs it on every push and pull request, on Python 3.9 (a stock Mac's) and 3.13.

## Screens

`make test` can't see the game. Before shipping a HUD, menu or branding change, look at it:

```bash
python3 tools/screenshots.py /tmp/shots      # play.png, aim.png, menu.png, pause.png
```

It runs psdoom headless on the current `state/` level, walks to a room through the agent channel, presses Esc and P, and saves each screen. Kills and hits go to no-op commands, so the kill log is untouched.

## The browser version

`make test` covers `web/levelkit.py` (a visitor's level, built from recorded Connect AI answers), but not the page. `make web-test` builds `web/dist` and plays it in headless Chromium (`tests/web/smoke.mjs`), with Claude's API and the Connect AI MCP server faked, so it needs no keys:

```bash
cd tests/web && npm ci && npx playwright-core install chromium   # once
make web-test
```

It checks that offline Claude plays on load, that a refused key hands the game back to offline Claude, that a level builds from the visitor's data (Pyodide and zdbsp in the page) and plays, and that Demo level goes back. The Pages workflow runs it before every deploy; a failure stops the deploy.
