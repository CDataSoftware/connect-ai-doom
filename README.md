# Connect AI × DOOM

You ask Claude a business question. Claude answers it through the Connect AI MCP server, and a Doom level is built from what Connect AI let it reach. Each app becomes a room, each record a monster, each permission a keycard door, and each blocked request a locked vault.

## Run

You need a Mac with [Homebrew](https://brew.sh), Claude Code signed in (the `claude` CLI), and a Connect AI account with a few connections and a personal access token. Google Chrome is optional; it gives the panel its own window.

```bash
./setup.sh                        # one time: SDL 1.2 libs, psdoom-ng, zdbsp, Freedoom
./raid.sh login                   # one time: your Connect AI email and token, kept in the Keychain
./raid.sh new                     # Claude asks Connect AI the default question (about a minute), then you play
./raid.sh new -q "Which vendors are we late paying?"
./raid.sh replay                  # play the last level again, without a new Claude run
./raid.sh offline                 # made-up data, no network
./raid.sh brief-only              # only Claude's run, no game (for rehearsing)
./raid.sh replay --claude-plays   # Claude plays instead of you; works with any mode
```

Every mode has the same two parts: Claude's briefing builds the level (`new` runs one, `replay` reuses the last), then the game keeps it live (monsters refresh from Connect AI every 15 seconds, and each kill reads its record again). The old names still work: `--again`, `--demo` and `brief`.

The game opens on the left of the screen and the side panel (http://localhost:8666) in its own Chrome window on the right, ready for a screen recording. Drag either window to resize it. `LAYOUT=off` opens the panel in a browser tab instead, and `GEOMETRY=1280x800` sets the game's size.

**Controls:** WASD to move, the mouse to turn, left click or Space to fire, E or right click to open doors, Shift to run, P to pause, Esc for the menu. Aim at a monster to see its record (app, name, detail) above the status bar. Clear all the rooms, then try the red door.

**At a booth:** run one live briefing before the doors open, then `./raid.sh replay` for each player: every game starts with all the records back, and no new Claude run is needed. If the network drops, `./raid.sh offline` plays with made-up data.

The default question asks for at-risk customers across the CRM, support and billing apps, plus a lookup of the customer with SSN `123-45-6789` in PostgreSQL PII. Connect AI blocks that lookup.

## How the MCP server drives the game

Connect AI is involved at three points: Claude's briefing, the monster refresh during play, and a live read on every kill.

```
                  Connect AI MCP server (read-only tools)
                 ▲              ▲                     ▲
   1. briefing   │   2. every   │ 15 s     3. each    │ kill
                 │              │                     │
  brief.py ── claude -p    connect_doom.py poll    connect_doom.py lookup
        │                       │                     │
  state/mission.json       state/cache.json      state/lookups.jsonl
        │                       │                     │
  level builders ──> WAD   psdoom ── list/kill/hit ──>│
                                │                     │
                         side panel at localhost:8666 reads it all
```

**1. The briefing builds the level.** `brief.py` runs Claude Code headless (`claude -p`) with one MCP server: Connect AI, limited to its read-only tools. Claude explores the catalogs and tables it needs, answers the question, and returns a level plan: up to three apps, each with one SELECT that returns the columns `Id`, `Label`, `Detail` and `Hot`. That column contract is what lets any connector become a room. Then `brief.py` turns what Connect AI reported into the level:

- **Rooms and monsters.** Each query's rows are the room's monsters. `Label` is the name tag, and rows with `Hot = 1` are pink demons.
- **Keycard doors.** Connect AI reports this user's permissions on each connection. An app with Insert or Update gets a yellow door, a read-only app a blue door. The colors reflect real permissions, but they are only a picture of them: picking up a key in the game changes no access anywhere.
- **The PII vault.** Every call that Connect AI's Data Security refused becomes a vault behind a red door with no red key. Using the door shows the actual refusal, for example `CONNECT AI: PII DETECTED IN INBOUND QUERY`.

Claude's own words are used only for the room titles and the answer. Rooms come from the queries it ran, doors from Connect AI's permissions, and the vault from the tool results. `tools/make_arena.py`, `make_branding.py` and `make_deh.py` turn the plan into the map, the signs and the keycard messages.

**2. Monsters stay live.** psdoom-ng is a Doom port that spawns a monster per process. Here it calls `connect_doom.py` instead of `ps`: `list` for the monsters, `kill` when one dies, `hit` when one is shot. These calls run inside the game loop, so they only read local files. `connect_doom.py poll` keeps those files fresh by re-running each room's query through the MCP server every 15 seconds. `connect_ai_mcp.py` is the small MCP client it uses.

**3. Each kill reads the record again.** A kill writes nothing back to your apps. It logs the kill and starts a background `lookup` that reads that one record live through Connect AI, using the room's own query. The game doesn't wait for it. The panel shows what came back and how long the round trip took.

**The side panel** shows Claude's steps through the MCP server as they happen during the briefing, with blocked calls in red. While a game is running it switches to a game view: the latest kill or hit, each room's progress, and the kill feed with each live read.

## Claude plays

`--claude-plays` hands the marine to Claude (`./raid.sh replay --claude-plays`). It gets a second MCP server, `play.py mcp`, with the tools `look`, `go_to` and `attack`, next to the same read-only Connect AI server. Claude picks up the keycards, clears the rooms room by room, narrates each kill, and tries the vault once. Its steps stream into the panel.

## Account

It runs as `te_us@cdata.com`, an account with Data Security rules for credit card and US Social Security numbers and connections with mixed permissions. Set `CONNECT_AI_USER` and `CONNECT_AI_PAT`, or let `raid.sh` read the token from the macOS Keychain (service `connect-doom`). All data in the account is synthetic.

## More

- [docs/internals.md](docs/internals.md): files, the psdoom patch, the agent channel, Doom limits
- [docs/testing.md](docs/testing.md): the offline test suite
