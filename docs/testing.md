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

`make test` covers `web/levelkit.py` (a visitor's level, built from recorded Connect AI answers), but not the page. `make web-test` builds `web/dist` and plays it in headless Chromium (`tests/web/e2e.mjs`), with Claude's, OpenAI's and OpenRouter's APIs and the Connect AI MCP server faked, so it needs no keys:

```bash
cd tests/web && npm ci && npx playwright-core install chromium   # once
make web-test
```

It covers every button and flow a visitor can reach, each test in its own browser context: the start card (Play, Watch, its fade), taking over from Claude, the controls, the steps' tips, clearing the level through the red door to the end card and Play again, dying and the restart, god mode (on from the start; the AI settings' switch and Esc > Options), the AI settings (each provider's key checked, kept, refused and forgotten; Connect AI checked and refused; the Model row), each provider's model playing and a refused one handing over to offline Claude, building a level from the visitor's data (Pyodide and zdbsp in the page), a build that fails, Demo level, the panel's tabs and question box, every page's links, the phone and small-window notes, and the blog embed.

`node e2e.mjs <text>` runs only the tests whose names contain it; `JOBS=<n>` sets how many run at once (3 by default). The Pages workflow runs it before every deploy; a failure stops the deploy.
