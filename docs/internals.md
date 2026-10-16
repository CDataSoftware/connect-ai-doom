# Internals

## Pipeline

```
raid.sh
  brief.py ── claude -p, Connect AI MCP only (read-only tools) ── state/run.jsonl  (panel feed)
                                                              └─ state/mission.json (rooms, permissions, refusals)
  tools/make_arena.py + make_branding.py + make_deh.py ── state/level.wad, brand.wad, level.deh
  connect_doom.py poll ── re-runs each room's query through the MCP server every 15 s ── state/cache.json
  psdoom-ng ── list / kill / hit ──> connect_doom.py (local files; kill starts a background lookup)
  connect_doom.py panel ── http://localhost:8666
```

- `brief.py` keeps the token out of the MCP config: the config says `Basic ${CONNECT_AI_BASIC}` and Claude Code expands it from the environment. The MCP URL carries `?ops=` so Claude only gets Connect AI's read-only tools.
- psdoom spawns monsters into three fixed boxes and picks the box from the monster's id mod 3, so room *i*'s records get ids with `id % 3 == i`.
- A killed record is hidden from `list`, otherwise psdoom resurrects it. `python3 connect_doom.py score` prints the kill log; `reset` brings everyone back.
- `POLL_SECONDS` (default 15) and `MAX_PER_ROOM` (default 20) tune the refresh.

## Configuration

| Setting | What it does |
|---|---|
| `./raid.sh login` | Saves your Connect AI email (in `state/account`) and token (in the macOS Keychain, service `connect-doom`). |
| `CONNECT_AI_USER`, `CONNECT_AI_PAT` | Override the saved account and token. |
| `CONNECT_AI_OP_ITEM` | An `op://` path: the first run reads the token from 1Password and copies it into the Keychain. |
| `CONNECT_AI_MCP_URL` | The MCP server (default `https://mcp.cloud.cdata.com/mcp`). |
| `LAYOUT=off` | Keep psdoom's default window and open the panel in a browser tab, not full screen. |
| `GEOMETRY=WxH` | The game window's size; psdoom snaps it to its own screen modes. |
| `NO_PANEL=1` | No side panel. |
| `NOGORE=1` | No blood or corpses: hits throw bullet puffs, and a killed record vanishes in green teleport fog (`make_deh.py --no-gore`). For booths and talks. |
| `PANEL_PORT` | The panel's port (default 8666). |
| `CONNECT_AI_TIMEOUT` | Seconds the bridge gives each Connect AI call during a game, start to finish (20). Each live refresh is saved in `state/saved-records.json`, and a game that starts without Connect AI plays those. |
| `BOOTH_PAUSE`, `BOOTH_GAMES` | `booth` mode: seconds between games (10), and a number of games to stop after (none). |
| `POLL_SECONDS`, `MAX_PER_ROOM` | The refresh interval (15) and the most monsters per room (20). |
| `BRIEF_MODEL`, `PLAY_MODEL`, `PLAY_MAX_TURNS` | The Claude model for the briefing and for Claude plays (sonnet), and Claude plays' turn limit (60). |
| `REPLAY_GRACE`, `REPLAY_PAUSE` | Auto replay: how long Claude gets after the last kill for the vault and a summary (45 s), and the pause after a run ends before the level restarts (6 s). |
| `IDLE_SECONDS` | With auto replay on and Claude plays off: how long without a key or the mouse before Claude takes over (60). |
| `PLAYER=replay` | Claude plays always uses the offline player (`--offline-plays` sets it). |
| `OFFLINE_AFTER` | How long a Claude run can be without a network before the offline player takes over from where the marine stands (20 s). |
| `QUESTION` | The default question for `./raid.sh new`. |

The old mode names still work: `--again` (replay), `--demo` (offline) and `brief` (brief-only).

