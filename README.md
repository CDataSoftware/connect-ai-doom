# Connect AI × DOOM

A Connect AI MCP server demo in Doom. You ask Claude a business question. Claude answers it by working through the Connect AI MCP server, and the Doom level is built from what Connect AI let it reach:

- **One room per app** Claude pulled data from (Salesforce, Zendesk, QuickBooks, whatever answers the question). The records it found are the monsters; urgent ones are pink demons. A sign in each room shows the app, what Claude found there, and the access level.
- **Keycard doors are permissions.** Connect AI reports what this user may do on each connection. Read-only apps get a blue door, read and write apps a yellow door, and the keycards wait in the lobby ("READ ACCESS GRANTED BY CONNECT AI").
- **The PII vault.** If Connect AI's Data Security blocked anything during the run (for example a lookup by SSN), the level gets a vault behind a red door. There is no red key. Using the door shows Connect AI's actual refusal, for example `CONNECT AI: PII DETECTED IN INBOUND QUERY`, and the window shows walls of masked `XXXX` values.
- **A side panel** at http://localhost:8666 shows every step Claude takes through the MCP server, in plain words, as it happens; the blocked call shows in red. It also shows Claude's answer and a live kill feed.

Nothing is faked: rooms come from the queries Claude ran, permissions from Connect AI, and refusals from the tool results. Claude's own summary is only used for the room titles and the answer.

## Run

```bash
./setup.sh                        # one time: SDL 1.2 libs, psdoom-ng, zdbsp, Freedoom
./raid.sh                         # Claude runs the default question (about a minute), then you play
./raid.sh -q "Which vendors are we late paying?"
./raid.sh --again                 # replay the last briefing without a new Claude run
./raid.sh --demo                  # made-up data, no network
./raid.sh brief                   # only the briefing, for rehearsing
./raid.sh --again --claude-plays  # optional: Claude drives the marine (off unless you pass the flag)
python3 connect_doom.py score     # kill log; reset brings everyone back
```

The default question asks for at-risk customers across the CRM, support and billing apps, plus a compliance lookup of the customer with SSN `123-45-6789` in PostgreSQL PII. That lookup is what Connect AI blocks.

## Tests

```bash
make test          # V=1 lists each test; needs Pillow (`requirements-dev.txt`), plus node and shellcheck for those checks
```

The suite runs offline against fakes: a fake Connect AI MCP server, a fake `claude` CLI that streams a canned run, a tiny stand-in for `freedoom2.wad`, and fake `psdoom`, `zdbsp`, `security`, `op` and `open`. It covers the MCP client, `brief.py`, every bridge command and the panel server, the level, sign and DeHackEd builders, the panel's script (in Node), and `raid.sh` end to end in demo, live, `--again` and `brief` modes. It also shellchecks both scripts and confirms the psdoom patch matches `vendor/` when that checkout exists. GitHub Actions runs it on every push.

## Claude plays (optional)

`--claude-plays` hands the marine to Claude. It gets a small Doom MCP server (`play.py mcp`) with three tools, `look`, `go_to` and `attack`, next to the same read-only Connect AI server. Claude picks up the keycards, goes room by room, narrates each kill ("Hooli Corp's $15,127 overdue invoice is urgent, taking it down."), and tries the vault once. Its steps stream into the panel's Claude plays feed. It plays in god mode with a full chaingun, because it thinks in seconds, not tics. `PLAY_MODEL` (default sonnet) and `PLAY_MAX_TURNS` (default 60) tune it; a 20-turn run costs about $0.13.

Under the hood, psdoom has an agent channel (in the patch, `pr_process.c`): with `PSDOOM_AGENT_DIR` set, the game reads one command per line from `<dir>/cmd` (`MOVE x y`, `USE`, `TURN deg`, `ATTACK pid`, `SHOT`, `STOP`) and writes the player, every data monster and the last finished command to `<dir>/state.json`. The tools turn those into routes through the hall and doors. `SHOT` saves the frame as `DOOMnn.pcx`, so the whole mode also runs headless with `SDL_VIDEODRIVER=dummy`, no window and no input, which is how it's tested.

## Account

It runs as the Tech Evangelism account `te_us@cdata.com`, which has the Credit Card Number and US Social Security Number rules in **Data Security** and 47 connections with mixed permissions. Set `CONNECT_AI_USER` and `CONNECT_AI_PAT`, or let `raid.sh` read them from the macOS Keychain (service `connect-doom`, account = the user). If the Data Security rules are switched off, the SSN lookup goes through, no vault appears, and the panel shows the unmasked answer, which is its own demo moment.

All data in these accounts is synthetic. The game only reads: Claude gets Connect AI's read-only tools (`?ops=` on the MCP URL), and kills write nothing back: each one is logged to `state/kills.jsonl`, and the record is read again live through Connect AI with its room's query (in the background, so the game doesn't wait). The panel shows what came back and how long the round trip took.

## How it works

```
raid.sh
  brief.py ── claude -p, Connect AI MCP only (read-only tools) ── state/run.jsonl  (panel feed)
                                                              └─ state/mission.json (rooms, permissions, refusals)
  tools/make_arena.py + make_branding.py + make_deh.py ── state/level.wad, brand.wad, level.deh
  connect_doom.py poll ── re-runs each room's query through the MCP server every 15 s ── state/cache.json
  psdoom-ng ── list / kill / hit ──> connect_doom.py (local files only)
  connect_doom.py panel ── http://localhost:8666
```

- `brief.py` runs Claude Code headless with only the Connect AI MCP server. The token stays in the environment: the MCP config says `Basic ${CONNECT_AI_BASIC}` and Claude Code expands it. Claude must return, per app, one SELECT with the columns `Id`, `Label`, `Detail`, `Hot`; that contract is what lets any connector become a room.
- psdoom spawns monsters into three fixed boxes and picks the box from the monster's id mod 3, so room *i*'s records get ids with `id % 3 == i`.
- psdoom calls the hooks synchronously in the game loop, so `list` and `kill` only touch local files. A killed record is hidden from `list`, otherwise psdoom resurrects it.

## Pieces

- `connect_ai_mcp.py`: a small MCP client (initialize, tools/call, CSV results).
- `tools/make_arena.py`: draws the level on a 16-unit grid of rectangles (rooms, hall, lobby, doors, sign alcoves, vault, window) and derives every wall from where the cells differ; `zdbsp` builds the nodes.
- `tools/make_branding.py`: brand gray walls with an Agility yellow trim, the per-run signs, the vault's masked walls, and (with the CData brand kit) the Connect AI logo on the Esc menu. The kit stays out of git; `setup.sh` extracts it from `~/Documents/dev/cdata/BRANDKIT.zip` (override with `BRANDKIT_ZIP`).
- `tools/make_deh.py`: a DeHackEd patch that rewrites the keycard messages; `# *allow-long-strings*` lets Chocolate Doom take strings longer than the originals.
- `patches/psdoom-ng-connect-doom.patch`: `-nopslev` always means arena mode (upstream checks the game mode before Freedoom is identified, so monsters landed in the void); name tags show only the short name; the kill command's first output line becomes the on-screen message.
- Vanilla limits that shaped the signs: wall textures tile every 128 px vertically (hence the 128-tall alcoves), and multi-patch textures cap at 64 KB (hence one baked patch per sign).
- psdoom ignores SIGTERM; close the window or `pkill -9 -f trunk/src/psdoom`. On macOS a crash shows a system alert that outlives the game; click OK.
