// connect-doom in a browser: the side panel's full-screen stage (panel/index.html, loaded after this
// script) with the game, compiled to WebAssembly, on the stage's screen. What raid.sh and the bridge
// (connect_doom.py) do on a Mac happens here: the panel's /state.json starts from the state
// web/build.sh baked (connect_doom.py state) and takes this game's kills, hits and red-door tries, and
// psdoom's list / kill / hit / vault commands are answered from the level's records. replay.js plays
// the level the way Claude did, for "Watch Claude play".
(() => {
  const REPO = "https://github.com/CDataSoftware/connect-ai-doom";
  const CONNECT_AI = "https://www.cdata.com/ai/";
  const external = (href, text, cls = "") =>
    `<a href="${href}" target="_blank" rel="noopener noreferrer"${cls ? ` class="${cls}"` : ""}>${text}</a>`;
  // A custom event for the site's Plausible analytics (web/build.sh puts its script on the page)
  const trackEvent = (name, props) => window.plausible?.(name, props ? {props} : undefined);

  // ---------- the stage's layout (raid.sh passes it as ?stage=gx,gy,gw,gh,split,top) ----------
  // The game takes two thirds of the window at 4:3, with room for the brand bars above and below.
  function layout() {
    const w = innerWidth, h = innerHeight, split = Math.round(w * 2 / 3);
    const gh = Math.max(200, Math.min(h - 200, (split - 48) * 3 / 4)), gw = gh * 4 / 3;
    return [Math.round((split - gw) / 2), Math.round((h - gh) / 2), Math.round(gw), Math.round(gh), split, 0];
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
            standing: alive().length, vault: vault.slice(-5), play_events: playEvents};
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

  // The panel polls /state.json; nothing here talks to a server.
  const realFetch = window.fetch.bind(window);
  const json = (body, status = 200) => new Response(JSON.stringify(body), {status, headers: {"Content-Type": "application/json"}});
  window.fetch = (url, opts) => {
    const path = new URL(url, location.href).pathname;
    if (path.endsWith("/state.json")) return Promise.resolve(json(state ? currentState() : {}));
    return realFetch(url, opts);
  };

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
    // No question box: a new question needs Claude and Connect AI, which the Mac version runs.
    state = {...baked, game: {mode: "player", started: Date.now() / 1000}, ask: null};
    plainWords();
    const coach = makeCoach(canvas);
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
        Object.assign(ENV, {PSDOOMPSCMD: "list", PSDOOMKILLCMD: "kill", PSDOOMRENICECMD: "hit",
          PSDOOMVAULTCMD: "vault", PSDOOM_LABELS: "/doom/labels.tsv", PSDOOM_MONSTERS: monsterKinds.trim(),
          PSDOOM_AGENT_DIR: "/doom/agent", HOME: "/doom"});
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
    if (fine) fine.innerHTML = "Every monster is a record Claude read through the " +
      external(CONNECT_AI, "CData Connect AI") + " MCP server. Click the game to play; Esc frees the mouse.";
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
      <div class="keys">WASD move · mouse turn · click fire · walk into doors · Esc frees the mouse</div>`);
    const end = card("coach-end", "");
    end.hidden = true;
    const tip = document.createElement("div");
    tip.id = "coach-tip";
    tip.hidden = true;
    tip.setAttribute("role", "status");
    screen.append(intro, end, tip);

    let hideAt = 0, step = 0, replay = null, ended = false;
    const show = (title, text, seconds) => {
      tip.innerHTML = `<b>${title}</b><span>${text}</span>`;
      tip.hidden = false;
      tip.style.animation = "none"; void tip.offsetWidth; tip.style.animation = "";
      hideAt = seconds ? Date.now() + seconds * 1000 : 0;
    };

    function playYourself() {
      intro.hidden = end.hidden = true;
      if (replay) { replay.stop(); replay = null; }
      state.game = {...state.game, mode: "player"};
      canvas.focus();
      lockPointer(canvas);
      if (step === 0) {
        step = 1;
        show("STEP 1 OF 3 · GRAB YOUR KEYS", "Keycards are your permissions. Connect AI gives you one for each app you can open. They're in the lobby.");
      } else tip.hidden = true;
    }

    function watch() {
      intro.hidden = end.hidden = true;
      if (document.pointerLockElement) document.exitPointerLock();
      playEvents.length = 0;
      state.game = {...state.game, mode: "replay"};
      show("WATCHING CLAUDE PLAY", "Claude grabs the keycards, then clears the rooms. Its feed is on the right. Click the game to take over.");
      const level = {mission, records: monsters, said};
      replay = window.ConnectDoomReplay.play(level, (kind, data) => playEvents.push({t: Date.now() / 1000, kind, ...data}));
      replay.done.then(() => { if (replay) { replay = null; finish("watched"); } });
    }

    // The end: what just happened, in a sentence or two, and where to go next.
    function finish(how) {
      if (ended) return;
      ended = true;
      trackEvent("Level clear", {how});
      tip.hidden = true;
      if (document.pointerLockElement) document.exitPointerLock();
      const n = kills.length;
      end.innerHTML = `<div class="card">
        <div class="eyebrow">DOOMCP · LEVEL CLEAR · ${n}/${monsters.length} RECORDS</div>
        <h3 id="coach-end-title">${how === "watched" ? "Claude" : "You"} just read ${n} records from ${appList}.</h3>
        <p>All through one MCP server: Connect AI. It let Claude read the apps it was allowed into${denied
          ? ", and blocked the one request it shouldn't answer: a customer's Social Security number" : ""}.
          That's what Connect AI does for any AI agent and your business data.</p>
        <div class="row">${external(CONNECT_AI, "TRY CONNECT AI", "btn")}
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
      if (go === "watch") { trackEvent("Watch Claude"); watch(); }
    });
    end.addEventListener("click", e => {
      if (e.target.closest(`a[href="${CONNECT_AI}"]`)) trackEvent("Try Connect AI");
      if (e.target.closest("[data-go=again]")) location.reload();
    });
    // During Claude's run, a click on the game takes the marine over where it stands.
    canvas.addEventListener("click", () => {
      if (replay) { trackEvent("Take over from Claude"); playYourself(); return; }
      canvas.focus();
      lockPointer(canvas);
    });

    setInterval(() => {
      if (hideAt && Date.now() > hideAt) { tip.hidden = true; hideAt = 0; }
      const keys = window.Module?._web_keys ? Module._web_keys() : 0;
      if (step === 1 && !replay && (keys & need) === need) {
        step = 2;
        trackEvent("Got keys");
        show("STEP 2 OF 3 · CLEAR THE ROOMS", "Access granted. Each room is an app, and each monster is a record from it. Shoot one to read it in the panel.", 9);
      }
    }, 300);
    let endTimer = 0;
    events.addEventListener("cleared", () => {
      if (replay) return;                                   // Claude's run goes on to the red door itself
      trackEvent("Rooms cleared");
      if (!denied) { finish("played"); return; }
      step = 3;
      show("STEP 3 OF 3 · TRY THE RED DOOR", "Every record is cleared. The red door guards data Connect AI blocked. There is no key.", 12);
      endTimer = setTimeout(() => finish("played"), 30000);  // nobody tried the door: wrap up anyway
    });
    events.addEventListener("vault", () => {
      if (replay || alive().length) return;
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
    };
  }

  // ---------- links, and desktops only ----------
  function addLinks() {
    const style = document.createElement("style");
    style.textContent = `
      .weblinks { display: flex; gap: 18px; margin-left: auto; }
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
    nav.innerHTML = `<a href="about.html">ABOUT</a>${external(CONNECT_AI, "CONNECT AI")}${external(REPO, "GITHUB")}`;
    const chip = document.getElementById("stage-mode");
    chip.parentNode.insertBefore(nav, chip);
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
