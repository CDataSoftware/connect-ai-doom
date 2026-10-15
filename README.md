# connect-doom: Revenue Raid

Open Stripe invoices, pulled live through CData Connect AI, become Doom monsters. Invoices more than 90 days overdue are pink demons; newer ones are shotgun guys. Each monster is named with the customer's initials and the amount due (`CSI1499`). Kill one and it's logged as chased; the monster stays dead.

Built on [psdoom-ng](https://github.com/keymon/psdoom-ng), the Chocolate Doom fork that turns Unix processes into monsters. Its process hooks (`PSDOOMPSCMD`, `PSDOOMKILLCMD`, `PSDOOMRENICECMD`) point at `connect_doom.py` instead of `ps` and `kill`.

## Run

```bash
./setup.sh                 # one time: builds SDL 1.2 libs, psdoom-ng, zdbsp, Freedoom, arena map
./raid.sh --demo           # made-up invoices, no network
CONNECT_AI_USER=you@cdata.com CONNECT_AI_PAT=... ./raid.sh   # live Stripe data
python3 connect_doom.py score   # kill log
python3 connect_doom.py reset   # bring everyone back
```

Monsters appear in the three arenas north of the lobby a few seconds after the level starts (psdoom polls every 256 tics).

## How it works

```
psdoom-ng ── list ──> connect_doom.py ── reads ──> state/cache.json  <── poll (every 15s) ── Connect AI /api/query
          ── kill ──>                 ── appends ─> state/kills.jsonl ── SANDBOX=1 ──> INSERT via Connect AI
```

- psdoom calls the hooks synchronously in the game loop, so `list`/`kill` only touch local files (~40 ms). The background `poll` does the network.
- Source data is never modified. A killed record is hidden from `list`, otherwise psdoom resurrects it like an Arch-vile.
- `SANDBOX=1` also inserts each kill into `SANDBOX_TABLE` (default `[Snowflake_WH].[PUBLIC].[DOOM_FOLLOW_UPS]`, which has to be created first). Untested until a PAT exists.
- Swap the data with `SOURCE_QUERY` (columns `Id`, `CustomerName`, `AmountDue`, `DueDate`), `MAX_MONSTERS`, `DEMON_DAYS`.

## Pieces

- `tools/make_arena.py`: writes the MAP01 arena (three 1024x1024 rooms at x = 0, 2048, 4096, the layout psdoom's `-nopslev` mode spawns into) plus a lobby with weapons; `zdbsp` builds the nodes.
- `patches/psdoom-ng-nopslev-arena.patch`: makes `-nopslev` set arena mode unconditionally; upstream checks the game mode before Freedoom is identified, so monsters landed in the void.
- psdoom ignores SIGTERM; close the window or `pkill -9 -f trunk/src/psdoom`.