**For the full show, an account needs** a few connections, at least one read-only and one writable (blue and yellow doors), and a Data Security rule for US Social Security numbers (the red door). The demo GIF was recorded on a CData test account whose data is synthetic.

## Files

- `raid.sh`: the launcher (modes, login, window layout, controls); `setup.sh` builds everything once, and rebuilds the game when the patch changes.
- `brief.py`: the briefing: runs `claude -p` with the Connect AI MCP server and turns the result into `state/mission.json`.
- `connect_ai_mcp.py`: a small MCP client (initialize, tools/call, CSV results).
- `claude_run.py`: runs Claude Code headless (`claude -p`, stream-json) for `brief.py` and `play.py`: the read-only Connect AI MCP server config, and the event log the panel reads.
- `connect_doom.py`: the psdoom bridge (`list`, `kill`, `hit`), the poller, the live `lookup` and `vault` checks, `score`/`reset`, and the panel server.
- `play.py`: Claude plays: a Doom MCP server (`look`, `go_to`, `attack`) over the agent channel, the `claude -p` run that uses it, `play.py replay`, the offline player, and `play.py loop`, which follows the Options menu's switches.
- `tools/make_arena.py`: draws the level on a 16-unit grid of rectangles (rooms, hall, lobby, doors, sign alcoves, vault, window) and derives every wall from where the cells differ; `zdbsp` builds the nodes.
- `tools/make_branding.py`: brand gray walls with an Agility yellow trim, the per-run signs, the vault's masked walls, the Connect AI logo on the Esc menu, the pause screen and the HUD corner, the CData logotype on the arena signs and the pause screen, and the Options menu's *Claude plays* and *Auto replay* labels. The logos it needs from the CData brand kit are in `assets/brand/`.
- `tools/make_deh.py`: a DeHackEd patch that rewrites the keycard messages; `# *allow-long-strings*` lets Chocolate Doom take strings longer than the originals. With `--no-gore` it also redraws the blood frames as bullet puffs and the two monsters' death frames as teleport fog.
- `tools/make_sprites.py`: the panel's sprite PNGs from the WAD, and its logos from `assets/brand/`.
- `tools/screenshots.py`: headless screenshots of the game's screens (see testing.md).
- `panel/index.html`: the side panel; it polls `/state.json` twice a second. Its fonts are in `panel/fonts` (DM Sans and DM Mono from the CData brand, Press Start 2P and VT323 for Doom; SIL Open Font License), so it needs no network. With `?stage=gx,gy,gw,gh,split,top` it's the full-screen page: the stage on the left (brand bars above and below the game's box) and the panel on the right.
- `tools/stage.js`: the full-screen layout's backdrop, a JavaScript for Automation script: one borderless window over the main screen showing the stage page in a web view, with the menu bar and the Dock hidden. It hides when another app comes to the front and returns with the game; Cmd-Q on it ends the raid. `raid.sh` sizes the game to the biggest psdoom mode that fits two thirds of the screen and puts its borderless window on the page's game box.

## The psdoom patch

`patches/psdoom-ng-connect-doom.patch`:

- `-nopslev` always means arena mode (upstream checks the game mode before Freedoom is identified, so monsters landed in the void).
- Name tags show only the short name.
- The kill command's first output line becomes the on-screen message.
- The record under the crosshair: with `PSDOOM_LABELS` set, the game shows that monster's two lines from the file (`pid<TAB>line1<TAB>line2`, written by `connect_doom.py` with each cache refresh) on a dark band above the status bar.
- The window is resizable on macOS too (upstream turned that off there).
- With `PSDOOM_FRAMELESS` set, the window has no title bar and hides the menu bar and the Dock while it's in front (the full-screen layout). It floats above other windows while it or the stage (`PSDOOM_STAGE_PID`) is in front, and drops back to a normal window when another app is.
- While *Claude plays* is on, the game doesn't grab the mouse, so people can click through Claude's feed.
- The red door: using a red keycard door without the key also runs `PSDOOMVAULTCMD` in the background (at most every 2 seconds), which `raid.sh` points at `connect_doom.py vault`.
- A small Connect AI logo (`CAICORNR` from `state/brand.wad`) in the view's top-right corner.
- P pauses as well as Pause (Mac keyboards have no Pause key); the pause screen is `M_PAUSE` from `state/brand.wad`.
- The agent channel for Claude plays (below), and how long since a person last pressed a key or moved the mouse.
- Two Options menu rows, *Claude plays* and *Auto replay*, in place of *End Game* (a booth visitor would land on the title screen; F7 still ends the game) and *Graphic Detail* (F5 still switches it).

