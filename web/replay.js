// Watch Claude play, in the browser: play.py's offline player (Autopilot) and its Doom tools, ported.
// It drives the marine through the game's agent channel, as on a Mac: one command at a time into
// /doom/agent/cmd, the game's state back from /doom/agent/state.json (Emscripten's in-memory files).
// It plays the way Claude did (keycards, each room with the urgent records first, the red door) and
// narrates each kill with the line Claude wrote about that record in a real run of this level.
// web.js hands it the level and a log for the panel's Claude feed; ai.js plays with its Doom tools.
window.ConnectDoomReplay = (() => {
  const AGENT = "/doom/agent";
  const ARENA_X = [0, 2048, 4096];           // tools/make_arena.py
  const ENGAGED = 160, STALL = 6;            // play.py: stop walking into monsters crowding a door
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const rand = (lo, hi) => lo + Math.random() * (hi - lo);

  class Stopped extends Error {}

  // ---------- the game channel (play.Game) ----------
  // One command count for everyone writing to the channel (a player, web.js's switches): the game
  // runs a command only when its number is past the last one.
  let seq = Math.floor(Date.now() / 1000);
  function send(line) {
    seq += 1;
    Module.FS.writeFile(`${AGENT}/cmd.tmp`, `${seq} ${line}\n`);
    Module.FS.rename(`${AGENT}/cmd.tmp`, `${AGENT}/cmd`);
  }

  class Game {
    constructor() { this.stopped = false; }

    async state() {
      if (this.stopped) throw new Stopped();
      for (let i = 0; i < 20; i++) {
        try { return JSON.parse(Module.FS.readFile(`${AGENT}/state.json`, {encoding: "utf8"})); }
        catch (e) { await sleep(100); }
      }
      throw new Error("the game isn't writing its state");
    }

    async do(verb, args = [], {timeout = 25, until = null} = {}) {
      seq = Math.max(seq, (await this.state()).command?.seq || 0);
      send(`${verb} ${args.map(a => Math.trunc(a)).join(" ")}`.trim());
      const mine = seq, end = Date.now() + timeout * 1000;
      while (Date.now() < end) {
        const s = await this.state();
        if (s.command.done_seq >= mine) return s.command.result;
        if (until && until(s)) { send("STOP"); return "interrupted"; }
        await sleep(100);
      }
      send("STOP");
      return "timeout";
    }
  }

  // Someone plays: the game steers the marine (with god mode) and leaves the mouse free. Stopped,
  // the marine is handed back where it stands, unless handBack is false (another player takes over).
  function takeOver(game) {
    send("OPTION 0 1");
    return (handBack = true) => {
      if (game.stopped) return;
      send("STOP");
      if (handBack) setTimeout(() => send("OPTION 0 0"), 150);
      game.stopped = true;
    };
  }

  // ai.js's Doom tools: look, go_to and attack, each taking the model's arguments.
  function doomTools(level) {
    const game = new Game();
    return {tools: tools(game, level.mission, Object.fromEntries(level.records.map(r => [r.pid, r]))),
            stop: takeOver(game)};
  }

  // ---------- the level (play.py) ----------
  const keySpots = rooms => [...new Set(rooms.map(r => r.writable ? "yellow" : "blue"))].sort()
    .map((_, i) => [2496 + 128 * i, -656]);

  function where(p) {
    if (p.y >= 0) return `room ${Math.min(Math.max(Math.floor((p.x + 512) / 2048), 0), 2) + 1}`;
    if (p.y > -448) return "hall";
    if (p.x >= 512 && p.x <= 1280) return "vault door";
    return "lobby";
  }

  function roomIndex(target, m) {
    const t = String(target).toLowerCase().trim();
    return m.rooms.findIndex((r, i) => [String(i + 1), `room ${i + 1}`, r.app.toLowerCase(),
      (r.connection || "").toLowerCase()].includes(t) || r.app.toLowerCase().includes(t));
  }

  const closeMonsters = (s, within = 200) => s.monsters.filter(mm => mm.health > 0 && mm.distance < within)
    .sort((a, b) => (a.visible === b.visible ? a.distance - b.distance : a.visible ? -1 : 1)).map(mm => mm.pid);

  function engaged(x, y) {
    const dists = [];
    return s => {
      const p = s.player;
      dists.push(Math.hypot(p.x - x, p.y - y));
      const near = s.monsters.some(mm => mm.health > 0 && mm.visible && mm.distance < ENGAGED);
      const stalled = dists.length > STALL && Math.min(...dists.slice(-STALL)) > dists[dists.length - STALL - 1] - 16;
      return near && stalled;
    };
  }

  function openFloor(p) {
    const here = where(p);
    if (here.startsWith("room")) return [ARENA_X[Number(here.slice(-1)) - 1] + 512, 512];
    if (here === "lobby") return [2560, -704];
    if (here === "vault door") return [896, -256];
    return [p.x, -256];
  }

  // ---------- the Doom tools (play.tool_look, tool_go_to, tool_attack) ----------
  function tools(game, m, recs) {
    async function move(x, y) {
      let result = await game.do("MOVE", [x, y], {until: engaged(x, y)});
      if (result === "blocked" && !closeMonsters(await game.state()).length) {
        await game.do("MOVE", openFloor((await game.state()).player));
        result = await game.do("MOVE", [x, y]);
      }
      return result;
    }

    async function look() {
      const s = await game.state(), p = s.player;
      const rooms = m.rooms.map((r, i) => ({room: i + 1, app: r.app, found: r.title,
        door: r.writable ? "yellow (read + write)" : "blue (read only)",
        monsters_left: s.monsters.filter(mm => mm.health > 0 && mm.pid % 3 === i).length}));
      const visible = s.monsters.filter(mm => mm.health > 0 && mm.visible).sort((a, b) => a.distance - b.distance)
        .slice(0, 12).map(mm => ({pid: mm.pid, app: recs[mm.pid]?.app, record: recs[mm.pid]?.label,
          detail: recs[mm.pid]?.detail, urgent: !!recs[mm.pid]?.demon, distance: mm.distance}));
      return {you_are_in: where(p), health: p.health, keycards: Object.keys(p.keys).filter(k => p.keys[k]),
              rooms, pii_vault: !!(m.denied || []).length, visible_monsters: visible};
    }

    async function goTo({place}) {
      const target = String(place || "").toLowerCase();
      const p = (await game.state()).player, here = where(p);
      let route = [];
      if (here.startsWith("room")) {
        const cx = ARENA_X[Number(here.slice(-1)) - 1] + 512;
        route.push([cx, 120], [cx, -150], [cx, -256]);
      } else if (here === "lobby") route.push([2560, -600], [2560, -256]);
      else if (here === "hall") route.push([p.x, -256]);

      if (target.includes("key")) {
        route = here === "lobby" ? [[2560, -600]] : [...route, [2560, -256], [2560, -600]];
        route.push(...keySpots(m.rooms));
      } else if (target.includes("vault")) {
        route.push([896, -256], [896, -470], ["TURN", 270], ["USE"], "WAIT", [896, -560]);
      } else {
        const i = roomIndex(target, m);
        if (i < 0) return {result: `Unknown place ${target}`};
        const cx = ARENA_X[i] + 512;   // Doom's use range is 64 units: stop short of the door (y = -48)
        route.push([cx, -256], [cx, -78], ["TURN", 90], ["USE"], "WAIT", [cx, 150], [cx, 400]);
      }

      for (const step of route) {
        if (step === "WAIT") await sleep(1200);           // the door takes about a second to open
        else if (step[0] === "USE") await game.do("USE");
        else if (step[0] === "TURN") await game.do("TURN", [step[1]]);
        else {
          const result = await move(...step);
          if (result !== "arrived") {
            const s = await game.state();
            if (target.includes("vault") && where(s.player) === "vault door")
              return {result: "The red door won't open. There is no red keycard: Connect AI refused this request. " +
                              m.denied[0].error.split("\n")[0], you_are_in: where(s.player)};
            const seen = new Set(s.monsters.filter(mm => mm.visible).map(mm => mm.pid));
            const shootable = closeMonsters(s).filter(pid => seen.has(pid));
            if (shootable.length) return {result: `Blocked: monsters in the way (${shootable.length} in sight).`,
                                          you_are_in: where(s.player), closest_pids: shootable.slice(0, 5)};
            return {result: `Couldn't get there (${result}).`, you_are_in: where(s.player)};
          }
        }
      }
      const s = await game.state();
      return {result: "arrived", you_are_in: where(s.player),
              keycards: Object.keys(s.player.keys).filter(k => s.player.keys[k])};
    }

    async function attack({pid}) {
      const rec = recs[pid] || {};
      const s = await game.state();
      const t = s.monsters.find(mm => mm.pid === pid && mm.health > 0);
      if (t && !t.visible && "x" in t) {
        const there = where(t);
        if (there.startsWith("room") && there !== where(s.player)) await goTo({place: there});
      }
      const result = await game.do("ATTACK", [pid], {timeout: 16});
      return {result, app: rec.app, record: rec.label, detail: rec.detail};
    }

    return {look, go_to: goTo, attack};
  }

  // ---------- the offline player (play.Autopilot) ----------
  const URGENT = ["{label}: {detail}. Urgent, taking it down.", "{label} is urgent ({detail}), going for it.",
                  "Next, {label}: {detail}. That one's urgent.", "{label} can't wait: {detail}."];
  const OTHER = ["{label}, {detail}. Taking it down.", "{label} next ({detail}).", "Clearing {label}: {detail}.",
                 "Now {label}: {detail}."];
  const AGAIN = ["Another one from {label}: {detail}.", "{label} again: {detail}.", "{label} has more: {detail}."];

  // play(level, log): level {mission, records: [monster], said: {pid: line}}; log(kind, data) feeds the
  // panel. Returns {stop} at once; done resolves when the run ends (or is stopped).
  function play(level, log) {
    const game = new Game();
    const m = level.mission, recs = Object.fromEntries(level.records.map(r => [r.pid, r]));
    const t = tools(game, m, recs);
    let calls = 0, turn = 0;
    const named = {}, killed = [];

    async function say(text) {
      await sleep(rand(1200, 2600));       // thinking
      const words = text.split(" ");
      for (let i = 0; i < words.length; i += 3) {
        if (game.stopped) throw new Stopped();
        log("text_delta", {text: words.slice(i, i + 3).join(" ") + (i + 3 < words.length ? " " : "")});
        await sleep(60);
      }
      log("thought", {text});
    }

    async function use(tool, args = {}) {
      const id = `replay_${++calls}`, start = Date.now();
      log("call", {id, server: "doom", tool, args, connection: null});
      const out = await t[tool](args);
      log("result", {id, tool, connection: null, error: false, rows: 0, text: JSON.stringify(out), ms: Date.now() - start});
      await sleep(rand(400, 1000));
      return out;
    }

    function line(pid) {
      turn += 1;
      if (level.said[pid]) return level.said[pid];
      const r = recs[pid];
      if (!r) return "Another record in the way, taking it down.";
      const seen = named[r.label] || 0;
      named[r.label] = seen + 1;
      const lines = seen ? AGAIN : r.demon ? URGENT : OTHER;
      return lines[turn % lines.length].replace("{label}", r.label).replace("{detail}", String(r.detail || "").slice(0, 80))
        .replace(", .", ".");
    }

    async function attack(pid) {
      await say(line(pid));
      const out = await use("attack", {pid});
      if (out.result === "killed") killed.push(pid);
      return out.result;
    }

    const alive = async room => (await game.state()).monsters
      .filter(mm => mm.health > 0 && (room === undefined || mm.pid % 3 === room));

    async function go(place, tries = 4) {
      let out;
      for (let n = 0; n < tries; n++) {
        out = await use("go_to", {place});
        if (out.result === "arrived" || out.result.includes("red door")) return out;
        const seen = new Set((await alive()).filter(mm => mm.visible).map(mm => mm.pid));
        for (const pid of (out.closest_pids || []).filter(pid => seen.has(pid)).slice(0, 3)) await attack(pid);
      }
      return out;
    }

    async function clear(room) {
      const failed = new Set();
      for (let n = 0; n < 60; n++) {
        const left = (await alive(room)).filter(mm => !failed.has(mm.pid));
        if (!left.length) return;
        if (n % 5 === 4) await use("look");
        // what Claude does: what it can see, urgent first, then the nearest
        left.sort((a, b) => (a.visible === b.visible ? 0 : a.visible ? -1 : 1)
          || (!!recs[b.pid]?.demon - !!recs[a.pid]?.demon) || a.distance - b.distance);
        if (!["killed", "gone"].includes(await attack(left[0].pid))) failed.add(left[0].pid);
      }
    }

    async function run() {
      const started = Date.now();
      log("question", {text: m.question});
      log("session", {model: "claude", tools: ["mcp__doom__attack", "mcp__doom__go_to", "mcp__doom__look"]});
      const total = (await alive()).length;
      await use("look");
      const apps = m.rooms.map(r => r.app);
      await say(`${apps.length} rooms: ${apps.join(", ")}. Keycards first, then the urgent records, room by room.`);
      await go("keycards");
      for (const [i, r] of m.rooms.entries()) {
        if (!(await alive(i)).length) continue;
        await say(`Room ${i + 1}, ${r.app}: ${r.title}.`);
        await go(`room ${i + 1}`);
        await clear(i);
      }
      if ((m.denied || []).length) {
        await say("The rooms are done. Trying the PII vault's red door once, to see what Connect AI does.");
        await go("vault", 1);
        await say(`Connect AI refused the request: ${m.denied[0].error.split("\n")[0]}. There's no red keycard, so the vault stays shut.`);
      }
      const urgent = killed.filter(pid => recs[pid]?.demon).length;
      let summary = `Cleared ${total - (await alive()).length} of ${total} records across ${apps.join(", ")}, ${urgent} of them urgent.`;
      if ((m.denied || []).length) summary += "\nConnect AI blocked the PII vault request, so that data never reached the game.";
      await say(summary);
      log("done", {cost_usd: null, duration_ms: Date.now() - started, turns: calls, text: summary});
    }

    const stop = takeOver(game);
    const done = sleep(300).then(run).catch(e => { if (!(e instanceof Stopped)) console.error("replay:", e); });
    return {done, stop};
  }

  return {play, doomTools, send, Stopped};
})();
