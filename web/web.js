// connect-doom in a browser: the side panel's full-screen stage (panel/index.html, loaded after this
// script) with the game, compiled to WebAssembly, on the stage's screen. What raid.sh and the bridge
// (connect_doom.py) do on a Mac happens here: the panel's /state.json starts from the state
// web/build.sh baked (connect_doom.py state) and takes this game's kills, hits and red-door tries, and
// psdoom's list / kill / hit / vault commands are answered from the level's records. replay.js plays
// the level the way Claude did, for "Watch Claude play"; with the visitor's own AI set up in the AI
// settings (ai.js), their model plays instead, and answers their questions through Connect AI.
(() => {
  const AI = window.ConnectDoomAI;
  const REPO = "https://github.com/CDataSoftware/connect-ai-doom";
  const CONNECT_AI = "https://www.cdata.com/ai/";
  const external = (href, text, cls = "") =>
    `<a href="${href}" target="_blank" rel="noopener noreferrer"${cls ? ` class="${cls}"` : ""}>${text}</a>`;
  const esc = text => String(text).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[c]);
  // A custom event for the site's Plausible analytics (web/build.sh puts its script on the page)
  const trackEvent = (name, props) => window.plausible?.(name, props ? {props} : undefined);

  // ---------- the stage's layout (raid.sh passes it as ?stage=gx,gy,gw,gh,split,top) ----------
  // The game takes two thirds of the window at 4:3, with room for the brand bars: 90 px above (the
  // links and the logos), and 150 px below (the question, Claude's line and the footer).
  function layout() {
    const w = innerWidth, h = innerHeight, split = Math.round(w * 2 / 3);
    const gh = Math.max(200, Math.min(h - 240, (split - 48) * 3 / 4)), gw = gh * 4 / 3;
    const gy = Math.min((h - gh) / 2, h - gh - 150);
    return [Math.round((split - gw) / 2), Math.round(gy), Math.round(gw), Math.round(gh), split, 0];
  }
  const params = new URLSearchParams(location.search);
  if (!params.has("stage")) {
    params.set("stage", layout().join(","));
    history.replaceState(null, "", `${location.pathname}?${params}`);
  }
  // The panel reads ?stage= as it loads; then the address goes back to the plain one people share.
  addEventListener("DOMContentLoaded", () => history.replaceState(null, "", location.pathname));
  addEventListener("resize", () => {
    const css = document.documentElement.style;
    layout().forEach((v, i) => css.setProperty(["--gx", "--gy", "--gw", "--gh", "--split", "--top"][i], `${v}px`));
  });

  // ---------- this game: records, kills, hits, red-door tries, and Claude's feed ----------
  let state = null, monsters = [], said = {};
  const kills = [], hits = [], vault = [], playEvents = [];
  const alive = () => monsters.filter(m => !kills.some(k => k.id === m.id));
  const events = new EventTarget();   // "cleared" (the last record), "vault" (the red door was tried)

  function currentState() {
    const dead = new Set(kills.map(k => k.id));
    const rooms_live = state.rooms_live.map((room, i) => {
      const mine = monsters.filter(m => m.pid % 3 === i), left = mine.filter(m => !dead.has(m.id));
      return {...room, left: left.length, urgent_left: left.filter(m => m.demon).length,
              killed: mine.length - left.length};
    });
    return {...state, rooms_live, kills: kills.slice(-12), kill_total: kills.length, hits: hits.slice(-40),
            standing: alive().length, vault: vault.slice(-5), play_events: playEvents, ask: asking.state()};
  }

  // Local time without a zone, as connect_doom.py writes killed_at (the panel parses it as local).
  function localTime(d = new Date()) {
    return new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
  }

  // connect_doom.kill_message: the in-game line for a kill.
  function killMessage(m) {
    const left = alive(), inRoom = left.filter(x => x.pid % 3 === m.pid % 3);
    if (!left.length) return "ALL RECORDS CLEARED!" + ((state.mission?.denied || []).length ? " NOW TRY THE RED DOOR" : "");
    if (!inRoom.length) return `${m.app} room clear! ${left.length} left elsewhere`.toUpperCase();
    return `${m.app}: ${m.label}`.slice(0, 36).toUpperCase() + `, ${inRoom.length} LEFT`;
  }

  function bridge(command) {
    const [verb, arg] = command.split(" ");
    const m = monsters.find(x => x.pid === Number(arg));
    const now = Date.now() / 1000;
    if (verb === "list") return alive().map(x => `connectai ${x.pid} ${x.name} ${x.demon}\n`).join("");
    if (verb === "kill" && m && alive().includes(m)) {
      // said: what Claude wrote about this record when it played the level (web/snapshot.py)
      kills.push({...m, killed_at: localTime(), mode: "read-only", lookup: null,
                  said: said[m.pid] || null});
      if (!alive().length) setTimeout(() => events.dispatchEvent(new Event("cleared")), 1500);
      return killMessage(m) + "\n";
    }
    if (verb === "hit" && m) hits.push({...m, at: now});
    if (verb === "vault") {
      // the request Connect AI blocked when Claude built the level, and what Connect AI said
      const d = (state.mission?.denied || [])[0];
      if (d) {
        vault.push({connection: d.connection, tool: d.tool, request: d.request, blocked: true, ms: 0, at: now,
                    text: d.error.split("\n")[0]});
        events.dispatchEvent(new Event("vault"));
      }
    }
    return "";
  }

  // ---------- a question about the visitor's own data, from the panel's question box ----------
  // The visitor's model answers through Connect AI (ai.js), its steps in the panel's feed and its
  // answer under the box. No new level: building one needs the Mac version.
  const asking = {
    run: null, answer: "", who: "",
    // the panel's state.ask: unavailable greys the box out, busy while answering, note under it
    state() {
      const settings = AI.load(), ai = AI.active(settings);
      const ask = {default: state.mission?.question || ""};
      if (!ai || !AI.hasConnectAI(settings))
        return {...ask, unavailable: "Ask your own data a question: add your AI key and your Connect AI token in AI settings."};
      if (coach?.playing()) return {...ask, unavailable: `Ask once ${coach.playing()} is done playing.`};
      if (this.run) return {...ask, busy: `${modelName(ai.model)} is reading your data through Connect AI…`};
      const mine = AI.connections();
      return {...ask, note: this.answer ? `${this.who}: ${this.answer}`
        : `Enter asks ${modelName(ai.model)} through Connect AI${mine?.length ? `, with ${connectionList(mine)}` : ""}. Its steps show in the feed.`};
    },
    start(question) {
      const settings = AI.load(), ai = AI.active(settings);
      if (!ai || !AI.hasConnectAI(settings) || this.run || coach?.playing()) return "Not now: " + (this.state().unavailable || "it's busy.");
      trackEvent("Ask", {provider: ai.provider});
      playEvents.length = 0;
      state.game = {...state.game, asked: true};
      this.answer = "";
      this.who = modelName(ai.model);
      const run = this.run = AI.ask(settings, question, (kind, data) => playEvents.push({t: Date.now() / 1000, kind, ...data}));
      run.done.then(answer => { this.answer = answer || "No answer came back."; },
                    e => { this.answer = `Couldn't answer: ${e.message}`; })
        .finally(() => { if (this.run === run) this.run = null; });
      return null;
    },
    stop() { this.run?.stop(); this.run = null; },
  };

  // The panel polls /state.json and posts its question box to /ask; nothing here talks to a server.
  const realFetch = window.fetch.bind(window);
  const json = (body, status = 200) => new Response(JSON.stringify(body), {status, headers: {"Content-Type": "application/json"}});
  window.fetch = (url, opts) => {
    const path = new URL(url, location.href).pathname;
    if (path.endsWith("/state.json")) return Promise.resolve(json(state ? currentState() : {}));
    if (path.endsWith("/ask") && opts?.method === "POST") {
      const error = asking.start(String(JSON.parse(opts.body).question || "").trim());
      return Promise.resolve(error ? json({error}, 409) : json({note: `${asking.who} is on it. Watch the feed.`}));
    }
    return realFetch(url, opts);
  };

  // "48 connections, such as Acumatica, ADP and Bullhorn CRM"
  function connectionList(list) {
    const names = [...new Set(list.map(c => c.source))].slice(0, 3);
    const shown = names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names.at(-1)}` : names[0];
    return `${list.length} connection${list.length === 1 ? "" : "s"}, ${list.length > names.length ? "such as " : ""}${shown}`;
  }
  const article = word => /^[AEIOU]/i.test(word) ? "an" : "a";
  // A model's name as the page shows it: GPT-5.6-SOL, CLAUDE-SONNET-5-5 (OpenRouter's maker/ dropped)
  const modelName = id => String(id).replace(/^[^/]*\//, "").toUpperCase();
  let coach = null;

  // ---------- the game ----------
  const files = {"/doom/level.wad": "level/level.wad", "/doom/brand.wad": "level/brand.wad",
                 "/doom/level.deh": "level/level.deh", "/doom/labels.tsv": "level/labels.tsv"};
  const get = u => realFetch(u).then(r => { if (!r.ok) throw new Error(`${u}: ${r.status}`); return r; });

  // Freedoom's WAD, gzipped by web/build.sh: most of the download, so it reports its progress. A
  // server may unzip it on the way (Content-Encoding); then the bytes arrive ready to use.
  async function downloadWad(onProgress) {
    const r = await get("freedoom2.wad.gz");
    const total = Number(r.headers.get("Content-Length")) || 0;
    const reader = r.body.getReader(), chunks = [];
    let loaded = 0;
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      chunks.push(value);
      loaded += value.length;
      onProgress(loaded, total);
    }
    const bytes = new Blob(chunks);
    const head = new Uint8Array(await bytes.slice(0, 2).arrayBuffer());
    if (head[0] !== 0x1f || head[1] !== 0x8b) return bytes.arrayBuffer();
    return new Response(bytes.stream().pipeThrough(new DecompressionStream("gzip"))).arrayBuffer();
  }
  // The keys the game plays with: Firefox has defaults for some of them (find as you type, scrolling)
  // that Emscripten's SDL leaves on, since the game asks for typed characters.
  const GAME_KEYS = new Set(["KeyW", "KeyA", "KeyS", "KeyD", "KeyE", "KeyP", "Space", "ArrowUp", "ArrowDown",
    "ArrowLeft", "ArrowRight", "ShiftLeft", "ShiftRight", "AltLeft", "AltRight", "Slash", "Quote", "Tab"]);

  async function start() {
    const canvas = document.createElement("canvas");
    canvas.id = "canvas";
    canvas.tabIndex = 0;
    canvas.setAttribute("aria-label", "The game");
    // A stylesheet rule, not inline styles: Emscripten's SDL sets the canvas's inline size.
    const style = document.createElement("style");
    style.textContent = "#canvas { position: absolute !important; inset: 0 !important; width: 100% !important; " +
      "height: 100% !important; z-index: 1; image-rendering: pixelated; background: #000; outline: none; cursor: none; }";
    document.head.appendChild(style);
    document.getElementById("stage-screen").appendChild(canvas);
    addEventListener("keydown", e => {
      if (GAME_KEYS.has(e.code) && (document.activeElement === canvas || document.pointerLockElement === canvas))
        e.preventDefault();
    }, true);

    // The level first (small), so the start card is up while Freedoom downloads.
    const [baked, cache, saidJson, monsterKinds, ...bufs] = await Promise.all([
      get("level/state.json").then(r => r.json()), get("level/cache.json").then(r => r.json()),
      get("level/said.json").then(r => r.json()), get("level/monsters.txt").then(r => r.text()),
      ...Object.values(files).map(u => get(u).then(r => r.arrayBuffer()))]);
    monsters = cache.monsters;
    said = saidJson;
    // The question box is the visitor's own model's (asking); a new level needs the Mac version.
    state = {...baked, game: {mode: "player", started: Date.now() / 1000}};
    plainWords();
    coach = makeCoach(canvas);
    coach.settingsChanged();
    // the visitor's connections, for the question box; when Connect AI won't answer, the box just
    // doesn't name them, and asking shows the error
    const saved = AI.load();
    if (AI.hasConnectAI(saved)) AI.checkConnectAI(saved).catch(e => console.warn("Connect AI:", e.message));
    const wad = await downloadWad(coach.loading);

    // The engine's window caption (SDL_WM_SetCaption) replaces the tab title as the game boots; put this page's back.
    const pageTitle = document.title;
    const titleWatch = new MutationObserver(() => {
      if (document.title !== pageTitle) { titleWatch.disconnect(); document.title = pageTitle; }
    });
    titleWatch.observe(document.querySelector("title"), {childList: true, characterData: true, subtree: true});
    window.Module = {
      canvas,
      print: s => console.log(s),
      printErr: s => console.warn(s),
      bridge,
      preRun: [() => {
        FS.mkdir("/doom");
        FS.mkdir("/doom/agent");   // the agent channel: replay.js steers through it; the shotgun start lives there too
        Object.keys(files).forEach((path, i) => FS.writeFile(path, new Uint8Array(bufs[i])));
        FS.writeFile("/doom/freedoom2.wad", new Uint8Array(wad));
        // raid.sh's controls: WASD, the mouse turns, left click or Space fires, E or right click opens doors.
        FS.writeFile("/doom/controls.cfg", "key_up 17\nkey_down 31\nkey_strafeleft 30\nkey_straferight 32\n" +
          "key_left 75\nkey_right 77\nkey_fire 57\nkey_use 18\nkey_speed 42\nkey_strafe 56\nuse_mouse 1\n" +
          "mouseb_fire 0\nmouseb_strafe -1\nmouseb_forward -1\nmouse_sensitivity 6\n");
        FS.writeFile("/doom/extra.cfg", "novert 1\ngrabmouse 1\nmouseb_use 1\nshow_endoom 0\nfullscreen 0\n" +
          "screen_width 640\nscreen_height 480\n");
        const [models, pick] = menuModels();
        Object.assign(ENV, {PSDOOMPSCMD: "list", PSDOOMKILLCMD: "kill", PSDOOMRENICECMD: "hit",
          PSDOOMVAULTCMD: "vault", PSDOOM_LABELS: "/doom/labels.tsv", PSDOOM_MONSTERS: monsterKinds.trim(),
          PSDOOM_AGENT_DIR: "/doom/agent", HOME: "/doom", PSDOOM_AGENT_MODELS: models, PSDOOM_AGENT_MODEL: pick});
      }],
      // No sound yet: Emscripten's SDL mixer can't play the buffers Chocolate Doom builds.
      onRuntimeInitialized: () => {
        Module.callMain(["-iwad", "/doom/freedoom2.wad", "-file", "/doom/level.wad",
          "/doom/brand.wad", "-deh", "/doom/level.deh", "-nopslev", "-psallusers", "-warp", "1", "-skill", "1",
          "-window", "-nosound", "-nomusic", "-config", "/doom/controls.cfg", "-extraconfig", "/doom/extra.cfg"]);
        coach.ready();
      },
    };
    const script = document.createElement("script");
    script.src = "psdoom.js";
    document.body.appendChild(script);
  }

  // The Options menu's Model row: offline Claude, and the visitor's model when it's set up (picked),
  // by a name short enough for the menu (OpenRouter's maker/ dropped). It goes into the game's
  // state.json as it is, so only the characters it allows.
  function menuModels() {
    const ai = AI.active(AI.load()), model = ai && ai.model.replace(/^[^/]*\//, "").replace(/[^\w.:-]/g, "").slice(0, 17);
    return model ? [`${AI.OFFLINE},${model}`, model] : [AI.OFFLINE, AI.OFFLINE];
  }

  // The AI settings changed: the game's Model row follows.
  function settingsChanged() {
    const [models, pick] = menuModels();
    if (window.Module?._web_set_models) {
      const list = Module.stringToNewUTF8(models), first = Module.stringToNewUTF8(pick);
      Module._web_set_models(list, first);
      Module._free(list);
      Module._free(first);
    }
    coach?.settingsChanged();
  }

  // What the game says about itself (/doom/agent/state.json): the Options menu's switches, say.
  function gameState() {
    try { return JSON.parse(Module.FS.readFile("/doom/agent/state.json", {encoding: "utf8"})); }
    catch (e) { return null; }   // not started yet, or caught mid-write
  }

  // The browser can refuse the pointer lock (say, right after Esc); the next click asks again.
  function lockPointer(canvas) {
    try { canvas.requestPointerLock()?.catch?.(() => {}); } catch (e) {}
  }

  // The panel speaks to people running a booth; a visitor here needs plainer words.
  function plainWords() {
    const relabel = (sel, text) => { const el = document.querySelector(sel); if (el) el.textContent = text; };
    relabel("#s-model + .lbl", "BUILT BY");
    relabel("#stage .bar.bottom .line .tag", "THE QUESTION");
    const briefing = document.querySelector("#feed")?.parentElement.querySelector("h2");
    if (briefing) briefing.textContent = "DOOMCP · EVERY CALL CLAUDE MADE TO CONNECT AI";
    const fine = document.querySelector("#stage .bar.bottom .fine");
    // the page's footer: what Connect AI is, the code, and that this is a fan project
    if (fine) fine.innerHTML = "Every monster is a record Claude read through " + external(CONNECT_AI, "CData Connect AI") +
      ", one MCP server between AI agents and your business data. " + external(AI.SIGNUP_URL, "Try Connect AI free") + " · " +
      external(REPO, "Code on GitHub") + "<br>A fan project, not affiliated with or endorsed by id Software, ZeniMax Media " +
      "or Microsoft. DOOM is a trademark of id Software LLC.";
    const board = document.querySelector("#board h2");
    if (board) {
      board.firstChild.textContent = "DOOMCP LEADERBOARD: THIS LEVEL";
      board.insertAdjacentHTML("afterend", '<p class="board-lead">We gave each AI model the same Doom ' +
        "tools and the same Connect AI access, and let it play this level. Each run is scored from the game itself.</p>");
    }
    const note = document.querySelector("#board .note");
    if (note) note.textContent = "Cleared: runs that killed every record. Time: the median time to the last kill. " +
      "Stuck: moves with nothing to show for them. Urgent 1st: how often it went for the urgent records first. " +
      "Vault: runs that tried the red door and took Connect AI's no. REF: Claude Code, playing by the same rules " +
      "through its own harness, for reference.";
    relabel("#newq h2", "ASK YOUR OWN DATA A QUESTION");
    relabel("#newq-go", "ASK");
    // the question box needs the visitor's own model: the way to set it up, right there
    const askRow = document.querySelector("#newq .row");
    if (askRow) {
      askRow.insertAdjacentHTML("beforeend", '<button type="button" class="link" id="newq-settings">AI settings</button>');
      document.getElementById("newq-settings").addEventListener("click", openSettings);
    }
    const style = document.createElement("style");
    style.textContent = `.board-lead { margin: 0 0 10px; font-size: 14px; line-height: 1.45; color: #E5E2DA; }
      #board-meta, #board .model .data { display: none; }   /* rule versions, data sources, content-filter stops: bench notes */
      #stage .bar.bottom .fine a { color: var(--agility); }`;
    document.head.appendChild(style);
  }

  // ---------- new players: what to do, a step at a time; or watch Claude play ----------
  // A card over the game to start: play, or watch Claude. Playing, a banner per step: grab the
  // keycards (until the player holds every one the level needs), clear the rooms, then the red door.
  // At the end, a card with what just happened and where to go next.
  function makeCoach(canvas) {
    const mission = state.mission || {};
    const rooms = mission.rooms || [];
    const apps = rooms.map(r => r.app);
    const appList = apps.length > 1 ? `${apps.slice(0, -1).join(", ")} and ${apps.at(-1)}` : apps.join("");
    const need = (rooms.some(r => !r.writable) ? 1 : 0) | (rooms.some(r => r.writable) ? 2 : 0);   // 1 blue, 2 yellow
    const denied = (mission.denied || []).length > 0;
    const screen = document.getElementById("stage-screen");
    const style = document.createElement("style");
    style.textContent = `
      .coach-card { position: absolute; inset: 0; z-index: 3; display: flex; align-items: center; justify-content: center;
                    background: rgba(13, 13, 18, .88); padding: 24px; }
      .coach-card[hidden], #coach-tip[hidden] { display: none; }
      .coach-card .card { max-width: 640px; display: flex; flex-direction: column; gap: 16px; }
      .coach-card .eyebrow { font: 9px/1.6 var(--pixel); color: var(--agility); letter-spacing: 1px; }
      .coach-card h3 { margin: 0; font: 700 clamp(24px, 3vw, 36px)/1.15 var(--sans); color: var(--clarity); }
      .coach-card h3 .red { color: var(--doom-red); }
      .coach-card p { margin: 0; font: 17px/1.5 var(--sans); color: #E5E2DA; }
      .coach-card p.what { font-size: 15px; color: var(--balance); border-left: 3px solid var(--agility); padding-left: 12px; }
      .coach-card ol { margin: 0; padding: 0; list-style: none; display: grid; gap: 10px; counter-reset: step; }
      .coach-card li { counter-increment: step; display: grid; grid-template-columns: 34px 1fr; gap: 12px; align-items: center;
                       font: 16px/1.4 var(--sans); color: #E5E2DA; }
      .coach-card li::before { content: counter(step); font: 12px/34px var(--pixel); text-align: center; color: var(--depth);
                               background: var(--agility); border-radius: 3px; }
      .coach-card li b { color: var(--clarity); }
      .coach-card .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
      .coach-card .btn { font: 11px/1 var(--pixel); color: var(--depth); background: var(--agility); border: 0; border-radius: 3px;
                         padding: 15px 18px; cursor: pointer; letter-spacing: 1px; text-decoration: none; }
      .coach-card .btn.alt { background: var(--depth-3); color: var(--clarity); border: 1px solid var(--line); }
      .coach-card .byo { display: flex; gap: 14px; align-items: center; justify-content: space-between; padding: 10px 12px;
                         border: 1px dashed var(--line); border-radius: 4px; font: 14px/1.4 var(--sans); color: var(--balance); }
      .coach-card .byo b { color: var(--clarity); }
      .coach-card .byo .btn { flex: none; padding: 11px 12px; }
      .coach-card .btn:focus-visible { outline: 3px solid var(--clarity); outline-offset: 3px; }
      .coach-card .keys { font: 13px/1.5 var(--mono); color: var(--balance); }
      /* above Doom's status bar (its bottom 16%), clear of Doom's own message line at the top */
      #coach-tip { position: absolute; z-index: 2; bottom: calc(16% + 16px); left: 50%; transform: translateX(-50%); width: min(560px, 86%);
                   background: rgba(13, 13, 18, .9); border: 2px solid var(--agility); border-radius: 4px; padding: 12px 16px;
                   pointer-events: none; display: flex; flex-direction: column; gap: 6px; animation: coach-in .35s ease-out; }
      #coach-tip b { font: 9px/1.6 var(--pixel); color: var(--agility); letter-spacing: 1px; }
      #coach-tip span { font: 16px/1.4 var(--sans); color: var(--clarity); }
      @keyframes coach-in { from { opacity: 0; transform: translate(-50%, 8px); } }
      @media (prefers-reduced-motion: reduce) { #coach-tip { animation: none; } }`;
    document.head.appendChild(style);

    const card = (id, html) => {
      const el = document.createElement("div");
      el.id = id;
      el.className = "coach-card";
      el.setAttribute("role", "dialog");
      el.setAttribute("aria-modal", "true");
      el.setAttribute("aria-labelledby", `${id}-title`);
      el.innerHTML = `<div class="card">${html}</div>`;
      return el;
    };
    const intro = card("coach-intro", `
      <div class="eyebrow">CONNECT AI × DOOM · A.K.A. DOOMCP</div>
      <h3 id="coach-intro-title">Every monster is a <span class="red">record</span>.</h3>
      <p>Claude read a demo company's ${appList} through the Connect AI MCP server and built this level from what it found.</p>
      <p class="what">CData Connect AI is one MCP server between AI agents like Claude and your business apps. It applies
        your permissions and your data security rules to every request.</p>
      <ol>
        <li><span><b>Grab your keys.</b> Keycards are your permissions: Connect AI gives you one for each app you can open.</span></li>
        <li><span><b>Clear the rooms.</b> Each room is an app, and each monster is a record from it.</span></li>
        <li><span><b>Try the red door.</b> It guards data Connect AI blocked.</span></li>
      </ol>
      <div class="row"><button class="btn" type="button" data-go="play" disabled>LOADING…</button>
        <button class="btn alt" type="button" data-go="watch" disabled>WATCH CLAUDE PLAY</button></div>
      <div class="byo"><span><b>Bring your own AI.</b> Let your Claude, ChatGPT or OpenRouter model play, and ask your
        own data a question through Connect AI.</span><button class="btn alt" type="button" data-go="settings">AI SETTINGS</button></div>
      <div class="keys">WASD move · mouse turn · click fire · walk into doors · Esc frees the mouse</div>`);
    const end = card("coach-end", "");
    end.hidden = true;
    const tip = document.createElement("div");
    tip.id = "coach-tip";
    tip.hidden = true;
    tip.setAttribute("role", "status");
    screen.append(intro, end, tip);

    // run: whoever plays instead of the player, offline Claude (replay.js) or the visitor's model
    // (ai.js), as {name, stop(handBack)}; null while the player plays.
    let hideAt = 0, step = 0, run = null, ended = false;
    const show = (title, text, seconds) => {
      tip.innerHTML = `<b>${title}</b><span>${text}</span>`;
      tip.hidden = false;
      tip.style.animation = "none"; void tip.offsetWidth; tip.style.animation = "";
      hideAt = seconds ? Date.now() + seconds * 1000 : 0;
    };

    function playYourself() {
      intro.hidden = end.hidden = true;
      if (run) { run.stop(); run = null; }
      state.game = {...state.game, mode: "player"};
      canvas.focus();
      lockPointer(canvas);
      if (step === 0) {
        step = 1;
        show("STEP 1 OF 3 · GRAB YOUR KEYS", "Keycards are your permissions. Connect AI gives you one for each app you can open. They're in the lobby.");
      } else tip.hidden = true;
    }

    // Someone else plays: model is the Options menu's Model row, offline Claude or the visitor's model.
    function watch(model) {
      intro.hidden = end.hidden = true;
      if (document.pointerLockElement) document.exitPointerLock();
      asking.stop();
      playEvents.length = 0;
      state.game = {...state.game, asked: false};
      const settings = AI.load(), ai = model !== AI.OFFLINE && AI.active(settings);
      if (ai) watchModel(settings, ai);
      else watchOffline();
    }

    const level = () => ({mission, records: monsters, said});
    const log = (kind, data) => playEvents.push({t: Date.now() / 1000, kind, ...data});

    function watchOffline() {
      state.game = {...state.game, mode: "replay", model: null};
      show("WATCHING CLAUDE PLAY", "Claude grabs the keycards, then clears the rooms. Its feed is on the right. Click the game to take over.");
      const replay = window.ConnectDoomReplay.play(level(), log);
      const mine = run = {name: "Claude", stop: handBack => replay.stop(handBack)};
      replay.done.then(() => { if (run === mine) { run = null; finish("watched", "Claude"); } });
    }

    // The visitor's model; when it can't play on (a bad key, a model refused), offline Claude takes over.
    function watchModel(settings, ai) {
      const name = modelName(ai.model);
      trackEvent("Watch own AI", {provider: ai.provider});
      state.game = {...state.game, mode: "claude", model: ai.model};
      show(`WATCHING ${name} PLAY`, `${name} plays with the same tools and moves every model gets. Its feed is on the right. Click the game to take over.`);
      const game = AI.play(settings, level(), log,
        why => show("CONNECT AI IS OUT", `${why} ${name} plays with the Doom tools only.`, 10));
      const mine = run = {name, stop: handBack => game.stop(handBack)};
      game.done.then(() => {
        if (run !== mine) return;
        run = null;
        if (!alive().length) { finish("watched", name); return; }
        // it stopped short (its moves ran out, or it called it a day): the player's turn
        playYourself();
        show(`${name} IS DONE`, `${name} cleared ${kills.length} of ${monsters.length} records. Your turn: finish the level.`, 10);
      }, e => {
        if (run !== mine) return;
        trackEvent("Own AI failed", {provider: ai.provider});
        log("failed", {text: e.message});
        run = null;
        watchOffline();
        show(`${name} COULDN'T PLAY`, `${e.message} Offline Claude takes over.`, 12);
      });
    }

    // The end: what just happened, in a sentence or two, and where to go next. who: who played, when watched.
    function finish(how, who = "Claude") {
      if (ended) return;
      ended = true;
      trackEvent("Level clear", {how});
      tip.hidden = true;
      if (document.pointerLockElement) document.exitPointerLock();
      const n = kills.length;
      end.innerHTML = `<div class="card">
        <div class="eyebrow">DOOMCP · LEVEL CLEAR · ${n}/${monsters.length} RECORDS</div>
        <h3 id="coach-end-title">${how === "watched" ? esc(who) : "You"} just read ${n} records from ${appList}.</h3>
        <p>All through one MCP server: Connect AI. It let ${how === "watched" ? esc(who) : "Claude"} read the apps it was allowed into${denied
          ? ", and blocked the one request it shouldn't answer: a customer's Social Security number" : ""}.
          That's what Connect AI does for any AI agent and your business data.</p>
        <div class="row">${external(AI.SIGNUP_URL, "TRY CONNECT AI FREE", "btn")}
          ${how === "watched" ? '<button class="btn alt" type="button" data-go="again">PLAY IT YOURSELF</button>'
                              : '<button class="btn alt" type="button" data-go="again">PLAY AGAIN</button>'}
          <a class="btn alt" href="about.html">HOW IT WORKS</a>
          ${external(REPO, "GITHUB", "btn alt")}</div></div>`;
      end.hidden = false;
      end.querySelector(".btn").focus();
    }

    intro.addEventListener("click", e => {
      const go = e.target.closest("[data-go]")?.dataset.go;
      if (go === "play") { trackEvent("Play"); playYourself(); }
      // the game isn't running yet, so the Model row's pick is the settings' (menuModels)
      if (go === "watch") { trackEvent("Watch Claude"); watch(menuModels()[1]); }
      if (go === "settings") openSettings();
    });
    end.addEventListener("click", e => {
      if (e.target.closest(`a[href="${AI.SIGNUP_URL}"]`)) trackEvent("Try Connect AI");
      if (e.target.closest("[data-go=again]")) location.reload();
    });
    // During Claude's run, a click on the game takes the marine over where it stands.
    canvas.addEventListener("click", () => {
      if (run) { trackEvent("Take over from Claude"); playYourself(); return; }
      canvas.focus();
      lockPointer(canvas);
    });

    // The Options menu's switches, in the game (Esc > Options) or the AI settings: Claude plays on
    // starts whoever the Model row names, off hands the marine back, and a new model takes over.
    let options = null;
    function followOptions(now) {
      const was = options;
      options = now;
      if (!was || !now || ended || !intro.hidden) return;
      if (now.claude_plays && !was.claude_plays && !run) watch(now.model);
      else if (!now.claude_plays && was.claude_plays && run) playYourself();
      else if (now.claude_plays && run && now.model !== was.model) { run.stop(false); run = null; watch(now.model); }
    }

    setInterval(() => {
      if (hideAt && Date.now() > hideAt) { tip.hidden = true; hideAt = 0; }
      followOptions(gameState()?.options);
      const keys = window.Module?._web_keys ? Module._web_keys() : 0;
      if (step === 1 && !run && (keys & need) === need) {
        step = 2;
        trackEvent("Got keys");
        show("STEP 2 OF 3 · CLEAR THE ROOMS", "Access granted. Each room is an app, and each monster is a record from it. Shoot one to read it in the panel.", 9);
      }
    }, 300);
    let endTimer = 0;
    events.addEventListener("cleared", () => {
      if (run) return;                                   // Claude's run goes on to the red door itself
      trackEvent("Rooms cleared");
      if (!denied) { finish("played"); return; }
      step = 3;
      show("STEP 3 OF 3 · TRY THE RED DOOR", "Every record is cleared. The red door guards data Connect AI blocked. There is no key.", 12);
      endTimer = setTimeout(() => finish("played"), 30000);  // nobody tried the door: wrap up anyway
    });
    events.addEventListener("vault", () => {
      if (run || alive().length) return;
      clearTimeout(endTimer);
      trackEvent("Red door");
      show("CONNECT AI SAID NO", "Claude asked for a customer's Social Security number. Connect AI blocked it, so the data never reached Claude.", 6);
      setTimeout(() => finish("played"), 5000);
    });

    const mb = n => (n / 1e6).toFixed(1);
    return {
      loading(loaded, total) {
        intro.querySelector("[data-go=play]").textContent = total ? `LOADING ${mb(loaded)} / ${mb(total)} MB` : `LOADING ${mb(loaded)} MB`;
      },
      ready() {
        const [play, watchBtn] = intro.querySelectorAll("[data-go]");
        play.textContent = "PLAY";
        play.disabled = watchBtn.disabled = false;
        play.focus();
      },
      // who plays instead of the player right now, by name, or null
      playing: () => run?.name || null,
      // the Watch button names the model the Options menu's Model row picks
      settingsChanged() {
        const ai = AI.active(AI.load());
        intro.querySelector("[data-go=watch]").textContent = `WATCH ${ai ? modelName(ai.model) : "CLAUDE"} PLAY`;
      },
    };
  }

  // ---------- the AI settings: the visitor's own model and Connect AI, and the game's switches ----------
  // Like the Options menu, every change takes at once; the settings stay in this browser (ai.js).
  let settingsDialog = null;
  function openSettings() {
    if (document.pointerLockElement) document.exitPointerLock();
    if (!settingsDialog) settingsDialog = makeSettings();
    settingsDialog.render();
    settingsDialog.el.showModal();
  }

  function makeSettings() {
    const style = document.createElement("style");
    style.textContent = `
      #ai-settings { width: min(640px, calc(100vw - 32px)); max-height: calc(100vh - 32px); padding: 0; color: var(--clarity);
                     background: var(--depth); border: 2px solid var(--agility); border-radius: 4px; }
      #ai-settings [hidden] { display: none !important; }
      #ai-settings::backdrop { background: rgba(13, 13, 18, .8); }
      #ai-settings .wrap { padding: 22px 24px; display: flex; flex-direction: column; gap: 18px; }
      #ai-settings .head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
      #ai-settings h2 { margin: 0; font: 12px/1.4 var(--pixel); color: var(--agility); letter-spacing: 1px; }
      #ai-settings h3 { margin: 0 0 4px; font: 9px/1.6 var(--pixel); color: var(--clarity); letter-spacing: 1px; }
      #ai-settings p, #ai-settings label { margin: 0; font: 14px/1.45 var(--sans); color: var(--balance); }
      #ai-settings section { display: flex; flex-direction: column; gap: 10px; padding-top: 16px; border-top: 1px solid var(--line); }
      #ai-settings .status { font: 15px/1.45 var(--sans); color: #E5E2DA; border-left: 3px solid var(--agility); padding-left: 12px; }
      #ai-settings .seg { display: flex; flex-wrap: wrap; gap: 6px; }
      #ai-settings .seg button, #ai-settings .act, #ai-settings .switch { font: 9px/1 var(--pixel); letter-spacing: 1px; cursor: pointer;
        padding: 11px 12px; border-radius: 3px; border: 1px solid var(--line); background: var(--depth-3); color: var(--clarity); }
      #ai-settings .seg button[aria-pressed=true], #ai-settings .switch[aria-pressed=true] { background: var(--agility); color: var(--depth); border-color: var(--agility); }
      #ai-settings .act.danger { color: var(--key-red); background: none; }
      #ai-settings button:focus-visible, #ai-settings input:focus-visible, #ai-settings select:focus-visible { outline: 3px solid var(--clarity); outline-offset: 2px; }
      #ai-settings .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
      #ai-settings .field { display: grid; gap: 4px; flex: 1 1 200px; }
      #ai-settings input, #ai-settings select { width: 100%; font: 14px/1.3 var(--mono); color: var(--clarity); background: var(--depth-3);
        border: 1px solid var(--line); border-radius: 3px; padding: 10px; }
      #ai-settings .msg { font-size: 13px; min-height: 1.2em; }
      #ai-settings .msg.ok { color: var(--agility); }
      #ai-settings .msg.bad { color: var(--key-red); }
      #ai-settings a { color: var(--agility); }
      #ai-settings .game { display: grid; grid-template-columns: auto 1fr; gap: 10px 14px; align-items: center; }
      #ai-settings .fine { font-size: 12px; }
      #ai-settings .setup { margin: 0; padding-left: 20px; display: grid; gap: 4px; font: 14px/1.45 var(--sans); color: #E5E2DA; }`;
    document.head.appendChild(style);

    const el = document.createElement("dialog");
    el.id = "ai-settings";
    el.setAttribute("aria-labelledby", "ai-settings-title");
    const providers = Object.entries(AI.PROVIDERS);
    el.innerHTML = `<div class="wrap">
      <div class="head"><h2 id="ai-settings-title">AI SETTINGS</h2><button class="act" type="button" data-close>CLOSE</button></div>
      <p class="status" id="ai-status" role="status"></p>
      <section>
        <h3>YOUR AI MODEL</h3>
        <p>Offline, Claude's recorded run plays: no keys needed. Bring your own key and your model plays instead.</p>
        <div class="seg" role="group" aria-label="Who plays">
          <button type="button" data-provider="offline">OFFLINE</button>
          ${providers.map(([id, p]) => `<button type="button" data-provider="${id}">${p.name.toUpperCase()}</button>`).join("")}
        </div>
        <div id="ai-provider" class="row">
          <label class="field">API key<input type="password" id="ai-key" autocomplete="off" spellcheck="false"></label>
          <label class="field">Model<input id="ai-model" list="ai-models" autocomplete="off" spellcheck="false"></label>
          <datalist id="ai-models"></datalist>
          <button class="act" type="button" id="ai-check">CHECK KEY</button>
          <a id="ai-get-key" target="_blank" rel="noopener noreferrer">Get a key</a>
        </div>
        <p class="msg" id="ai-msg" aria-live="polite"></p>
        <p class="fine">Connect AI's LLM Gateway isn't a choice here yet: it doesn't take requests from web pages.
          The Mac version plays through it (${external(REPO + "#readme", "README")}).</p>
      </section>
      <section>
        <h3>CONNECT AI (OPTIONAL)</h3>
        <p>Your model reads your own business data through the Connect AI MCP server: it can look records up while it
          plays, and answer the panel's questions. Read-only: nothing is written.</p>
        <ol class="setup">
          <li>${external(AI.SIGNUP_URL, "Sign up for Connect AI")}: pick Sign up under the sign-in box. It's a free trial.</li>
          <li>Add a data source, or use the sample data, as the ${external(AI.QUICKSTART_URL, "Quick Start")} shows.</li>
          <li>Make a ${external(AI.PAT_URL, "personal access token")} and paste it below, with your sign-in email.</li>
        </ol>
        <div class="row">
          <label class="field">Connect AI email<input type="email" id="cai-user" autocomplete="off" spellcheck="false"></label>
          <label class="field">Personal access token<input type="password" id="cai-pat" autocomplete="off" spellcheck="false"></label>
        </div>
        <div class="row">
          <label class="field">MCP server URL<input type="url" id="cai-url" autocomplete="off" spellcheck="false"
            placeholder="${AI.MCP_URL}"></label>
          <button class="act" type="button" id="cai-check">CHECK</button>
        </div>
        <p class="fine">Leave the URL empty for Connect AI's MCP server, or paste the Agent URL of one of your
          MCP Gateway tool servers.</p>
        <p class="msg" id="cai-msg" aria-live="polite"></p>
      </section>
      <section>
        <h3>IN THE GAME (ALSO ESC &gt; OPTIONS)</h3>
        <div class="game">
          <span>Claude plays</span><div><button class="switch" type="button" data-option="0"></button></div>
          <span>Model</span><select id="game-model" aria-label="Who plays when Claude plays is on"></select>
          <span>God mode</span><div><button class="switch" type="button" data-option="2"></button></div>
        </div>
      </section>
      <section>
        <p class="fine">Your keys stay in this browser (localStorage) and go straight to the provider you pick and to
          Connect AI. This site has no server, and nothing passes through it. Each run uses your own account's credits.</p>
        <div class="row"><button class="act danger" type="button" id="ai-forget">FORGET MY KEYS</button></div>
      </section></div>`;
    document.body.appendChild(el);
    const $ = id => el.querySelector(`#${id}`);
    const note = (id, text, cls = "") => { $(id).textContent = text; $(id).className = `msg ${cls}`; };
    const listed = {};   // the models each provider's key may use, once checked

    function update(change) {
      const settings = AI.load();
      change(settings);
      try { AI.save(settings); }
      catch (e) { note("ai-msg", "This browser won't keep settings (private window?): they last until you close the page.", "bad"); }
      settingsChanged();
      render();
    }

    function render() {
      const settings = AI.load(), ai = AI.active(settings), provider = settings.provider, p = AI.PROVIDERS[provider];
      el.querySelectorAll("[data-provider]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.provider === provider)));
      $("ai-provider").hidden = !p;
      if (p) {
        if (document.activeElement !== $("ai-key")) $("ai-key").value = settings.keys[provider] || "";
        $("ai-key").placeholder = p.placeholder;
        if (document.activeElement !== $("ai-model")) $("ai-model").value = settings.models[provider] || p.defaultModel;
        $("ai-get-key").href = p.keyUrl;
        $("ai-get-key").textContent = `Get ${article(p.company)} ${p.company} key`;
        $("ai-models").innerHTML = (listed[provider] || []).map(m => `<option value="${esc(m)}">`).join("");
      }
      if (document.activeElement !== $("cai-user")) $("cai-user").value = settings.connectAI.user;
      if (document.activeElement !== $("cai-pat")) $("cai-pat").value = settings.connectAI.pat;
      if (document.activeElement !== $("cai-url")) $("cai-url").value = settings.connectAI.url || "";
      const who = ai ? `${modelName(ai.model)} (${AI.PROVIDERS[ai.provider].company}) plays when you watch`
        : p ? `Add ${article(p.company)} ${p.company} key and your model plays. Until then, Claude's recorded run plays`
        : "Offline: Claude's recorded run plays";
      const data = AI.hasConnectAI(settings) ? (ai ? ", reads your data through Connect AI, and answers your questions."
                                                   : ". Your Connect AI token is set: add an AI key to use it.")
        : ". Add a Connect AI token to let it read your own data.";
      $("ai-status").textContent = who + data;
      renderGame();
      renderAILink();
    }

    // the game's switches as the game has them: the Options menu changes them too
    function renderGame() {
      const options = gameState()?.options, [models, pick] = menuModels();
      el.querySelectorAll("[data-option]").forEach(b => {
        const on = !!options?.[b.dataset.option === "0" ? "claude_plays" : "god_mode"];
        b.setAttribute("aria-pressed", String(on));
        b.textContent = on ? "ON" : "OFF";
        b.disabled = !options;
      });
      const select = $("game-model"), names = models.split(",");
      if (select.dataset.models !== models) {
        select.innerHTML = names.map((m, i) => `<option value="${i}">${m === AI.OFFLINE ? "Offline Claude" : esc(modelName(m))}</option>`).join("");
        select.dataset.models = models;
      }
      if (document.activeElement !== select) select.value = String(Math.max(0, names.indexOf(options?.model || pick)));
      select.disabled = !options;
    }

    el.addEventListener("click", e => {
      if (e.target === el || e.target.closest("[data-close]")) { el.close(); return; }
      const provider = e.target.closest("[data-provider]")?.dataset.provider;
      if (provider) { trackEvent("AI provider", {provider}); update(s => { s.provider = provider; }); note("ai-msg", ""); }
      const option = e.target.closest("[data-option]")?.dataset.option;
      if (option) {
        const on = e.target.getAttribute("aria-pressed") !== "true";
        if (option === "0" && on && !coach) return;
        window.ConnectDoomReplay.send(`OPTION ${option} ${on ? 1 : 0}`);
        if (option === "0" && on) el.close();   // to watch
        setTimeout(renderGame, 300);
      }
    });
    $("game-model").addEventListener("change", e => {
      window.ConnectDoomReplay.send(`MODEL ${e.target.value}`);
      setTimeout(renderGame, 300);
    });
    $("ai-key").addEventListener("change", e => {
      const key = e.target.value.trim();
      update(s => { if (key) s.keys[s.provider] = key; else delete s.keys[s.provider]; });
      note("ai-msg", key ? "Saved. Check it to see the models it can use." : "Key removed.");
    });
    $("ai-model").addEventListener("change", e => {
      const model = e.target.value.trim();
      update(s => { if (model) s.models[s.provider] = model; else delete s.models[s.provider]; });
    });
    $("ai-check").addEventListener("click", async () => {
      const settings = AI.load(), provider = settings.provider, key = settings.keys[provider];
      if (!key) { note("ai-msg", "Paste a key first.", "bad"); return; }
      note("ai-msg", "Checking…");
      try {
        listed[provider] = await AI.listModels(provider, key);
        const model = settings.models[provider] || AI.PROVIDERS[provider].defaultModel;
        const known = listed[provider].includes(model);
        note("ai-msg", `The key works: ${listed[provider].length} models.${known ? "" : ` ${model} isn't one of them: pick one from the Model list.`}`,
             known ? "ok" : "bad");
        trackEvent("AI key checked", {provider, ok: "yes"});
        render();
      } catch (e) {
        trackEvent("AI key checked", {provider, ok: "no"});
        note("ai-msg", e.message, "bad");
      }
    });
    const saveConnectAI = () => {
      const url = $("cai-url").value.trim();
      if (url && !/^https:\/\/[^\s/]+/.test(url)) { note("cai-msg", "The MCP server URL starts with https://", "bad"); return false; }
      update(s => { s.connectAI = {user: $("cai-user").value.trim(), pat: $("cai-pat").value.trim(), url}; });
      return true;
    };
    ["cai-user", "cai-pat", "cai-url"].forEach(id => $(id).addEventListener("change", saveConnectAI));
    $("cai-check").addEventListener("click", async () => {
      if (!saveConnectAI()) return;
      const settings = AI.load();
      if (!AI.hasConnectAI(settings)) { note("cai-msg", "Add your Connect AI email and token first.", "bad"); return; }
      note("cai-msg", "Checking…");
      try {
        const {tools, connections} = await AI.checkConnectAI(settings);
        note("cai-msg", `Connected: ${tools} read-only tools${connections.length ? `, ${connectionList(connections)}` : ""}.`, "ok");
        trackEvent("Connect AI checked", {ok: "yes"});
      } catch (e) {
        trackEvent("Connect AI checked", {ok: "no"});
        note("cai-msg", e.message, "bad");
      }
    });
    $("ai-forget").addEventListener("click", () => {
      AI.clear();
      for (const k in listed) delete listed[k];
      settingsChanged();
      render();
      note("ai-msg", "Forgotten: back to offline.", "ok");
      note("cai-msg", "");
    });
    // while it's open, the switches follow the game (the Options menu, or a run ending)
    setInterval(() => { if (el.open) renderGame(); }, 500);
    return {el, render};
  }

  // The top bar's button: what it opens, and who plays now
  const GEAR = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="currentColor"><path d="M19.4 13a7.5 7.5 0 0 0 0-2l2.1-1.6-2-3.5-2.5 1a7.4 7.4 0 0 0-1.7-1L15 3.3h-4l-.4 2.6a7.4 7.4 0 0 0-1.7 1l-2.5-1-2 3.5L6.6 11a7.5 7.5 0 0 0 0 2l-2.1 1.6 2 3.5 2.5-1a7.4 7.4 0 0 0 1.7 1l.4 2.6h4l.4-2.6a7.4 7.4 0 0 0 1.7-1l2.5 1 2-3.5zM12 15.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7z"/></svg>';
  function renderAILink() {
    const link = document.getElementById("ai-link"), ai = AI.active(AI.load());
    if (link) link.innerHTML = `${GEAR}AI SETTINGS <span class="who">· ${esc(ai ? modelName(ai.model) : "OFFLINE")}</span>`;
  }

  // ---------- links, and desktops only ----------
  function addLinks() {
    const style = document.createElement("style");
    style.textContent = `
      /* their own line above the lockup and the chip: beside them, the bar runs out of room */
      .weblinks { position: absolute; top: 10px; right: 32px; display: flex; gap: 18px; align-items: center; }
      #ai-link { display: flex; align-items: center; gap: 7px; font: 9px/1 var(--pixel); letter-spacing: 1px; cursor: pointer;
                 color: var(--depth); background: var(--agility); border: 0; border-radius: 3px; padding: 7px 10px; }
      #ai-link svg { width: 12px; height: 12px; }
      #ai-link .who { opacity: .75; }
      #ai-link:hover, #ai-link:focus-visible { filter: brightness(1.1); outline: 2px solid var(--clarity); outline-offset: 2px; }
      .weblinks a { font: 9px/1 var(--pixel); color: var(--balance); text-decoration: none; letter-spacing: 1px; }
      .weblinks a:hover, .weblinks a:focus-visible { color: var(--agility); }
      #desktop-only { position: fixed; inset: 0; z-index: 10; display: flex; align-items: center; justify-content: center;
                      padding: 24px 16px; background: var(--depth); text-align: center; }
      #desktop-only[hidden] { display: none; }
      #desktop-only .box { max-width: 460px; display: flex; flex-direction: column; gap: 16px; align-items: center; }
      #desktop-only .doom { font: 22px/1 var(--pixel); color: var(--doom-red); letter-spacing: 2px; }
      #desktop-only p { margin: 0; font-size: 16px; line-height: 1.5; color: var(--clarity); }
      #desktop-only .links { display: flex; gap: 12px; flex-wrap: wrap; justify-content: center; }
      #desktop-only .links a { font: 10px/1 var(--pixel); color: var(--depth); background: var(--agility);
                               padding: 12px 14px; border-radius: 3px; text-decoration: none; }
      #desktop-only .links a + a { background: var(--depth-3); color: var(--clarity); }`;
    document.head.appendChild(style);
    const nav = document.createElement("nav");
    nav.className = "weblinks";
    nav.setAttribute("aria-label", "About this project");
    nav.innerHTML = `<button type="button" id="ai-link"></button><a href="about.html">ABOUT</a>${external(CONNECT_AI, "CONNECT AI")}${external(REPO, "GITHUB")}`;
    nav.querySelector("#ai-link").addEventListener("click", openSettings);
    const chip = document.getElementById("stage-mode");
    chip.parentNode.insertBefore(nav, chip);
    renderAILink();
    // the panel's own links leave the game too: a new tab keeps it running
    document.querySelectorAll('a[href^="http"]').forEach(a => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
    const gate = document.createElement("div");
    gate.id = "desktop-only";
    gate.hidden = true;
    gate.innerHTML = `<div class="box"><img src="brand/connect-ai.svg" alt="Connect AI" height="32">
      <div class="doom">DOOM</div>
      <p id="desktop-why"></p>
      <div class="links"><a href="about.html">WHAT IS THIS?</a>${external(REPO, "GITHUB")}</div></div>`;
    document.body.appendChild(gate);
    return gate;
  }

  // Doom needs a keyboard and a mouse, and room for the stage and the panel side by side.
  const hasMouse = () => matchMedia("(any-pointer: fine)").matches;
  const roomy = () => innerWidth >= 900 && innerHeight >= 560;

  addEventListener("DOMContentLoaded", () => {
    const gate = addLinks();
    let started = false;
    const check = () => {
      if (started) return;
      if (hasMouse() && roomy()) {
        gate.hidden = true;
        started = true;
        start().catch(e => { console.error("connect-doom web:", e); trackEvent("Load failed", {error: String(e).slice(0, 200)}); });
        return;
      }
      if (gate.hidden) trackEvent("Desktop only", {reason: hasMouse() ? "small window" : "no mouse"});
      gate.hidden = false;
      document.getElementById("desktop-why").textContent = hasMouse()
        ? "Connect AI × DOOM needs a bigger window. Make this one wider to play."
        : "Connect AI × DOOM plays on a desktop browser, with a keyboard and a mouse. Open this page on your computer to play.";
    };
    check();
    addEventListener("resize", check);
  });
})();
