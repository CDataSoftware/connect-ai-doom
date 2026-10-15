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

## Files

- `connect_ai_mcp.py`: a small MCP client (initialize, tools/call, CSV results).
- `connect_doom.py`: the psdoom bridge (`list`, `kill`, `hit`), the poller, the live `lookup`, `score`/`reset`, and the panel server.
- `tools/make_arena.py`: draws the level on a 16-unit grid of rectangles (rooms, hall, lobby, doors, sign alcoves, vault, window) and derives every wall from where the cells differ; `zdbsp` builds the nodes.
- `tools/make_branding.py`: brand gray walls with an Agility yellow trim, the per-run signs, the vault's masked walls, and (with the CData brand kit) the Connect AI logo on the Esc menu. The kit stays out of git; `setup.sh` extracts it from `~/Documents/dev/cdata/BRANDKIT.zip` (override with `BRANDKIT_ZIP`).
- `tools/make_deh.py`: a DeHackEd patch that rewrites the keycard messages; `# *allow-long-strings*` lets Chocolate Doom take strings longer than the originals.
- `panel/index.html`: the side panel; it polls `/state.json` twice a second.

## The psdoom patch

`patches/psdoom-ng-connect-doom.patch`:

- `-nopslev` always means arena mode (upstream checks the game mode before Freedoom is identified, so monsters landed in the void).
- Name tags show only the short name.
- The kill command's first output line becomes the on-screen message.
- The record under the crosshair: with `PSDOOM_LABELS` set, the game shows that monster's two lines from the file (`pid<TAB>line1<TAB>line2`, written by `connect_doom.py` with each cache refresh) on a dark band above the status bar.
- The window is resizable on macOS too (upstream turned that off there).
- The agent channel for Claude plays (below).

## Claude plays: the agent channel

With `PSDOOM_AGENT_DIR` set, psdoom reads one command per line from `<dir>/cmd` (`MOVE x y`, `USE`, `TURN deg`, `ATTACK pid`, `SHOT`, `STOP`) and writes the player, every data monster and the last finished command to `<dir>/state.json`. `play.py`'s tools turn those into routes through the hall and doors. Claude plays in god mode with a full chaingun, because it thinks in seconds, not tics. `PLAY_MODEL` (default sonnet) and `PLAY_MAX_TURNS` (default 60) tune it; a 20-turn run costs about $0.13.

`SHOT` saves the frame as `DOOMnn.pcx`, so the mode also runs headless with `SDL_VIDEODRIVER=dummy`, which is how it's tested.

## Account notes

If the account's Data Security rules are switched off, the SSN lookup goes through, no vault appears, and the panel shows the unmasked answer.

## Doom limits and quirks

- Wall textures tile every 128 px vertically (hence the 128-tall sign alcoves), and multi-patch textures cap at 64 KB (hence one baked patch per sign).
- psdoom ignores SIGTERM; close the window or `pkill -9 -f trunk/src/psdoom`. On macOS a crash shows a system alert that outlives the game; click OK.
