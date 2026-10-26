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
| `CONNECT_AI_TIMEOUT` | Seconds the bridge gives each Connect AI call during a game, start to finish (30); a refresh reads the rooms at once. Each live refresh is saved in `state/saved-records.json`, and a game that starts without Connect AI plays those. |
| `BOOTH_PAUSE`, `BOOTH_GAMES` | `booth` mode: seconds between games (10), and a number of games to stop after (none). |
| `POLL_SECONDS`, `MAX_PER_ROOM` | The refresh interval (15) and the most monsters per room (20). |
| `BRIEF_MODEL`, `PLAY_MODEL`, `PLAY_MAX_TURNS` | The Claude model for the briefing and for Claude plays (sonnet), and Claude plays' turn limit (60). |
| `REPLAY_GRACE`, `REPLAY_PAUSE` | Auto replay: how long Claude gets after the last kill for the vault and a summary (45 s), and the pause after a run ends before the level restarts (6 s). |
| `IDLE_SECONDS` | With auto replay on and Claude plays off: how long without a key or the mouse before Claude takes over (60). |
| `PLAYER=replay` | Claude plays always uses the offline player, like the *Offline Claude* switch. |
| `OFFLINE_AFTER` | How long a Claude run can be without a network before the offline player takes over from where the marine stands (20 s). |
| `QUESTION` | The default question for `./raid.sh new`. |

The old mode names still work: `--again` (replay), `--demo` (offline) and `brief` (brief-only).

**For the full show, an account needs** a few connections, at least one read-only and one writable (blue and yellow doors), and a Data Security rule for US Social Security numbers (the red door). The demo GIF was recorded on a CData test account whose data is synthetic.

## Files

