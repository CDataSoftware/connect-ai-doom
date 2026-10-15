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

It also shellchecks both scripts and confirms the psdoom patch matches `vendor/` when that checkout exists. GitHub Actions runs it on every push.

## Screens

`make test` can't see the game. Before shipping a HUD, menu or branding change, look at it:

```bash
python3 tools/screenshots.py /tmp/shots      # play.png, aim.png, menu.png, pause.png
```

It runs psdoom headless on the current `state/` level, walks to a room through the agent channel, presses Esc and P, and saves each screen. Kills and hits go to no-op commands, so the kill log is untouched.
