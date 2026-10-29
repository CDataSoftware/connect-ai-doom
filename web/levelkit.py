"""The browser version's level builder. web/ai.js runs it in Pyodide with the Mac version's own code
(brief.py, connect_doom.py and tools/), so a level built in a browser is built the way raid.sh
builds one. The page does what needs the network: it asks Connect AI (ai.js) and hands the answers
over. zdbsp, compiled to WebAssembly, adds the nodes after build().

  plan(question, final, calls, connections) -> mission JSON     brief.build_mission
  build(answers)                                                web/build.sh's level steps, into /level
"""
import concurrent.futures
import contextlib
import io
import json
import os
import runpy
import sys

KIT, LEVEL = "/kit", "/level"
os.environ["CONNECT_DOOM_STATE"] = LEVEL
sys.path[:0] = [KIT, f"{KIT}/tools"]

import brief  # noqa: E402
import connect_ai_mcp  # noqa: E402
import connect_doom  # noqa: E402

CONNECTIONS, ANSWERS = [], {}


class Answered(connect_ai_mcp.Client):
    """Connect AI as the page found it: the connections it lists, and its answer to each call the
    page made (keyed by tool and arguments)."""

    def __init__(self, *args, **kwargs):
        pass

    def connections(self):
        return CONNECTIONS

    def call(self, tool, **args):
        found = ANSWERS.get(json.dumps([tool, args], sort_keys=True))
        if found is None:
            raise connect_ai_mcp.MCPError(f"the page didn't ask Connect AI for {tool} {args}")
        if found["error"]:
            raise connect_ai_mcp.MCPError(found["text"].removeprefix("Error: "))
        return found["text"]


class InTurn(concurrent.futures.Executor):
    """connect_doom.fetch's thread pool, one call after another: Pyodide can't start threads."""

    def __init__(self, *args):
        pass

    def submit(self, fn, *args):
        future = concurrent.futures.Future()
        future.set_result(fn(*args))
        return future


connect_ai_mcp.Client = Answered
concurrent.futures.ThreadPoolExecutor = InTurn


def plan(question, final, calls, connections):
    """The mission from the model's final answer and its Connect AI calls (brief.build_mission:
    rooms, and the refused and masked requests), with each connection's permissions."""
    CONNECTIONS[:] = json.loads(connections)
    return json.dumps(brief.build_mission(question, final, json.loads(calls)))


def script(path, *args):
    """Run one of the Mac version's scripts as raid.sh would; returns what it printed."""
    sys.argv = [path, *args]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        runpy.run_path(f"{KIT}/{path}", run_name="__main__")
    return out.getvalue()


def build(answers):
    """The level's files in /level from the mission plan() wrote and the page's answers to its
    rooms' queries: what web/build.sh does with a level, up to zdbsp (level-raw.wad)."""
    for a in json.loads(answers):
        ANSWERS[json.dumps([a["tool"], a["args"]], sort_keys=True)] = a
    mission = json.loads(connect_doom.MISSION.read_text())
    stats = []
    monsters = connect_doom.fetch(mission, stats)
    if not monsters:
        why = "; ".join(f"{s['connection']}: {s.get('error', 'no rows')}" for s in stats)
        raise RuntimeError(f"Connect AI returned no records for the level's rooms ({why}).")
    # like the site's own level (web/snapshot.py): records read once, as the level was built
    connect_doom.write_cache(monsters, "snapshot", stats)
    m = str(connect_doom.MISSION)
    script("tools/make_branding.py", f"{KIT}/freedoom2.wad", f"{KIT}/assets/brand", f"{LEVEL}/brand.wad", "--mission", m)
    script("tools/make_arena.py", f"{LEVEL}/level-raw.wad", "--mission", m, "--records", str(connect_doom.CACHE), "--brand")
    script("tools/make_deh.py", m, f"{LEVEL}/level.deh", "--no-gore")   # no blood, as on the site's own level
    with open(f"{LEVEL}/monsters.txt", "w") as f:
        f.write(script("tools/themes.py", "monsters", m))
    with open(f"{LEVEL}/state.json", "w") as f:
        json.dump(connect_doom.panel_state(), f)   # the panel's state before the first kill
    return len(monsters)
