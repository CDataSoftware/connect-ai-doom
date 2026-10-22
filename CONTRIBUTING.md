# Contributing

Thanks for helping. Bug reports, ideas and pull requests are all welcome.

## Get set up

You need Python 3.9 or newer (a stock Mac's `python3` works). The tests need no Connect AI account, no `claude` login and no game build. They run against fakes, so they also run on Linux.

```bash
git clone https://github.com/CDataSoftware/connect-ai-doom.git
cd connect-ai-doom
pip install -r requirements-dev.txt     # Pillow
make test                               # V=1 lists each test
```

Node and shellcheck are optional; the tests that need them skip without them. To play, follow the [README's install steps](README.md#install), or use `./raid.sh offline` after `./setup.sh` to play without an account.

On Windows, run the tests with `python -m unittest discover -s tests`. The `raid.sh` tests skip there, and `tests/test_raid_py.py` covers `raid.py` instead. A change to shared Python code (the bridge, the briefing, Claude plays) should pass on both Windows and a Mac or Linux.

## Where things are

- `raid.sh`: the launcher. Start here. `raid.py` is its Windows counterpart.
- `setup.sh` builds the game on a Mac; `setup.cmd` runs `setup.ps1`, which runs `tools/setup-win.sh` in MSYS2, on Windows.
- `compat.py`: what Windows does differently (replacing open files, stopping a process and its children).
- `brief.py`: the briefing, Claude through the Connect AI MCP server.
- `connect_doom.py`: the bridge psdoom calls, the poller, and the side panel's server.
- `panel/index.html`: the side panel, one file with no build step.
- `tools/`: the level, branding and sprite builders.
- `patches/psdoom-ng-connect-doom.patch`: our changes to the game's C code. `patches/psdoom-ng-windows.patch` goes on top of it on Windows.

[docs/internals.md](docs/internals.md) explains how they fit together, and [docs/testing.md](docs/testing.md) covers the tests.

## Making a change

1. Fork the repo and create a branch.
2. Make your change, and add or update a test when it changes behavior.
3. Run `make test`.
4. For anything visible in the game (HUD, menus, branding), run `python3 tools/screenshots.py /tmp/shots` and look at the screenshots. `make test` can't see the game.
5. Open a pull request that says what changed and why.

### Changing the game's C code

`setup.sh` clones psdoom-ng into `vendor/src/psdoom-ng` and applies the patch. Edit the C files there, rebuild with `make -C vendor/src/psdoom-ng/trunk`, then regenerate the patch:

```bash
cd vendor/src/psdoom-ng
git diff --src-prefix=i/ --dst-prefix=w/ -- 'trunk/src/*.c' 'trunk/src/*.h' > ../../../patches/psdoom-ng-connect-doom.patch
```

A test checks that the patch matches your checkout.

On Windows, `tools/setup-win.sh` applies `psdoom-ng-windows.patch` after the main patch. To change the Windows patch, stage the file with only the main patch applied, make your edit, and diff against the staged copy:

```bash
cd vendor/src/psdoom-ng
git checkout -- trunk/src/pr_process.c
git apply --include=trunk/src/pr_process.c ../../../patches/psdoom-ng-connect-doom.patch
git add trunk/src/pr_process.c
git apply ../../../patches/psdoom-ng-windows.patch    # then edit
git diff -- trunk/src/pr_process.c > ../../../patches/psdoom-ng-windows.patch
git reset -q -- trunk/src/pr_process.c
```

`raid.py` rebuilds the game when either patch or `tools/setup-win.sh` changes.

### Commit messages

Use `feat: <Summary>`, `fix: <Summary>` or `chore: <Summary>`, starting the summary with a capital letter. Use `chore:` for changes users don't see.

## Ideas

- **Linux support.** `setup.sh` and the window layout are macOS-only today. The game and the bridge aren't.
- **The full-screen layout on Windows.** `tools/stage.js` is Cocoa; Windows plays in a window with the panel in a browser tab.
- **More doors.** Show other Connect AI governance (column masking, row filters) as level features.
- **Questions that make good levels.** Try connectors beyond CRM, support and billing, and share what works.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE). Changes to the psdoom patch are GPL-2.0, like psdoom-ng itself.
