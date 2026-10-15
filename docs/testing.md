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
- `raid.sh` end to end in demo, live, `--again` and `brief` modes

It also shellchecks both scripts and confirms the psdoom patch matches `vendor/` when that checkout exists. GitHub Actions runs it on every push.