- `raid.sh`: the launcher (modes, login, window layout, controls); `setup.sh` builds everything once, and rebuilds the game when the patch changes; `raid.sh` runs `setup.sh --game` itself when a pull brought a new patch (the game only, no downloads, about 15 s).
- `brief.py`: the briefing: runs `claude -p` with the Connect AI MCP server and turns the result into `state/mission.json`.
- `connect_ai_mcp.py`: a small MCP client (initialize, tools/list, tools/call, CSV results).
- `claude_run.py`: runs Claude Code headless (`claude -p`, stream-json) for `brief.py` and `play.py`: the read-only Connect AI MCP server config, and the event log the panel reads.
- `leaderboard.py`: scores each model run from its feed and the game, keeps `state/leaderboard.jsonl`, and ranks the models on a level (`./raid.sh leaderboard`, and the panel's board).
- `gateway_run.py`: runs any model through Connect AI's LLM Gateway when `raid.sh --model` picks one to play: an agent loop over OpenAI-style chat completions that writes the same event log.
- `connect_doom.py`: the psdoom bridge (`list`, `kill`, `hit`), the poller, the live `lookup` and `vault` checks, `score`/`reset`, and the panel server.
- `play.py`: Claude plays: a Doom MCP server (`look`, `go_to`, `attack`) over the agent channel, the `claude -p` run that uses it, `play.py replay`, the offline player, and `play.py loop`, which follows the Options menu's switches.
- `tools/make_arena.py`: draws the level on a 16-unit grid of rectangles (rooms, hall, lobby, doors, sign alcoves, vault, window) and derives every wall from where the cells differ; `zdbsp` builds the nodes. Inside each 1024x1024 spawn box the floor only steps by 24 units or less (a dais, a pit, a moat, crate aisles), so monsters spawn anywhere and Claude's straight-line moves never snag; pillars, alcoves and lamps stand in a 192-unit ring around the box, which climbs in three 24-unit tiers to the walls, and in the hall, flush with the walls. Inside each door is a landing a step up whose edges block monsters (`BLOCK_MONSTERS`), and psdoom's spawn rows start above it, so the records spread round its edge instead of jamming the doorway. Each room's alcoves keep a cache (health west, ammo east, armor and a backpack north), its records leave loot in rows down the ring (shells for each record, a rocket and a stimpack for each urgent one, counted from `state/cache.json`, or an offline mission's rows), and a glass window looks onto it from the hall. The hall's light strips glow slowly in a ripple, and its south wall and each room's far alcoves are animated dashboards (`DBRAIN1-4` from `state/brand.wad`).
- `tools/themes.py`: each room's look, scenery and monsters. Six layouts (CRM office, support ops center, billing treasury, data lake, ERP warehouse, tech base); every app on the account picks one and brands it with its own walls, alcove art, floor and lamps, and its own pair of monsters (a record, and a bigger one for an urgent record). Unknown apps get the layout their name suggests, or the tech base. `raid.sh` passes the pairs to psdoom as `PSDOOM_MONSTERS`.
- `tools/make_branding.py`: brand gray walls with an Agility yellow trim, the per-run signs, the vault's masked walls, the Connect AI logo on the Esc menu, the pause screen and the HUD corner, the CData logotype on the arena signs and the pause screen, and the labels of the Options menu's seven switches. The logos it needs from the CData brand kit are in `assets/brand/`.
- `tools/make_deh.py`: a DeHackEd patch that rewrites the keycard messages; `# *allow-long-strings*` lets Chocolate Doom take strings longer than the originals. It also gives every record monster about half Doom's hit points, and the big ones 100. With `--no-gore` it also redraws the blood frames as bullet puffs and every record monster's death frames as teleport fog.
- `tools/make_sprites.py`: the panel's sprite PNGs from the WAD, and its logos from `assets/brand/`.
- `tools/screenshots.py`: headless screenshots of the game's screens (see testing.md).
- `panel/index.html`: the side panel; it polls `/state.json` twice a second. Its fonts are in `panel/fonts` (DM Sans and DM Mono from the CData brand, Press Start 2P and VT323 for Doom; SIL Open Font License), so it needs no network. Its question box posts to `/ask`: the bridge writes `state/ask.json` and kills the raid's psdoom (`RAID_PID`, which `raid.sh` gives the panel), and `raid.sh`, which runs the game in a loop, moves it to `asking.json`, runs `brief.py` with it, and builds and starts the new level; a briefing that fails or finds no rooms puts the last `mission.json` back and writes `ask-failed.json`, which the panel shows for two minutes. With `?stage=gx,gy,gw,gh,split,top` it's the full-screen page: the stage on the left (brand bars above and below the game's box) and the panel on the right.
- `tools/stage.js`: the full-screen layout's backdrop, a JavaScript for Automation script: one borderless window over the main screen showing the stage page in a web view, with the menu bar and the Dock hidden. It hides when another app comes to the front and returns with the game; Cmd-Q on it ends the raid. `raid.sh` sizes the game to the biggest psdoom mode that fits two thirds of the screen and puts its borderless window on the page's game box.

## The psdoom patch

`patches/psdoom-ng-connect-doom.patch`:

- `-nopslev` always means arena mode (upstream checks the game mode before Freedoom is identified, so monsters landed in the void).
- Name tags show only the short name, up to 12 letters (upstream kept 7), on the monster, nearest first; a tag that would cover a nearer one is skipped.
- Every game starts with a shotgun and a box of shells, after auto replay's restart too. God mode also keeps every ammo type full.
- A game whose launcher is gone (its parent is now launchd: `raid.sh` was killed or died) quits within a second, so it never sits frozen on a booth's screen; `raid.sh`'s exit trap kills the game as well.
- The data monsters' spawn rows start at y = 192, clear of the landing inside each door.
- A record (a data monster) ignores noise and wakes only when a player comes within `PSDOOM_WAKE` units (default 384) and in sight, or shoots it, so a room is fought a few at a time.
- Cmd-Q (SDL_QUIT) quits at once, with no "are you sure?" prompt; `raid.sh` also sets `show_endoom 0`, so no ENDOOM screen waits for a key. The menu's Quit Game still asks.
- A grabbed mouse hides the cursor with `SDL_ShowCursor`, not just a blank cursor, so sdl12-compat switches to relative mouse mode. Before, the game warped the pointer to the middle every tic and macOS dropped the movement after each warp: a trackpad barely turned the view.
- The status bar is redrawn while the menu is open and as it closes: the Options menu reaches down over it and left letters behind.
- Walking into a shut door opens it, as the use key would (`P_BumpDoors`): a Mac trackpad has no right button, and Doom veterans press Space, which fires here. A door already moving or open is left alone, and a locked door's message comes at most once a second.
- Each room has its own monsters: `PSDOOM_MONSTERS` is six doomednums, a record's then an urgent record's for rooms 1-3 (`tools/themes.py`); without it, shotgun guys and pink demons as upstream.
- The renderer's vanilla limits are raised (visplanes 128 to 1024, drawsegs 256 to 2048, and the sprite, opening and clip-range tables), since the themed rooms and hall draw far more floor and ceiling planes than vanilla allows: looking down the hall used to end the game with `R_FindPlane: no more visplanes`.
- The kill command's first output line becomes the on-screen message.
- Glass: a line flagged 1024 (`ML_GLASS`) is seen through but stops sight, shots, projectiles and moves, so monsters behind a window neither wake nor fight through it (and `P_CheckSight` keeps Claude from aiming through it).
- Sector special 21 is a slow glow: one light level a tic, down to the dimmest neighbor and back, starting the sector's tag tics into the pulse, so a row of them ripples.
- The record under the crosshair: with `PSDOOM_LABELS` set, the game shows that monster's two lines from the file (`pid<TAB>line1<TAB>line2`, written by `connect_doom.py` with each cache refresh) on a dark band above the status bar.
- The window is resizable on macOS too (upstream turned that off there).
- With `PSDOOM_FRAMELESS` set, the window has no title bar and hides the menu bar and the Dock while it's in front (the full-screen layout). It floats above other windows while it or the stage (`PSDOOM_STAGE_PID`) is in front, and drops back to a normal window when another app is.
- While *Claude plays* is on, the game doesn't grab the mouse, so people can click through Claude's feed.
- The red door: using a red keycard door without the key also runs `PSDOOMVAULTCMD` in the background (at most every 2 seconds), which `raid.sh` points at `connect_doom.py vault`.
- A small Connect AI logo (`CAICORNR` from `state/brand.wad`) in the view's top-right corner.
- P pauses as well as Pause (Mac keyboards have no Pause key); the pause screen is `M_PAUSE` from `state/brand.wad`.
- The agent channel for Claude plays (below), and how long since a person last pressed a key or moved the mouse.
- Seven Options menu switches, *Claude plays*, *Auto replay*, *God mode* (for people, who can't die and never run out of ammo; Claude always has it), *Saved data*, *Offline Claude*, *Music* and *Sound effects*, in place of *End Game* (a booth visitor would land on the title screen; F7 still ends the game), *Graphic Detail* (F5 still switches it), *Messages* (F8) and *Screen Size* (- and +). Their starting values come from `PSDOOM_AGENT_CLAUDE`, `PSDOOM_AGENT_LOOP`, `PSDOOM_OPT_GOD`, `PSDOOM_OPT_SAVED`, `PSDOOM_OPT_OFFLINE`, `PSDOOM_OPT_MUSIC` and `PSDOOM_OPT_SFX` (the last two on unless set to 0); `OPTION n` numbers them in that order. *Music* and *Sound effects* off set that volume to 0, so the Sound Volume sliders (F4) keep their levels.
- The music is Freedoom's MIDI soundtrack, played by macOS's own General MIDI synth: `setup.sh` builds SDL_mixer with native MIDI and without Timidity, which needs sound fonts a Mac doesn't have. A change to SDL_mixer's build flags rebuilds it on the next `./setup.sh`.

## Claude plays: the agent channel

With `PSDOOM_AGENT_DIR` set, psdoom reads one command per line from `<dir>/cmd` (`MOVE x y`, `USE`, `TURN deg`, `ATTACK pid`, `KEY code`, `SHOT`, `RESTART`, `OPTION n 0|1`, `MODEL n`, `STOP`; `KEY` presses a key, which `tools/screenshots.py` uses to open the menus and pause; `RESTART` starts the level over; `OPTION` sets a switch; `MODEL` picks the Model row's n-th model) and writes the player, every data monster, the last finished command, the Options menu's switches and its Model row's model (`options.model`), `idle_seconds` (since a person's last key or mouse event; `KEY` presses don't count) to `<dir>/state.json`. `play.py`'s tools turn those into routes through the hall and doors. Claude plays in god mode with a full chaingun, because it thinks in seconds, not tics.

`raid.sh` always opens the channel and starts `play.py loop`; `--claude-plays` and `--loop` set the switches' starting values (`PSDOOM_AGENT_CLAUDE`, `PSDOOM_AGENT_LOOP`). While *Claude plays* is off the game refuses the commands that steer, and the god mode goes. The loop watches `state.json`: *Claude plays* on starts a run, off stops it and hands the marine back where it stands. Every change of the switches goes to `state/options.json`, which the next game of a booth session starts from (outside a booth, `raid.sh` clears it), and the bridge follows *Saved data*: from `state/agent/state.json` while the game runs, `options.json` before it's up. A run is `play.py run` (Claude) unless *Offline Claude* is on (flipping it mid-run swaps the player), `PLAYER=replay`, the level is the offline one, `api.anthropic.com` can't be reached, or a Claude run failed in the last 5 minutes (it exited with an error, or within 30 s, say because `claude` isn't signed in; or the network was gone for `OFFLINE_AFTER` mid-run, checked every 10 s, since `claude` retries a dead network for minutes): then it's `play.py replay`, which carries on from where the marine stands, auto replay or not, and `game.json`'s mode is `replay`, which the panel labels. With *Auto replay* on (`raid.sh`'s default), once the level is clear (after `REPLAY_GRACE`), the run ends (after `REPLAY_PAUSE`; 60 s if an offline run died within 30 s), or the player is dead (after `DEATH_PAUSE`, Claude plays on or off), it moves `state/game.json`'s `started` to now, so the bridge lists every record again, sends `RESTART` (allowed with *Claude plays* off too), and starts a new run if Claude plays; and with *Claude plays* off, `IDLE_SECONDS` without input sends `OPTION 0 1`.

`play.py replay` (`Autopilot`) calls the Doom tools directly in a fixed plan and logs the same events `claude_run.run` logs, with the text streamed a few words at a time. Each finished Claude run's `play.jsonl` is kept in `state/recordings/<level>/`, the level being a hash of the question and its rooms; for each record, the sentences naming it in the line Claude wrote just before attacking it become the offline player's line for that record. It never calls Connect AI. `PLAY_MODEL` (default sonnet) and `PLAY_MAX_TURNS` (default 60) tune it; a 20-turn run costs about $0.13.

With `PLAY_GATEWAY_MODEL` set (`raid.sh --model`), `play.py run` hands the run to `gateway_run.run` instead of `claude -p`: a small agent loop over the LLM Gateway's OpenAI-style `POST /chat/completions` (`https://cloud.cdata.com/gateway/v1`, `CONNECT_AI_GATEWAY_URL`), authenticated with a gateway virtual key (`CONNECT_AI_GATEWAY_KEY`, which `raid.sh` reads from the Keychain item `connect-doom`/`llm-gateway`; the Connect AI token isn't accepted there). It sends the same prompt with the Doom tools, run in-process, and Connect AI's read-only tools, called through `connect_ai_mcp.Client` (named `doom__look`, `connectai__queryData` and so on), and writes the same events to `state/play.jsonl`, plus the run's token counts on `done` (the gateway reports no cost). Each reply goes back as it came: Gemini keeps its reasoning in its tool call ids. Requests aren't streamed, because the gateway returns Gemini's streamed replies in Anthropic's format empty (which also means Claude Code can't drive Gemini through the gateway), and each reply may use up to 4096 tokens, since thinking models spend some before they answer. Each turn resends the conversation, so before each request the tool results older than the last six are cut to their first 300 characters (and any result to 8,000): without it a 34-turn Gemini run sent 493,000 input tokens. Rate limits and server errors are retried three times; any other refusal fails the run (exit 2). These runs aren't kept as recordings, so the offline player always narrates in Claude's words. `game.json`'s `model` names the player for the panel, and the network check looks for `cloud.cdata.com` instead of `api.anthropic.com`.

The leaderboard (`leaderboard.py`) is kept by the supervisor, not the run, since the supervisor ends a run itself (the level clear for `REPLAY_GRACE`, or past `PLAY_TIME_LIMIT`, default 600 s) and knows when a person stopped one. At each model run's launch it notes the records alive and which are urgent (`state/cache.json`'s `demon`) and whether *Saved data* is on; when the run ends it scores `state/play.jsonl` against the game's records left with `leaderboard.score` and appends the row to `state/leaderboard.jsonl` (with `ended` `done`, `failed` or `time limit`), keeping the run's feed in `state/runs/<level>/` (the row's `feed`) for a look at how it played, since the next run starts `play.jsonl` over. A run whose last reply the gateway or provider filtered (`done`'s `stop` is `content_filter`, which `gateway_run` records with each empty reply it asks again) is `blocked`: counted in the board's `blocked`, left out of its medians. Records count once each, and only those alive at the run's start. A gateway run's cost is its tokens at `leaderboard.PRICES`, the console's per-model rates (the gateway reports no dollars); a model missing there shows none. A model's live and saved-data runs are separate rows on the board, since live records change between runs. A run with no Doom tool call (Claude signed out, say), the offline player's runs and runs stopped by the switches aren't scored. A Claude Code run is named for its session's model plus "(Claude Code)", a gateway run for its model. `leaderboard.board(level)` takes each model's medians on one level (`level_key`, the hash the recordings use too), and the bridge serves it to the panel as `leaderboard`. A bench (`PLAY_MODELS`, `PLAY_RUNS`; `raid.sh --models --runs`) hands each launch the next model in rounds, starting `play.py run` with `PLAY_GATEWAY_MODEL` set or, for `claude-code`, removed; a failed run moves on to the next model rather than the offline player; `game.json`'s `bench` is `[run, of]` for the panel; each turn sends `MODEL` so the menu's Model row shows whose turn it is; after the last run, `OPTION 0 0` and `OPTION 1 0` turn *Claude plays* and *Auto replay* off.

