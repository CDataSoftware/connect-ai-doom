// connect-doom in a browser: the side panel's full-screen stage (panel/index.html, loaded after this
// script) with the game, compiled to WebAssembly, on the stage's screen. What raid.sh and the bridge
// (connect_doom.py) do on a Mac happens here: the panel's /state.json starts from the state
// web/build.sh baked (connect_doom.py state) and takes this game's kills and hits, and psdoom's
// list / kill / hit commands are answered from the level's records.
(() => {
  const REPO = "https://github.com/CDataSoftware/connect-ai-doom";

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

  // ---------- this game's records, kills and hits ----------
  let state = null, monsters = [], said = {};
  const kills = [], hits = [];
  const alive = () => monsters.filter(m => !kills.some(k => k.id === m.id));

  function currentState() {
    const dead = new Set(kills.map(k => k.id));
    const rooms_live = state.rooms_live.map((room, i) => {
      const mine = monsters.filter(m => m.pid % 3 === i), left = mine.filter(m => !dead.has(m.id));
      return {...room, left: left.length, urgent_left: left.filter(m => m.demon).length,
              killed: mine.length - left.length};
    });
    return {...state, rooms_live, kills: kills.slice(-12), kill_total: kills.length, hits: hits.slice(-40),
            standing: alive().length};
  }

  // connect_doom.kill_message: the in-game line for a kill.
  function killMessage(m) {
    const left = alive(), inRoom = left.filter(x => x.pid % 3 === m.pid % 3);
    if (!left.length) return "ALL RECORDS CLEARED!" + (state.mission?.denied ? " NOW TRY THE RED DOOR" : "");
    if (!inRoom.length) return `${m.app} room clear! ${left.length} left elsewhere`.toUpperCase();
    return `${m.app}: ${m.label}`.slice(0, 36).toUpperCase() + `, ${inRoom.length} LEFT`;
  }

  function bridge(command) {
    const [verb, arg] = command.split(" ");
    const m = monsters.find(x => x.pid === Number(arg));
    const now = Date.now() / 1000;
    if (verb === "list") return alive().map(x => `connectai ${x.pid} ${x.name} ${x.demon}`).join("\n") + "\n";
    if (verb === "kill" && m && alive().includes(m)) {
      // said: what Claude wrote about this record when it played the level (web/snapshot.py)
      if (alive().length === 1) setTimeout(() => window.dispatchEvent(new Event("connect-doom:cleared")), 1500);
      kills.push({...m, killed_at: new Date().toISOString().slice(0, 19), mode: "read-only", lookup: null,
                  said: said[m.pid] || null});
      return killMessage(m) + "\n";
    }
    if (verb === "hit" && m) hits.push({...m, at: now});
    return "";
  }

  // The panel polls /state.json and posts questions to /ask; nothing here talks to a server.
  const realFetch = window.fetch.bind(window);
  const json = (body, status = 200) => new Response(JSON.stringify(body), {status, headers: {"Content-Type": "application/json"}});
  window.fetch = (url, opts) => {
    const path = new URL(url, location.href).pathname;
    if (path.endsWith("/state.json")) return Promise.resolve(json(state ? currentState() : {}));
    return realFetch(url, opts);
  };

  // ---------- the game ----------
  const files = {"/doom/freedoom2.wad": "freedoom2.wad", "/doom/level.wad": "level/level.wad",
                 "/doom/brand.wad": "level/brand.wad", "/doom/level.deh": "level/level.deh",
                 "/doom/labels.tsv": "level/labels.tsv"};
  const get = u => realFetch(u).then(r => { if (!r.ok) throw new Error(`${u}: ${r.status}`); return r; });

  async function start() {
    const canvas = document.createElement("canvas");
    canvas.id = "canvas";
    canvas.tabIndex = 0;
    // A stylesheet rule, not inline styles: Emscripten's SDL sets the canvas's inline size.
    const style = document.createElement("style");
    style.textContent = "#canvas { position: absolute !important; inset: 0 !important; width: 100% !important; " +
      "height: 100% !important; z-index: 1; image-rendering: pixelated; background: #000; outline: none; cursor: none; }";
    document.head.appendChild(style);
    document.getElementById("stage-screen").appendChild(canvas);
    canvas.addEventListener("click", () => { canvas.focus(); lockPointer(canvas); });
    const [baked, cache, saidJson, monsterKinds, ...bufs] = await Promise.all([
      get("level/state.json").then(r => r.json()), get("level/cache.json").then(r => r.json()),
      get("level/said.json").then(r => r.json()), get("level/monsters.txt").then(r => r.text()),
      ...Object.values(files).map(u => get(u).then(r => r.arrayBuffer()))]);
    monsters = cache.monsters;
    said = saidJson;
    // Under the game: what the monsters are, and how to start.
    const fine = document.querySelector("#stage .bar.bottom .fine");
    if (fine) fine.textContent = "Every monster is a record Claude read through the Connect AI MCP server. " +
                                 "Click the game to play; Esc frees the mouse.";
    const briefing = document.querySelector("#feed")?.parentElement.querySelector("h2");
    if (briefing) briefing.textContent = "EVERY CALL CLAUDE MADE TO CONNECT AI";
    // No question box: a new question needs Claude and Connect AI, which the Mac version runs.
    state = {...baked, game: {mode: "player", started: Date.now() / 1000}, ask: null};
    coach(canvas, state.mission);

    window.Module = {
      canvas,
      print: s => console.log(s),
      printErr: s => console.warn(s),
      bridge,
      preRun: [() => {
        FS.mkdir("/doom");
        Object.keys(files).forEach((path, i) => FS.writeFile(path, new Uint8Array(bufs[i])));
        // raid.sh's controls: WASD, the mouse turns, left click or Space fires, E or right click opens doors.
        FS.writeFile("/doom/controls.cfg", "key_up 17\nkey_down 31\nkey_strafeleft 30\nkey_straferight 32\n" +
          "key_left 75\nkey_right 77\nkey_fire 57\nkey_use 18\nkey_speed 42\nkey_strafe 56\nuse_mouse 1\n" +
          "mouseb_fire 0\nmouseb_strafe -1\nmouseb_forward -1\nmouse_sensitivity 6\n");
        FS.writeFile("/doom/extra.cfg", "novert 1\ngrabmouse 1\nmouseb_use 1\nshow_endoom 0\nfullscreen 0\n" +
          "screen_width 640\nscreen_height 480\n");
        Object.assign(ENV, {PSDOOMPSCMD: "list", PSDOOMKILLCMD: "kill", PSDOOMRENICECMD: "hit",
          PSDOOMVAULTCMD: "vault", PSDOOM_LABELS: "/doom/labels.tsv", PSDOOM_MONSTERS: monsterKinds.trim(),
          HOME: "/doom"});
      }],
      // No sound yet: Emscripten's SDL mixer can't play the buffers Chocolate Doom builds.
      onRuntimeInitialized: () => Module.callMain(["-iwad", "/doom/freedoom2.wad", "-file", "/doom/level.wad",
        "/doom/brand.wad", "-deh", "/doom/level.deh", "-nopslev", "-psallusers", "-warp", "1", "-skill", "1",
        "-window", "-nosound", "-nomusic", "-config", "/doom/controls.cfg", "-extraconfig", "/doom/extra.cfg"]),
    };
    const script = document.createElement("script");
    script.src = "psdoom.js";
    document.body.appendChild(script);
  }
  // The browser can refuse the pointer lock (say, right after Esc); the next click asks again.
  function lockPointer(canvas) {
    try { canvas.requestPointerLock()?.catch?.(() => {}); } catch (e) {}
  }

  // ---------- new players: what to do, a step at a time ----------
  // A card over the game until the first click, then a banner per step: grab the keycards (until the
  // player holds every one the level needs), clear the rooms, and once they're clear, the red door.
  function coach(canvas, mission) {
    const rooms = mission?.rooms || [];
    const need = (rooms.some(r => !r.writable) ? 1 : 0) | (rooms.some(r => r.writable) ? 2 : 0);   // 1 blue, 2 yellow
    const screen = document.getElementById("stage-screen");
    const style = document.createElement("style");
    style.textContent = `
      #coach-intro { position: absolute; inset: 0; z-index: 3; display: flex; align-items: center; justify-content: center;
                     background: rgba(13, 13, 18, .86); cursor: pointer; padding: 24px; }
      #coach-intro[hidden], #coach-tip[hidden] { display: none; }
      #coach-intro .card { max-width: 620px; display: flex; flex-direction: column; gap: 16px; }
      #coach-intro .eyebrow { font: 9px/1.6 var(--pixel); color: var(--agility); letter-spacing: 1px; }
      #coach-intro h3 { margin: 0; font: 700 clamp(24px, 3vw, 36px)/1.15 var(--sans); color: var(--clarity); }
      #coach-intro h3 .red { color: var(--doom-red); }
      #coach-intro p { margin: 0; font: 17px/1.5 var(--sans); color: #E5E2DA; }
      #coach-intro ol { margin: 0; padding: 0; list-style: none; display: grid; gap: 10px; counter-reset: step; }
      #coach-intro li { counter-increment: step; display: grid; grid-template-columns: 34px 1fr; gap: 12px; align-items: center;
                        font: 16px/1.4 var(--sans); color: #E5E2DA; }
      #coach-intro li::before { content: counter(step); font: 12px/34px var(--pixel); text-align: center; color: var(--depth);
                                background: var(--agility); border-radius: 3px; }
      #coach-intro li b { color: var(--clarity); }
      #coach-intro .go { align-self: flex-start; font: 11px/1 var(--pixel); color: var(--depth); background: var(--agility);
                         border: 0; border-radius: 3px; padding: 15px 18px; cursor: pointer; letter-spacing: 1px; }
      #coach-intro .keys { font: 13px/1.5 var(--mono); color: var(--balance); }
      /* above Doom's status bar (its bottom 16%), clear of Doom's own message line at the top */
      #coach-tip { position: absolute; z-index: 2; bottom: calc(16% + 16px); left: 50%; transform: translateX(-50%); width: min(560px, 86%);
                   background: rgba(13, 13, 18, .9); border: 2px solid var(--agility); border-radius: 4px; padding: 12px 16px;
                   pointer-events: none; display: flex; flex-direction: column; gap: 6px; animation: coach-in .35s ease-out; }
      #coach-tip b { font: 9px/1.6 var(--pixel); color: var(--agility); letter-spacing: 1px; }
      #coach-tip span { font: 16px/1.4 var(--sans); color: var(--clarity); }
      @keyframes coach-in { from { opacity: 0; transform: translate(-50%, 8px); } }`;
    document.head.appendChild(style);

    const intro = document.createElement("div");
    intro.id = "coach-intro";
    intro.innerHTML = `<div class="card">
      <div class="eyebrow">CONNECT AI × DOOM</div>
      <h3>Every monster is a <span class="red">record</span>.</h3>
      <p>Claude read a company's apps through the Connect AI MCP server and built this level from what it found.</p>
      <ol>
        <li><span><b>Grab your keys.</b> Keycards are your permissions: Connect AI gives you one for each app you can open.</span></li>
        <li><span><b>Clear the rooms.</b> Each room is an app, and each monster is a record from it.</span></li>
        <li><span><b>Try the red door.</b> It guards data Connect AI blocked.</span></li>
      </ol>
      <button class="go" type="button">CLICK TO PLAY</button>
      <div class="keys">WASD move · mouse turn · click fire · walk into doors · Esc frees the mouse</div></div>`;
    const tip = document.createElement("div");
    tip.id = "coach-tip";
    tip.hidden = true;
    screen.append(intro, tip);

    let hideAt = 0;
    const show = (title, text, seconds) => {
      tip.innerHTML = `<b>${title}</b><span>${text}</span>`;
      tip.hidden = false;
      tip.style.animation = "none"; void tip.offsetWidth; tip.style.animation = "";
      hideAt = seconds ? Date.now() + seconds * 1000 : 0;
    };
    let step = 0;
    intro.addEventListener("click", () => {
      intro.hidden = true;
      canvas.focus();
      lockPointer(canvas);
      if (step === 0) {
        step = 1;
        show("STEP 1 OF 3 · GRAB YOUR KEYS", "Keycards are your permissions. Connect AI gives you one for each app you can open. They're in the lobby.");
      }
    });
    setInterval(() => {
      if (hideAt && Date.now() > hideAt) { tip.hidden = true; hideAt = 0; }
      const keys = window.Module?._web_keys ? Module._web_keys() : 0;
      if (step === 1 && (keys & need) === need) {
        step = 2;
        show("STEP 2 OF 3 · CLEAR THE ROOMS", "Access granted. Each room is an app, and each monster is a record from it. Shoot one to read it in the panel.", 9);
      }
    }, 300);
    addEventListener("connect-doom:cleared", () => {
      if ((mission?.denied || []).length) {
        step = 3;
        show("STEP 3 OF 3 · TRY THE RED DOOR", "Every record is cleared. The red door guards data Connect AI blocked. There is no key.", 10);
      }
    });
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
    nav.innerHTML = `<a href="about.html">ABOUT</a><a href="${REPO}">GITHUB</a>`;
    const chip = document.getElementById("stage-mode");
    chip.parentNode.insertBefore(nav, chip);
    const gate = document.createElement("div");
    gate.id = "desktop-only";
    gate.hidden = true;
    gate.innerHTML = `<div class="box"><img src="brand/connect-ai.svg" alt="Connect AI" height="32">
      <div class="doom">DOOM</div>
      <p id="desktop-why"></p>
      <div class="links"><a href="about.html">WHAT IS THIS?</a><a href="${REPO}">GITHUB</a></div></div>`;
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
        start().catch(e => console.error("connect-doom web:", e));
        return;
      }
      gate.hidden = false;
      document.getElementById("desktop-why").textContent = hasMouse()
        ? "Connect AI × DOOM needs a bigger window. Make this one wider to play."
        : "Connect AI × DOOM plays on a desktop browser, with a keyboard and a mouse. Open this page on your computer to play.";
    };
    check();
    addEventListener("resize", check);
  });
})();