## Claude plays: the agent channel

With `PSDOOM_AGENT_DIR` set, psdoom reads one command per line from `<dir>/cmd` (`MOVE x y`, `USE`, `TURN deg`, `ATTACK pid`, `KEY code`, `SHOT`, `RESTART`, `OPTION n 0|1`, `STOP`; `KEY` presses a key, which `tools/screenshots.py` uses to open the menus and pause; `RESTART` starts the level over; `OPTION` sets a switch) and writes the player, every data monster, the last finished command, the Options menu's two switches and `idle_seconds` (since a person's last key or mouse event; `KEY` presses don't count) to `<dir>/state.json`. `play.py`'s tools turn those into routes through the hall and doors. Claude plays in god mode with a full chaingun, because it thinks in seconds, not tics.

`raid.sh` always opens the channel and starts `play.py loop`; `--claude-plays` and `--loop` set the switches' starting values (`PSDOOM_AGENT_CLAUDE`, `PSDOOM_AGENT_LOOP`). While *Claude plays* is off the game refuses the commands that steer, and the god mode goes. The loop watches `state.json`: *Claude plays* on starts a run, off stops it and hands the marine back where it stands. A run is `play.py run` (Claude) unless `PLAYER=replay`, the level is the offline one, `api.anthropic.com` can't be reached, or a Claude run failed in the last 5 minutes (it exited with an error, or within 30 s, say because `claude` isn't signed in; or the network was gone for `OFFLINE_AFTER` mid-run, checked every 10 s, since `claude` retries a dead network for minutes): then it's `play.py replay`, which carries on from where the marine stands, auto replay or not, and `game.json`'s mode is `replay`, which the panel labels. With *Auto replay* on, once the level is clear (after `REPLAY_GRACE`) or the run ends (after `REPLAY_PAUSE`; 60 s if an offline run died within 30 s), it moves `state/game.json`'s `started` to now, so the bridge lists every record again, sends `RESTART`, and starts a new run; and with *Claude plays* off, `IDLE_SECONDS` without input sends `OPTION 0 1`.

`play.py replay` (`Autopilot`) calls the Doom tools directly in a fixed plan and logs the same events `claude_run.run` logs, with the text streamed a few words at a time. Each finished Claude run's `play.jsonl` is kept in `state/recordings/<level>/`, the level being a hash of the question and its rooms; for each record, the sentences naming it in the line Claude wrote just before attacking it become the offline player's line for that record. It never calls Connect AI. `PLAY_MODEL` (default sonnet) and `PLAY_MAX_TURNS` (default 60) tune it; a 20-turn run costs about $0.13.

`SHOT` saves the frame as `DOOMnn.pcx`, so the mode also runs headless with `SDL_VIDEODRIVER=dummy`, which is how it's tested.

## Account notes

If the account's Data Security rules are switched off, the SSN lookup goes through, no vault appears, and the panel shows the unmasked answer.

## Doom limits and quirks

- Wall textures tile every 128 px vertically (hence the 128-tall sign alcoves), and multi-patch textures cap at 64 KB (hence one baked patch per sign).
- psdoom ignores SIGTERM; close the window or `pkill -9 -f trunk/src/psdoom`. On macOS a crash shows a system alert that outlives the game; click OK.
