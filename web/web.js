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
  // The header (the links and the logos) along the top, TOP tall; the game at 4:3 in the middle of
  // the window, in two thirds of it (leaving the panel PANEL_MIN at least) and GAP clear of the
  // header; the footer (the question, Claude's line and the fine print) just under the game, in
  // BOTTOM. When the window's short, the game shrinks to keep all three.
  const PANEL_MIN = 440, TOP = 90, GAP = 40, BOTTOM = 170;
  function layout() {
    const w = innerWidth, h = innerHeight, split = Math.round(Math.min(w * 2 / 3, w - PANEL_MIN));
    const gh = Math.max(200, Math.min(h - TOP - GAP - BOTTOM, (split - 64) * 3 / 4)), gw = gh * 4 / 3;
    const gy = Math.min(Math.max((h - gh) / 2, TOP + GAP), h - gh - BOTTOM);
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
            standing: alive().length, vault: vault.slice(-5), play_events: playEvents, ask: askState()};
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

  // ---------- the panel's question box: a level from the visitor's own data ----------
  // A question there builds a level, as the build dialog does (makeBuilder). The panel's state.ask:
  // null hides the box (nothing set up to build with), busy while a level builds, note under it.
  function askState() {
    if (!buildReady()) return null;
    const ai = AI.active(AI.load()), ask = {default: suggestQuestions(AI.connections())[0] || ""};   // "Use the default question"
    if (builder?.building()) return {...ask, busy: `${modelName(ai.model)} is building your level…`};
    return {...ask, note: `Enter builds a level from your data: ${modelName(ai.model)} picks the three apps that best ` +
                          `answer it, through Connect AI, with ${connectionList(AI.connections())}.`};
  }

  // The panel polls /state.json and posts its question box to /ask; nothing here talks to a server.
  const realFetch = window.fetch.bind(window);
  const json = (body, status = 200) => new Response(JSON.stringify(body), {status, headers: {"Content-Type": "application/json"}});
  window.fetch = (url, opts) => {
    const path = new URL(url, location.href).pathname;
    if (path.endsWith("/state.json")) return Promise.resolve(json(state ? currentState() : {}));
    if (path.endsWith("/ask") && opts?.method === "POST") {
      if (!buildReady()) return Promise.resolve(json({error: "Set up your AI and Connect AI in AI settings first."}, 409));
      openBuilder(String(JSON.parse(opts.body).question || ""), true);
      return Promise.resolve(json({note: "Building your level…"}));
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
  // ---------- suggested questions, from the visitor's own Connect AI connections ----------
  // Each kind of app gets a question it can answer; a CRM and a support app together get the
  // level's own kind of question first. The model finds the connections itself.
  // [kind, the apps it covers, best known first, question]
  const IDEAS = [
    ["crm", ["salesforce", "hubspot", "dynamics 365", "pipedrive", "zoho crm", "bullhorn"], a => `Which ${a} deals are slipping this quarter, and who owns them?`],
    ["support", ["zendesk", "servicenow", "freshdesk", "intercom", "help scout"], a => `Which ${a} tickets have been open longest, and for which customers?`],
    ["billing", ["stripe", "quickbooks", "netsuite", "xero", "sage intacct", "business central", "acumatica", "myob", "epicor", "chargebee", "zuora"],
     a => `Who owes us money in ${a}, and how overdue is it?`],
    ["projects", ["jira", "asana", "monday", "clickup", "smartsheet", "trello"], a => `Which ${a} issues are overdue or blocked, and who has them?`],
    ["store", ["shopify", "woocommerce", "bigcommerce", "magento"], a => `What were ${a}'s best-selling products last month?`],
    ["people", ["workday", "bamboohr", "adp", "gusto", "rippling"], a => `How has headcount changed by department in ${a} this year?`],
    ["ads", ["google ads", "facebook ads", "linkedin ads", "google analytics", "youtube analytics"],
     a => `Which ${a} campaigns cost the most for what they brought in last month?`],
    ["data", ["snowflake", "bigquery", "databricks", "redshift", "postgresql", "sql server", "mysql", "oracle", "sap hana"],
     a => `What's in our ${a} data? Name the main tables and what they hold.`],
    ["chat", ["slack", "microsoft teams", "gmail"], a => `What were the busiest ${a} conversations this week, and what were they about?`],
  ];

  // Up to three questions for these connections ([{name, source}]), most useful first
  function suggestQuestions(list) {
    // each app's kind: the longest name in it (Dynamics 365 Business Central is billing, not CRM)
    const kindOf = source => {
      const s = source.toLowerCase();
      let best = null;
      for (const [kind, apps] of IDEAS)
        apps.forEach((app, rank) => { if (s.includes(app) && app.length > (best?.length ?? 0)) best = {kind, rank, length: app.length}; });
      return best;
    };
    const found = new Map();   // kind -> its best-known app among these connections
    for (const source of new Set(list.map(c => c.source))) {
      const k = kindOf(source);
      if (k && (!found.has(k.kind) || k.rank < found.get(k.kind).rank)) found.set(k.kind, {source, rank: k.rank});
    }
    for (const [kind, {source}] of found) found.set(kind, source);
    const questions = [];
    if (found.has("crm") && found.has("support"))
      questions.push(`Which ${found.get("crm")} customers are at risk right now, with open ${found.get("support")} tickets?`);
    for (const [kind, , ask] of IDEAS) if (found.has(kind)) questions.push(ask(found.get(kind)));
    if (!questions.length && list.length) questions.push(`What's in ${list[0].source}? Summarize what it holds.`);
    return questions.slice(0, 3);
  }

  // Under the question box, once the visitor's model and Connect AI are set up and checked
  function showIdeas() {
    const box = document.getElementById("newq-ideas"), settings = AI.load(), list = AI.connections();
    if (!box) return;
    const questions = AI.active(settings) && AI.hasConnectAI(settings) && list ? suggestQuestions(list) : [];
    box.hidden = !questions.length;
    box.innerHTML = questions.length ? '<span class="lbl">TRY ONE OF THESE</span>' +
      questions.map(q => `<button type="button" data-idea="${esc(q)}">${esc(q)}</button>`).join("") : "";
  }

  // A model's name as the page shows it: GPT-5.6-SOL, CLAUDE-SONNET-5-5 (OpenRouter's maker/ dropped)
  const modelName = id => String(id).replace(/^[^/]*\//, "").toUpperCase();
  let coach = null;

  // ---------- the visitor's own level, kept in this browser ----------
  // ai.js builds it from their data; IndexedDB keeps it (its WADs are too big for localStorage), and the
  // page plays it in place of the site's level until they go back to the demo.
  const myLevel = {
    async store(mode, use) {
      const db = await new Promise((resolve, reject) => {
        const open = indexedDB.open("connect-doom", 1);
        open.onupgradeneeded = () => open.result.createObjectStore("level");
        open.onsuccess = () => resolve(open.result);
        open.onerror = () => reject(open.error);
      });
      return new Promise((resolve, reject) => {
        const t = db.transaction("level", mode), request = use(t.objectStore("level"));
        t.oncomplete = () => resolve(request.result);
        t.onerror = () => reject(t.error);
      });
    },
    load() { return this.store("readonly", s => s.get("mine")); },
    save(level) { return this.store("readwrite", s => s.put(level, "mine")); },
    clear() { return this.store("readwrite", s => s.delete("mine")); },
  };
  let mine = null;      // the level this page plays when it's the visitor's: {files, question, model}
  let gameWad = null;   // Freedoom's WAD, once downloaded: the level builder takes its palette and textures

  // ---------- the game ----------
  const files = {"/doom/level.wad": "level.wad", "/doom/brand.wad": "brand.wad",
                 "/doom/level.deh": "level.deh", "/doom/labels.tsv": "labels.tsv"};
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
    // Emscripten's SDL takes every key on the page (on document), so Tab never left a field and letters
    // typed in the question box reached the game: keys for the page's fields and dialogs stop at body
    for (const type of ["keydown", "keyup", "keypress"])
      document.body.addEventListener(type, e => {
        if (document.pointerLockElement !== canvas && e.target.closest?.("input, textarea, select, button, a, dialog"))
          e.stopPropagation();
      });

    // The level first (small), so the start card is up while Freedoom downloads: the visitor's own,
    // if they built one, else the site's.
    mine = await myLevel.load().catch(e => { console.warn("Your level:", e); return null; }) || null;
    document.body.classList.toggle("mine", !!mine);   // the visitor's level: no leaderboard (see plainWords)
    const level = mine ? name => Promise.resolve(new Response(mine.files[name])) : name => get(`level/${name}`);
    const [baked, cache, saidJson, monsterKinds, ...bufs] = await Promise.all([
      level("state.json").then(r => r.json()), level("cache.json").then(r => r.json()),
      mine ? {} : level("said.json").then(r => r.json()),   // Claude's lines from its run of the site's level
      level("monsters.txt").then(r => r.text()),
      ...Object.values(files).map(name => level(name).then(r => r.arrayBuffer()))]);
    monsters = cache.monsters;
    said = saidJson;
    state = {...baked, game: {mode: "player", started: Date.now() / 1000}};
    plainWords();
    coach = makeCoach(canvas);
    coach.settingsChanged();
    // the visitor's connections, for building a level from them (setupSteps says when Connect AI won't answer)
    const saved = AI.load();
    if (AI.hasConnectAI(saved)) AI.checkConnectAI(saved).then(() => { connectAIError = null; settingsChanged(); },
      e => { connectAIError = e.message; settingsChanged(); });
    const wad = gameWad = await downloadWad(coach.loading);

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
        const [models] = menuModels();   // offline Claude picked: it's who plays first (ready())
        Object.assign(ENV, {PSDOOMPSCMD: "list", PSDOOMKILLCMD: "kill", PSDOOMRENICECMD: "hit",
          PSDOOMVAULTCMD: "vault", PSDOOM_LABELS: "/doom/labels.tsv", PSDOOM_MONSTERS: monsterKinds.trim(),
          PSDOOM_AGENT_DIR: "/doom/agent", HOME: "/doom", PSDOOM_AGENT_MODELS: models, PSDOOM_AGENT_MODEL: AI.OFFLINE});
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

  // The AI settings changed: the game's Model row lists the new model, but keeps whoever plays now
  // (offline Claude when they're gone): a key added mid-game doesn't start a run on its credits.
  function settingsChanged() {
    const [models] = menuModels(), now = gameState()?.options?.model;
    const pick = models.split(",").includes(now) ? now : AI.OFFLINE;
    if (window.Module?._web_set_models) {
      const list = Module.stringToNewUTF8(models), first = Module.stringToNewUTF8(pick);
      Module._web_set_models(list, first);
      Module._free(list);
      Module._free(first);
    }
    coach?.settingsChanged();
    showIdeas();
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

  // The briefing's labels name whoever built the level: Claude for the site's, the visitor's model for theirs
  function nameBuilder(model) {
    const who = /claude/i.test(model) ? "CLAUDE" : /gpt|openai/i.test(model) ? "CHATGPT" : modelName(model);
    const set = (el, text) => { if (el) el.textContent = text; };
    set(document.getElementById("tab-brief"), `HOW ${who} BUILT IT`);
    set(document.querySelector("#feed")?.parentElement.querySelector("h2"), `DOOMCP · EVERY CALL ${who} MADE TO CONNECT AI`);
    set(document.querySelector("#answer")?.parentElement.querySelector("h2"), `${who}'S ANSWER`);
  }

  // The panel speaks to people running a booth; a visitor here needs plainer words.
  function plainWords() {
    const relabel = (sel, text) => { const el = document.querySelector(sel); if (el) el.textContent = text; };
    relabel("#s-model + .lbl", "BUILT BY");
    relabel("#stage .bar.bottom .line .tag", "THE QUESTION");
    nameBuilder(mine ? mine.model : "claude");
    const fine = document.querySelector("#stage .bar.bottom .fine");
    // the page's footer: what Connect AI is, the code, and that this is a fan project
    if (fine) fine.innerHTML = "Every monster is a record Claude read through " + external(CONNECT_AI, "CData Connect AI") +
      ", one MCP server between AI agents and your data. " + external(AI.SIGNUP_URL, "Try Connect AI free") + " · " +
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
    relabel("#newq h2", "BUILD A LEVEL FROM YOUR DATA");
    relabel("#newq-go", "BUILD MY LEVEL");
    // the question box needs the visitor's own model: the way to set it up, right there
    const askRow = document.querySelector("#newq .row");
    if (askRow) {
      askRow.insertAdjacentHTML("beforeend", '<button type="button" class="link" id="newq-settings">AI settings</button>');
      document.getElementById("newq-settings").addEventListener("click", openSettings);
      askRow.insertAdjacentHTML("afterend", '<div class="ideas" id="newq-ideas" hidden></div>');
      document.getElementById("newq-ideas").addEventListener("click", e => {
        const idea = e.target.closest("[data-idea]");
        const box = document.getElementById("newq-text");
        if (!idea || box.disabled) return;
        trackEvent("Suggested question");
        box.value = idea.dataset.idea;
        box.focus();
      });
    }
    const style = document.createElement("style");
    style.textContent = `/* the visitor's own level: no model has a run on it to rank */
      body.mine #tabs [data-tab=board] { display: none; }
      /* the header along the top, the footer just under the game (layout()) */
      #stage .bar.top { top: 0; height: 90px; container-type: inline-size; align-items: flex-end; padding-bottom: 12px; }
      #stage .bar.bottom { justify-content: flex-start; padding-top: 16px; }
      /* a narrow stage: smaller logos, so the chip beside them fits, and only the links that fit on one line
         (the footer links Connect AI too) */
      #stage .weblinks > * { white-space: nowrap; }
      @container (max-width: 720px) {
        #stage .weblinks { gap: 12px; }
        #ai-link .who, #stage .weblinks a[href="${CONNECT_AI}"] { display: none; }
      }
      @container (max-width: 600px) {
        #stage-mode { display: none; }   /* the panel says the same, at the top of its feed */
        .lockup { gap: 10px; }
        .lockup .cdata { height: 15px; } .lockup .cailogo { height: 22px; } .lockup .rule { height: 24px; }
        .lockup .x, .lockup .doom { font-size: 15px; }
      }
      /* the booth's loading screen: here the start card is, but for building a visitor's level (body.building) */
      body:not(.building) #stage-screen .splash { display: none; }
      body.building #canvas, body.building .coach-card, body.building #coach-tip, body.building #newq { visibility: hidden; }
      /* a narrow, tall panel: the rooms one under another, each with its count beside its name (short
         windows keep them side by side, or the game's events lose their room) */
      section:has(> #rooms-live) { container-type: inline-size; }
      @media (min-height: 860px) { @container (max-width: 440px) {
        body.stage #rooms-live { grid-template-columns: minmax(0, 1fr); }
        #rooms-live .rl { grid-template-columns: minmax(0, 1fr) auto; column-gap: 12px; row-gap: 4px; align-items: center; }
        #rooms-live .rl > * { grid-column: 1 / -1; }
        #rooms-live .rl > .top, #rooms-live .rl > .app { grid-column: 1; }
        #rooms-live .rl > .num { grid-column: 2; grid-row: 1 / span 2; font-size: 24px; text-align: right; }
        #rooms-live .rl .num small { font-size: 14px; }
      } }
      /* the question box and its ideas take room: the game's events keep enough to read the latest */
      body.stage.in-game main > section.grow:has(#events) { min-height: 96px; }
      body.stage.in-game main > #newq:not([hidden]) { flex: 0 1 auto; max-height: 30vh; overflow-y: auto; }   /* it scrolls */
      body.stage.in-game main > #play:not([hidden]) { min-height: 180px; }   /* whoever plays keeps their feed in view */
      #newq .ideas { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; }
      #newq .ideas[hidden] { display: none; }
      #newq .ideas .lbl { font: 8px/1.6 var(--pixel); color: var(--balance); letter-spacing: 1px; }
      #newq .ideas button { font: 13px/1.35 var(--sans); text-align: left; color: var(--clarity); background: var(--depth-3);
                            border: 1px solid var(--line); border-radius: 3px; padding: 6px 10px; cursor: pointer; }
      #newq .ideas button:hover, #newq .ideas button:focus-visible { border-color: var(--agility); color: var(--agility); }
      .board-lead { margin: 0 0 10px; font-size: 14px; line-height: 1.45; color: #E5E2DA; }
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
      .coach-card { position: absolute; inset: 0; z-index: 3; display: flex; align-items: safe center; justify-content: center; overflow-y: auto;
                    background: rgba(13, 13, 18, .88); padding: 24px; }
      .coach-card[hidden], #coach-tip[hidden] { display: none; }
      #coach-intro { background: rgba(13, 13, 18, .8); transition: opacity .8s ease-out; }   /* Claude plays behind it */
      #coach-intro.fading { opacity: 0; pointer-events: none; }
      #coach-intro:not([hidden]):not(.fading) ~ #coach-tip { visibility: hidden; }   /* the card says it all */
      @media (prefers-reduced-motion: reduce) { #coach-intro { transition: none; } }
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
      .coach-card { container-type: size; }
      @container (max-height: 600px) {
        .coach-card .card { gap: 10px; }
        .coach-card p.what, .coach-card .keys { display: none; }
        .coach-card h3 { font-size: 24px; } .coach-card p, .coach-card li { font-size: 14px; }
        .coach-card li { grid-template-columns: 26px 1fr; } .coach-card li::before { line-height: 26px; }
        .coach-card .btn { padding: 11px 14px; } .coach-card .byo { padding: 6px 10px; font-size: 13px; }
      }
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
      <p>${mine ? `${esc(modelName(mine.model))} read your ${appList} through the Connect AI MCP server and built this
        level for your question: “${esc(mine.question)}”` : `Claude read a demo company's ${appList} through the Connect AI MCP
        server and built this level from what it found.`}</p>
      <p class="what">${mine?.answer ? `${esc(modelName(mine.model))}'s answer: ${esc(mine.answer)}` : `CData Connect AI is one MCP
        server between AI agents like Claude and your apps. It applies your permissions and your data security rules to
        every request.`}</p>
      <ol>
        <li><span><b>Grab your keys.</b> Keycards are your permissions: Connect AI gives you one for each app you can open.</span></li>
        <li><span><b>Clear the rooms.</b> Each room is an app, and each monster is a record from it.</span></li>
        <li><span><b>Try the red door.</b> It guards data Connect AI blocked.</span></li>
      </ol>
      <div class="row"><button class="btn" type="button" data-go="play" disabled>LOADING…</button>
        <button class="btn alt" type="button" data-go="watch" disabled>WATCH CLAUDE PLAY</button></div>
      <div class="byo" id="coach-byo"></div>
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
    // keepIntro: the start card stays up over the game (the page's own start, see ready()).
    function watch(model, keepIntro = false) {
      if (!keepIntro) intro.hidden = true;
      end.hidden = true;
      if (run) { run.stop(false); run = null; }   // another player takes over where the marine stands
      if (document.pointerLockElement) document.exitPointerLock();
      playEvents.length = 0;
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
          ? `, and blocked the one request it shouldn't answer${mine ? "" : ": a customer's Social Security number"}` : ""}.
          That's what Connect AI does for any AI agent and your data.</p>
        <div class="row">${external(AI.SIGNUP_URL, "TRY CONNECT AI FREE", "btn")}
          ${how === "watched" ? '<button class="btn alt" type="button" data-go="again">PLAY IT YOURSELF</button>'
                              : '<button class="btn alt" type="button" data-go="again">PLAY AGAIN</button>'}
          <a class="btn alt" href="about.html">HOW IT WORKS</a>
          ${external(REPO, "GITHUB", "btn alt")}</div></div>`;
      end.hidden = false;
      end.querySelector(".btn").focus();
    }

    // The start card fades once its time is up, unless the pointer is on it (someone's reading)
    function fadeIntro() {
      if (intro.hidden || intro.classList.contains("fading")) return;
      if (intro.matches(":hover")) { setTimeout(fadeIntro, 3000); return; }
      intro.classList.add("fading");
      setTimeout(() => { intro.hidden = true; intro.classList.remove("fading"); }, 800);
    }

    intro.addEventListener("click", e => {
      if (e.target === intro) { fadeIntro(); return; }   // a click beside the card: back to the game
      const go = e.target.closest("[data-go]")?.dataset.go;
      if (go === "play") { trackEvent("Play"); playYourself(); }
      if (go === "watch") {
        // offline Claude is already playing: the card just goes; the visitor's own model takes over
        const model = menuModels()[1];
        trackEvent("Watch Claude", {who: model === AI.OFFLINE ? "offline" : "own model"});
        if (model === AI.OFFLINE && run) fadeIntro();
        else watch(model);
      }
      if (go === "settings") openSettings();
      if (go === "build") openBuilder();
      if (go === "demo") { trackEvent("Back to the demo level"); myLevel.clear().finally(() => location.reload()); }
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
      show("CONNECT AI SAID NO", mine ? "Connect AI blocked this request when your level was built, so the data never reached the model."
        : "Claude asked for a customer's Social Security number. Connect AI blocked it, so the data never reached Claude.", 6);
      setTimeout(() => finish("played"), 5000);
    });

    const mb = n => (n / 1e6).toFixed(1);
    return {
      loading(loaded, total) {
        intro.querySelector("[data-go=play]").textContent = total ? `LOADING ${mb(loaded)} / ${mb(total)} MB` : `LOADING ${mb(loaded)} MB`;
      },
      // The game's up: offline Claude starts playing behind the start card (never the visitor's own
      // model, which would spend their tokens on a page load), and the card fades after 10 s.
      ready() {
        const [play, watchBtn] = intro.querySelectorAll("[data-go]");
        play.textContent = "PLAY";
        play.disabled = watchBtn.disabled = false;
        play.focus();
        trackEvent("Auto watch");
        watch(AI.OFFLINE, true);
        this.settingsChanged();
        setTimeout(fadeIntro, 10000);
      },
      // who plays instead of the player right now, by name, or null
      playing: () => run?.name || null,
      // the Watch button names the model the Options menu's Model row picks
      settingsChanged() {
        const ai = AI.active(AI.load());
        // what the visitor can do next: set up their AI, build a level from their data, or go back to the demo
        intro.querySelector("#coach-byo").innerHTML = mine
          ? `<span><b>This is your level.</b> Build another from a new question, or go back to the site's.</span>
             <span class="row">${buildReady() ? '<button class="btn alt" type="button" data-go="build">NEW LEVEL</button>' : ""}
             <button class="btn alt" type="button" data-go="demo">DEMO LEVEL</button></span>`
          : buildReady()
          ? `<span><b>Your data is connected.</b> Build a level from it: ${esc(modelName(ai.model))} picks three apps
             that answer your question, and their records become the monsters.</span>
             <button class="btn" type="button" data-go="build">BUILD MY LEVEL</button>`
          : setupSteps().some(step => step.done)
          ? `<span><b>Almost there:</b> building a level from your data needs both.${checklist()}</span>
             <button class="btn alt" type="button" data-go="settings">AI SETTINGS</button>`
          : `<span><b>Bring your own AI.</b> Let your Claude, ChatGPT or OpenRouter model play, and build a level from your
             own data through Connect AI.</span><button class="btn alt" type="button" data-go="settings">AI SETTINGS</button>`;
        intro.querySelector("[data-go=watch]").textContent = ai ? `WATCH ${modelName(ai.model)} PLAY`
          : run ? "KEEP WATCHING" : "WATCH CLAUDE PLAY";
      },
    };
  }

  // ---------- building a level from the visitor's own data ----------
  // Their model plans it through Connect AI and the page builds it (ai.js); then the page starts
  // over on it. The model's Connect AI calls go to the panel's briefing tab as they happen.
  let builder = null;
  // go: build at once (the panel's question box), rather than show the question to edit first
  function openBuilder(question = "", go = false) {
    if (document.pointerLockElement) document.exitPointerLock();
    if (settingsDialog?.el.open) settingsDialog.el.close();
    builder ||= makeBuilder();
    builder.open(question);
    if (go) builder.build();
  }

  function makeBuilder() {
    styleDialogs();
    const el = document.createElement("dialog");
    el.id = "ai-build";
    el.className = "ai-dialog";
    el.setAttribute("aria-labelledby", "ai-build-title");
    el.innerHTML = `<div class="wrap">
      <div class="head"><h2 id="ai-build-title">BUILD A LEVEL FROM YOUR DATA</h2><button class="act" type="button" data-close>CLOSE</button></div>
      <p class="status" id="build-lead"></p>
      <div id="build-setup"><div id="build-checklist"></div>
        <div class="row"><button class="act go" type="button" data-settings>OPEN AI SETTINGS</button></div></div>
      <div id="build-form"><label>Your question<textarea id="build-q" rows="3" maxlength="500"></textarea></label>
        <div class="ideas" id="build-ideas"></div>
        <div class="row"><button class="act go" type="button" id="build-go">BUILD MY LEVEL</button></div></div>
      <ol class="steps" id="build-steps" hidden>
        <li data-step="brief">Planning three rooms from your data</li>
        <li data-step="records">Reading their records</li>
        <li data-step="map">Building the map</li>
        <li data-step="start">Starting your level</li>
      </ol>
      <p class="msg" id="build-msg" aria-live="polite"></p>
      <p class="fine" id="build-fine"></p></div>`;
    document.body.appendChild(el);
    const $ = id => el.querySelector(`#${id}`);
    const note = (text, cls = "") => { $("build-msg").textContent = text; $("build-msg").className = `msg ${cls}`; };
    let building = false;

    function step(name, text) {
      let past = true;
      for (const li of el.querySelectorAll("[data-step]")) {
        const now = li.dataset.step === name;
        if (now) past = false;
        li.className = now ? "now" : past ? "done" : "";
      }
      note(text);
    }

    // While it builds, the page shows what a Mac shows while Claude briefs a level: no game, the stage's
    // automap drawing the rooms as the model's Connect AI calls come in, and their feed in the panel.
    function showBuilding(ai) {
      document.body.classList.add("building");
      const headline = document.getElementById("stage-headline"), eyebrow = document.querySelector("#stage-screen .splash .eyebrow span");
      if (headline) headline.innerHTML = `${esc(modelName(ai.model))} is building a Doom level from your <span class="hl">live</span> data`;
      if (eyebrow) eyebrow.textContent = modelName(ai.model);
      nameBuilder(ai.model);
    }

    async function build() {
      const settings = AI.load(), ai = AI.active(settings), question = $("build-q").value.trim();
      if (!question) { note("Type a question first.", "bad"); return; }
      if (!gameWad) { note("The game is still loading. Try again in a moment.", "bad"); return; }
      building = true;
      $("build-go").disabled = $("build-q").disabled = true;
      $("build-steps").hidden = false;
      trackEvent("Build level", {provider: ai.provider});
      const before = {events: state.events, game: state.game, mission: state.mission}, events = [];
      Object.assign(state, {events, game: null});
      const log = (kind, data) => {
        events.push({t: Date.now() / 1000, kind, ...data});
        if (kind === "mission") state.mission = data.mission;   // the automap's rooms
      };
      el.close();
      showBuilding(ai);
      try {
        const {files, mission} = await AI.buildLevel(settings, question, gameWad, log, step);
        step("start", "Your level is built. Starting it…");
        await myLevel.save({files, question, model: ai.model, answer: mission.answer, built: Date.now()});
        trackEvent("Level built", {provider: ai.provider});
        await new Promise(r => setTimeout(r, 2500));   // the finished automap a moment, as on a Mac before the game opens
        location.reload();
      } catch (e) {
        console.error("Level build:", e);
        trackEvent("Level build failed", {provider: ai.provider});
        Object.assign(state, before);
        document.body.classList.remove("building");
        nameBuilder(mine ? mine.model : "claude");
        el.showModal();
        note(`Couldn't build it: ${e.message}`, "bad");
        $("build-go").disabled = $("build-q").disabled = false;
        building = false;
      }
    }

    el.addEventListener("click", e => {
      if (e.target === el || e.target.closest("[data-close]")) { el.close(); return; }
      const idea = e.target.closest("[data-idea]");
      if (idea && !building) $("build-q").value = idea.dataset.idea;
      if (e.target.closest("#build-go")) build();
      if (e.target.closest("[data-settings]")) { el.close(); openSettings(); }
    });
    return {
      build,
      building: () => building,
      open(question) {
        const ai = AI.active(AI.load()), ready = buildReady();
        $("build-setup").hidden = ready || building;
        $("build-form").hidden = !ready && !building;
        $("build-checklist").innerHTML = checklist();
        if (!building) {
          note("");
          const ideas = suggestQuestions(AI.connections() || []);
          $("build-q").value = question.trim() || ideas[0] || "";
          $("build-ideas").innerHTML = ideas.map(q => `<button type="button" data-idea="${esc(q)}">${esc(q)}</button>`).join("");
          $("build-steps").hidden = true;
        }
        $("build-lead").textContent = ready || building ? `${modelName(ai.model)} reads your data through Connect AI, picks the three apps ` +
          "that best answer your question, and turns their records into the level's monsters. Then you play it."
          : "Ask a question about your own data, and your AI model builds a Doom level from what it finds through Connect AI. " +
            "It needs two things first:";
        $("build-fine").textContent = ready || building ? `It takes about three minutes and up to 40 Connect AI calls, and uses your own ${AI.PROVIDERS[ai.provider].company} ` +
          "credits. You'll watch its Connect AI calls as it goes. The level stays in this browser."
          : "Your keys stay in this browser and go straight to the AI provider and to Connect AI.";
        el.showModal();
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
    // set up just now: what to do next comes up as the settings close
    const wasReady = buildReady();
    settingsDialog.el.addEventListener("close", () => { if (!wasReady && buildReady()) openBuilder(); }, {once: true});
  }

  // The visitor can build a level: their model and Connect AI are set up, and Connect AI listed their connections
  function buildReady() {
    return setupSteps().every(step => step.done);
  }

  // What building a level needs, each with where it stands: an AI model, and Connect AI's connections
  let connectAIError = null;   // why Connect AI didn't list the connections, the last time it was asked
  function setupSteps() {
    const settings = AI.load(), ai = AI.active(settings), list = AI.connections(), p = AI.PROVIDERS[settings.provider];
    return [
      {done: !!ai, text: ai ? `AI model: ${modelName(ai.model)} (${AI.PROVIDERS[ai.provider].company})`
        : p ? `AI model: paste ${article(p.company)} ${p.company} API key` : "AI model: pick Claude, ChatGPT or OpenRouter, and paste its API key"},
      {done: !!list?.length, text: !AI.hasConnectAI(settings) ? "Connect AI: add your email and personal access token"
        : connectAIError ? `Connect AI: ${connectAIError}` : list?.length ? `Connect AI: ${connectionList(list)}`
        : list ? "Connect AI: connected, but it lists no connections" : "Connect AI: checking…"},
    ];
  }
  const checklist = () => `<ul class="checklist">${setupSteps().map(step =>
    `<li class="${step.done ? "ok" : "todo"}">${esc(step.text)}</li>`).join("")}</ul>`;

  // The AI settings' and the level builder's dialogs, styled alike (once)
  let dialogStyled = false;
  function styleDialogs() {
    if (dialogStyled) return;
    dialogStyled = true;
    const style = document.createElement("style");
    style.textContent = `
      .ai-dialog { width: min(640px, calc(100vw - 32px)); max-height: calc(100vh - 32px); padding: 0; color: var(--clarity);
                     background: var(--depth); border: 2px solid var(--agility); border-radius: 4px; }
      .ai-dialog [hidden] { display: none !important; }
      .ai-dialog::backdrop { background: rgba(13, 13, 18, .8); }
      .ai-dialog .wrap { padding: 22px 24px; display: flex; flex-direction: column; gap: 18px; }
      .ai-dialog .head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
      .ai-dialog h2 { margin: 0; font: 12px/1.4 var(--pixel); color: var(--agility); letter-spacing: 1px; }
      .ai-dialog h3 { margin: 0 0 4px; font: 9px/1.6 var(--pixel); color: var(--clarity); letter-spacing: 1px; }
      .ai-dialog p, .ai-dialog label { margin: 0; font: 14px/1.45 var(--sans); color: var(--balance); }
      .ai-dialog section { display: flex; flex-direction: column; gap: 10px; padding-top: 16px; border-top: 1px solid var(--line); }
      .ai-dialog .status { font: 15px/1.45 var(--sans); color: #E5E2DA; border-left: 3px solid var(--agility); padding-left: 12px; }
      .ai-dialog .seg { display: flex; flex-wrap: wrap; gap: 6px; }
      .ai-dialog .seg button, .ai-dialog .act, .ai-dialog .switch { font: 9px/1 var(--pixel); letter-spacing: 1px; cursor: pointer;
        padding: 11px 12px; border-radius: 3px; border: 1px solid var(--line); background: var(--depth-3); color: var(--clarity); }
      .ai-dialog .seg button[aria-pressed=true], .ai-dialog .switch[aria-pressed=true] { background: var(--agility); color: var(--depth); border-color: var(--agility); }
      .ai-dialog .act.danger { color: var(--key-red); background: none; }
      .ai-dialog button:focus-visible, .ai-dialog input:focus-visible, .ai-dialog select:focus-visible { outline: 3px solid var(--clarity); outline-offset: 2px; }
      .ai-dialog .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
      .ai-dialog .field { display: grid; gap: 4px; flex: 1 1 200px; }
      .ai-dialog input, .ai-dialog select { width: 100%; font: 14px/1.3 var(--mono); color: var(--clarity); background: var(--depth-3);
        border: 1px solid var(--line); border-radius: 3px; padding: 10px; }
      .ai-dialog .msg { font-size: 13px; min-height: 1.2em; }
      .ai-dialog .msg.ok { color: var(--agility); }
      .ai-dialog .msg.bad { color: var(--key-red); }
      .ai-dialog a { color: var(--agility); }
      .ai-dialog .game { display: grid; grid-template-columns: auto 1fr; gap: 10px 14px; align-items: center; }
      .ai-dialog .fine { font-size: 12px; }
      .ai-dialog .setup { margin: 0; padding-left: 20px; display: grid; gap: 4px; font: 14px/1.45 var(--sans); color: #E5E2DA; }
      .ai-dialog .act.go { background: var(--agility); color: var(--depth); border-color: var(--agility); }
      .ai-dialog .act:disabled { opacity: .5; cursor: default; }
      .ai-dialog textarea { width: 100%; resize: vertical; font: 15px/1.4 var(--sans); color: var(--clarity); background: var(--depth-3);
        border: 1px solid var(--line); border-radius: 3px; padding: 10px; }
      .ai-dialog .ideas { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; }
      .ai-dialog .ideas button { font: 13px/1.35 var(--sans); text-align: left; color: var(--clarity); background: var(--depth-3);
        border: 1px solid var(--line); border-radius: 3px; padding: 6px 10px; cursor: pointer; }
      .ai-dialog .ideas button:hover { border-color: var(--agility); color: var(--agility); }
      .ai-dialog .steps { margin: 0; padding-left: 22px; display: grid; gap: 4px; font: 14px/1.45 var(--sans); color: var(--balance); }
      .ai-dialog .steps li.now { color: var(--agility); font-weight: 600; }
      .ai-dialog .steps li.done { color: var(--clarity); }
      .ai-dialog .steps li.done::after { content: " ✓"; color: var(--ok); }`;
    document.head.appendChild(style);
  }

  function makeSettings() {
    styleDialogs();

    const el = document.createElement("dialog");
    el.id = "ai-settings";
    el.className = "ai-dialog";
    el.setAttribute("aria-labelledby", "ai-settings-title");
    const providers = Object.entries(AI.PROVIDERS);
    el.innerHTML = `<div class="wrap">
      <div class="head"><h2 id="ai-settings-title">AI SETTINGS</h2><button class="act" type="button" data-close>CLOSE</button></div>
      <p class="status" id="ai-status" role="status"></p>
      <div class="row" id="ai-next" hidden><button class="act go" type="button" data-build>BUILD A LEVEL FROM YOUR DATA</button></div>
      <section>
        <h3>YOUR AI MODEL</h3>
        <p>Offline, Claude's recorded run plays: no keys needed. Bring your own key and your model plays instead.</p>
        <div class="seg" role="group" aria-label="Who plays">
          <button type="button" data-provider="offline">OFFLINE</button>
          ${providers.map(([id, p]) => `<button type="button" data-provider="${id}">${p.name.toUpperCase()}</button>`).join("")}
        </div>
        <div id="ai-provider" class="row">
          <label class="field">API key<input type="password" id="ai-key" autocomplete="off" spellcheck="false"></label>
          <label class="field">Model<select id="ai-model"></select></label>
          <button class="act" type="button" id="ai-check">CHECK KEY</button>
          <a id="ai-get-key" target="_blank" rel="noopener noreferrer">Get a key</a>
        </div>
        <p class="msg" id="ai-msg" aria-live="polite"></p>
      </section>
      <section>
        <h3>CONNECT AI</h3>
        <p>Your model reads your own data through the Connect AI MCP server: it can look records up while it
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
          <label class="field">MCP server URL<input type="url" id="cai-url" autocomplete="off" spellcheck="false"></label>
          <button class="act" type="button" id="cai-check">CHECK</button>
        </div>
        <p class="fine">This is Connect AI's MCP server. Change it only to use one of your MCP Gateway tool
          servers: paste its Agent URL.</p>
        <p class="msg" id="cai-msg" aria-live="polite"></p>
      </section>
      <section id="game-switches">
        <h3>IN THE GAME (ALSO ESC &gt; OPTIONS)</h3>
        <div class="game">
          <span>Claude plays</span><div><button class="switch" type="button" data-option="0"></button></div>
          <span class="game-model">Model</span><select class="game-model" id="game-model" aria-label="Who plays when Claude plays is on"></select>
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
        // the key's own models once checked, else the usual ones; the saved pick stays on the list either way
        const model = settings.models[provider] || p.defaultModel, models = listed[provider] || p.models;
        $("ai-model").innerHTML = [...new Set([model, ...models])].map(m => `<option>${esc(m)}</option>`).join("");
        $("ai-model").value = model;
        $("ai-get-key").href = p.keyUrl;
        $("ai-get-key").textContent = `Get ${article(p.company)} ${p.company} key`;
      }
      if (document.activeElement !== $("cai-user")) $("cai-user").value = settings.connectAI.user;
      if (document.activeElement !== $("cai-pat")) $("cai-pat").value = settings.connectAI.pat;
      if (document.activeElement !== $("cai-url")) $("cai-url").value = settings.connectAI.url || AI.MCP_URL;
      $("ai-status").innerHTML = (buildReady() ? "Ready: build a level from your data." :
        "To build a level from your data, and to ask it questions, you need both:") + checklist();
      renderGame();
      renderAILink();
      $("ai-next").hidden = !buildReady();
    }

    // the game's switches as the game has them: the Options menu changes them too
    function renderGame() {
      const options = gameState()?.options, [models] = menuModels();
      el.querySelectorAll("[data-option]").forEach(b => {
        const on = !!options?.[b.dataset.option === "0" ? "claude_plays" : "god_mode"];
        b.setAttribute("aria-pressed", String(on));
        b.textContent = on ? "ON" : "OFF";
      });
      const select = $("game-model"), names = models.split(",");
      if (select.dataset.models !== models) {
        select.innerHTML = names.map((m, i) => `<option value="${i}">${m === AI.OFFLINE ? "Offline Claude" : esc(modelName(m))}</option>`).join("");
        select.dataset.models = models;
      }
      if (document.activeElement !== select) select.value = String(Math.max(0, names.indexOf(options?.model)));
      $("game-switches").hidden = !options;   // no game yet (the start card is up)
      el.querySelectorAll(".game-model").forEach(e => { e.hidden = names.length < 2; });
    }

    el.addEventListener("click", e => {
      if (e.target === el || e.target.closest("[data-close]")) { el.close(); return; }
      if (e.target.closest("[data-build]")) { openBuilder(); return; }
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
      if (key) checkKey(); else note("ai-msg", "Key removed.");
    });
    $("ai-model").addEventListener("change", e => {
      const model = e.target.value.trim();
      update(s => { if (model) s.models[s.provider] = model; else delete s.models[s.provider]; });
    });
    $("ai-check").addEventListener("click", () => checkKey());
    async function checkKey() {
      const settings = AI.load(), provider = settings.provider, key = settings.keys[provider];
      if (!key) { note("ai-msg", "Paste a key first.", "bad"); return; }
      note("ai-msg", "Checking…");
      try {
        listed[provider] = await AI.listModels(provider, key);
        const model = settings.models[provider] || AI.PROVIDERS[provider].defaultModel;
        const known = listed[provider].includes(model);
        note("ai-msg", `The key works: ${listed[provider].length} models in the list.${known ? "" : ` ${model} isn't one of them: pick another.`}`,
             known ? "ok" : "bad");
        trackEvent("AI key checked", {provider, ok: "yes"});
        render();
      } catch (e) {
        trackEvent("AI key checked", {provider, ok: "no"});
        note("ai-msg", e.message, "bad");
      }
    }
    const saveConnectAI = () => {
      // Connect AI's own server is kept as no URL: ai.js adds its read-only ?ops= then
      const typed = $("cai-url").value.trim(), url = typed === AI.MCP_URL ? "" : typed;
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
        connectAIError = null;
        settingsChanged();
        render();
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
      #build-link { font: 9px/1 var(--pixel); letter-spacing: 1px; cursor: pointer; color: var(--agility); background: none;
                    border: 1px solid var(--agility); border-radius: 3px; padding: 6px 10px; }
      #build-link:hover, #build-link:focus-visible { background: var(--agility); color: var(--depth); outline: none; }
      /* what building a level needs (setupSteps), in the dialogs and on the start card */
      .checklist { list-style: none; margin: 6px 0 0; padding: 0; display: grid; gap: 3px; }
      .checklist li::before { content: "✗ "; color: var(--key-red); font-weight: 700; }
      .checklist li.ok::before { content: "✓ "; color: var(--ok); }
      .checklist li.todo { color: var(--clarity); }
      .coach-card .checklist li { display: block; font: 13px/1.4 var(--sans); counter-increment: none; }   /* not the card's steps */
      .coach-card .checklist li::before { content: "✗ "; color: var(--key-red); background: none; font: inherit; line-height: inherit; text-align: left; }
      .coach-card .checklist li.ok::before { content: "✓ "; color: var(--ok); }
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
    nav.innerHTML = `<button type="button" id="build-link">BUILD A LEVEL</button><button type="button" id="ai-link"></button><a href="about.html">ABOUT</a>${external(CONNECT_AI, "CONNECT AI")}${external(REPO, "GITHUB")}`;
    nav.querySelector("#ai-link").addEventListener("click", openSettings);
    nav.querySelector("#build-link").addEventListener("click", () => openBuilder());
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