The Options menu's Model row (`m_menu.c`, between *Claude plays* and *Auto replay*; its label `M_MODEL` comes from `make_branding.py`, and psdoom writes the model after it in Doom's small font) steps through `PSDOOM_AGENT_MODELS`, starting on `PSDOOM_AGENT_MODEL`. `raid.sh` lists Claude Code (`claude-code`), then the models the gateway key may use (`gateway_run.py models`: the list `GET /models` gave last time, kept in `state/gateway-models.json`, while a background `gateway_run.py refresh` fetches it again for the next game; only the first launch waits, 4 s at most), then any `--model` or `--models` named; it starts on the last booth game's pick, else `--model`'s, else Claude Code. The supervisor launches each run with the row's model (a bench's turn comes first), and a person's new pick stops the current run, unscored, so the pick takes over where the marine stands; it also ends a bench.

`SHOT` saves the frame as `DOOMnn.pcx`, so the mode also runs headless with `SDL_VIDEODRIVER=dummy`, which is how it's tested.

## Account notes

If the account's Data Security rules are switched off, the SSN lookup goes through, no vault appears, and the panel shows the unmasked answer.

## Doom limits and quirks

- Wall textures tile every 128 px vertically (hence the 128-tall sign alcoves), and multi-patch textures cap at 64 KB (hence one baked patch per sign).
- psdoom ignores SIGTERM and SIGINT; Cmd-Q or closing the window quits, or `pkill -9 -f trunk/src/psdoom`. On macOS a crash shows a system alert that outlives the game; click OK.
