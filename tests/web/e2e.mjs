// The browser version, end to end, in headless Chromium: web/dist (web/build.sh) served locally, with
// Claude's, OpenAI's and OpenRouter's APIs and the Connect AI MCP server faked, so it needs no keys and no network.
// Every button, dialog and flow a visitor can reach has a test here.
//   cd tests/web && npm ci && npx playwright-core install chromium && node e2e.mjs [name filter]
// CHROMIUM=<path> runs a Chromium of your own instead of Playwright's; JOBS=<n> runs n tests at once (default 3).
import {spawn} from "node:child_process";
import {fileURLToPath} from "node:url";
import {chromium} from "playwright-core";

const DIST = fileURLToPath(new URL("../../web/dist", import.meta.url));
const PORT = Number(process.env.PORT || 8799), SITE = `http://127.0.0.1:${PORT}/`;
const JOBS = Number(process.env.JOBS || 3), ONLY = process.argv[2] || "";
const CORS = {"access-control-allow-origin": "*"};
const THINK_MS = 1500;   // how long a fake model takes over a move, so the panel shows it playing

// ---------- the fakes ----------
// Connect AI: three connections, and the same rows for any query; `deny` refuses the token
const CATALOGS = "TABLE_CATALOG,DATA_SOURCE,DRIVER,PERMISSIONS\nSalesforce1,Salesforce,Salesforce,\"Select, Insert, Update\"\n" +
  "Zendesk1,Zendesk,Zendesk,Select\nStripe1,Stripe,Stripe,Select\n\nQuery ID: c1";
const ROWS = "Id,Label,Detail,Hot\n1,Acme Corp,$45000 overdue,1\n2,Globex,$9000 due,0\n3,Initech,$1200 due,0\n\nQuery ID: q1";
const PLAN = {answer: "Acme Corp is at risk.", rooms: [["Salesforce1", "Salesforce", "Deals slipping"],
  ["Zendesk1", "Zendesk", "Angry tickets"], ["Stripe1", "Stripe", "Unpaid invoices"]]
  .map(([connection, app, title]) => ({connection, app, title, query: `SELECT [Id] FROM [${connection}].[dbo].[t]`}))};

function fakeConnectAI(mode, calls) {
  return async route => {
    if (mode === "deny") return route.fulfill({status: 401, body: "Unauthorized", headers: CORS});
    const body = JSON.parse(route.request().postData());
    if (body.method === "tools/call") calls.push(body.params);
    const reply = result => route.fulfill({json: {jsonrpc: "2.0", id: body.id, result}, headers: CORS});
    if (!("id" in body)) return route.fulfill({status: 202, body: "", headers: CORS});
    if (body.method === "initialize") return reply({protocolVersion: "2025-06-18", capabilities: {}});
    if (body.method === "tools/list") return reply({tools: ["getCatalogs", "queryData"].map(name =>
      ({name, description: name, inputSchema: {type: "object", properties: {query: {type: "string"}}}}))});
    return reply({content: [{type: "text", text: body.params.name === "getCatalogs" ? CATALOGS : ROWS}]});
  };
}

// Claude, as a real model goes about it: briefing, it reads records through Connect AI, then plans the
// level; playing, it looks around once (and with `query`, reads records through Connect AI too), then stops.
// `refuse` turns the key down; `badplan` answers the briefing with prose, not a plan; `greedy` keeps
// calling Connect AI until it's told the level's calls are spent; `busy` is overloaded at first.
const QUERY = {query: "SELECT [Id], [Name] FROM [Salesforce1].[dbo].[Account]"};
function fakeClaude(mode) {
  let busy = mode === "busy";
  return async route => {
    if (mode === "refuse") return route.fulfill({status: 401, json: {error: {message: "invalid x-api-key"}}, headers: CORS});
    if (route.request().url().includes("/models")) return route.fulfill({json: {data: [{id: "claude-sonnet-5-5"}, {id: "claude-opus-5-5"}]}, headers: CORS});
    if (busy) { busy = false; return route.fulfill({status: 529, json: {error: {message: "Overloaded"}}, headers: CORS}); }
    const body = JSON.parse(route.request().postData()), turn = body.messages.filter(m => m.role === "assistant").length;
    const lastResult = JSON.stringify(body.messages.at(-1)?.content || "");
    const use = (name, input) => ({type: "tool_use", id: `t${turn}`, name, input});
    let content;
    if (/Doom level/.test(body.system || "")) {
      const spent = /all the Connect AI calls this level gets/.test(lastResult);
      content = mode === "badplan" ? [{type: "text", text: "I'd rather not plan a level."}]
        : (mode === "greedy" && !spent) || turn === 0 ? [{type: "text", text: "Reading the accounts."}, use("connectai__queryData", QUERY)]
        : [{type: "text", text: JSON.stringify(PLAN)}];
    } else {
      const moves = mode === "query" ? [use("doom__look", {}), use("connectai__queryData", QUERY)] : [use("doom__look", {})];
      content = turn < moves.length ? [{type: "text", text: "Looking around."}, moves[turn]] : [{type: "text", text: "Done."}];
      await new Promise(r => setTimeout(r, THINK_MS));
    }
    return route.fulfill({json: {content, stop_reason: "end_turn", usage: {input_tokens: 1, output_tokens: 1}}, headers: CORS});
  };
}

// OpenAI and OpenRouter (both OpenAI's chat format): the model looks around once, then stops
function fakeChat(models, {badArgs = false} = {}) {
  return async route => {
    const url = route.request().url();
    if (url.endsWith("/key")) return route.fulfill({json: {data: {label: "test"}}, headers: CORS});
    if (url.endsWith("/models")) return route.fulfill({json: {data: models}, headers: CORS});
    const body = JSON.parse(route.request().postData()), looked = body.messages.some(m => m.role === "tool");
    await new Promise(r => setTimeout(r, THINK_MS));
    const message = looked ? {role: "assistant", content: "Done."}
      : {role: "assistant", content: "Looking around.", tool_calls: [{id: "c1", type: "function", function: {name: "doom__look", arguments: badArgs ? '{"x": ' : "{}"}}]};
    return route.fulfill({json: {choices: [{message, finish_reason: looked ? "stop" : "tool_calls"}],
                                 usage: {prompt_tokens: 1, completion_tokens: 1}}, headers: CORS});
  };
}
const OPENAI_MODELS = [{id: "gpt-5.6-sol", created: 3}, {id: "gpt-5.6-luna", created: 2}, {id: "gpt-image-2", created: 1}];
const OPENROUTER_MODELS = [{id: "anthropic/claude-sonnet-5.5", supported_parameters: ["tools"]},
                           {id: "some/no-tools-model", supported_parameters: []}];

// The visitor's settings (ai.js's localStorage): their model, and Connect AI
const CONNECT_AI = {user: "test@example.com", pat: "test", url: ""};
const settings = (provider, extra = {}) => ({provider, keys: {[provider]: `${provider}-test-key`}, models: {}, connectAI: CONNECT_AI, ...extra});
const READY = settings("anthropic");

// ---------- the harness ----------
const results = [];
const tests = [];
const test = (name, run) => tests.push({name, run});

async function open(browser, t, {claude = "plan", openaiBadArgs = false, connectAI = "ok", seed = null, viewport = {width: 1440, height: 900},
                                 mobile = false, path = "", wait = true, reducedMotion = "no-preference"} = {}) {
  const ctx = await browser.newContext({viewport, reducedMotion, ...(mobile ? {isMobile: true, hasTouch: true} : {})});
  t.contexts.push(ctx);
  const p = await ctx.newPage();
  p.on("pageerror", e => t.check(false, `no page error (${e.message})`));
  await p.route("https://plausible.joekarlsson.io/**", r => r.fulfill({status: 204, body: ""}));
  await p.route("https://api.anthropic.com/**", fakeClaude(claude));
  await p.route("https://api.openai.com/**", fakeChat(OPENAI_MODELS, {badArgs: openaiBadArgs}));
  await p.route("https://openrouter.ai/**", fakeChat(OPENROUTER_MODELS));
  p.connectAICalls = [];   // the tools/call requests Connect AI got
  await p.route("https://mcp.cloud.cdata.com/**", fakeConnectAI(connectAI, p.connectAICalls));
  if (seed) await p.addInitScript(s => {   // once: the page's own changes stay across its reloads
    if (sessionStorage.getItem("seeded")) return;
    sessionStorage.setItem("seeded", "1");
    localStorage.setItem("connect-doom-ai", s);
  }, JSON.stringify(seed));
  await p.goto(SITE + path);
  if (wait) await started(p);
  return p;
}
const started = p => p.waitForSelector("#coach-intro [data-go=play]:not([disabled])", {timeout: 120000});
// the game's own state.json, and the panel's /state.json (what web.js serves it)
// (the game writes state.json a moment after it starts: wait for it)
const game = p => p.waitForFunction(() => { try { return JSON.parse(Module.FS.readFile("/doom/agent/state.json", {encoding: "utf8"})); }
                                             catch (e) { return false; } }, undefined, {timeout: 20000, polling: 200}).then(h => h.jsonValue());
const panel = p => p.evaluate(async () => (await fetch("/state.json")).json());
const text = (p, sel) => p.textContent(sel).then(t => (t || "").replace(/\s+/g, " ").trim());
// a condition in the page (synchronous: waitForFunction takes a Promise for true), or one checked from here
const until = (p, fn, arg, timeout = 15000) => p.waitForFunction(fn, arg, {timeout, polling: 200});
async function eventually(check, what, timeout = 15000) {
  for (const end = Date.now() + timeout; Date.now() < end; await new Promise(r => setTimeout(r, 200)))
    if (await check()) return;
  throw new Error(`timed out waiting for ${what}`);
}
// the tip says `what`: a RegExp, or text to find as it is
const tipSays = (p, what, timeout) => until(p, ([src, words]) => {
  const tip = document.getElementById("coach-tip").textContent;
  return src === null ? tip.includes(words) : new RegExp(src).test(tip);
}, what instanceof RegExp ? [what.source, null] : [null, what], timeout);
const gameSays = (p, fn, timeout = 15000) => until(p, `(() => { try { const s = JSON.parse(Module.FS.readFile("/doom/agent/state.json", {encoding: "utf8"})); return (${fn})(s); } catch (e) { return false; } })()`, undefined, timeout);
const settingsOpen = p => p.evaluate(() => !!document.getElementById("ai-settings")?.open);
const builderOpen = p => p.evaluate(() => !!document.getElementById("ai-build")?.open);
const saved = p => p.evaluate(() => JSON.parse(localStorage.getItem("connect-doom-ai") || "null"));
// The game holds the mouse (pointer lock) while the visitor plays; Esc frees it in a browser, which
// Chromium on Linux grants and headless Chromium on a Mac doesn't. Freed, the page's buttons take clicks.
const freeMouse = p => p.evaluate(() => document.exitPointerLock());
// take the marine from offline Claude: a click on the game, as a visitor does
async function takeOver(p) {
  if (await p.isVisible("#coach-intro")) await p.click("#coach-intro [data-go=play]");
  else await p.click("#canvas");
  await gameSays(p, s => s.options.claude_plays === 0);
}
// every record killed, as the game reports each kill to the page (psdoom's kill command)
async function killAll(p) {
  const pids = (await game(p)).monsters.map(m => m.pid);
  return p.evaluate(pids => pids.map(pid => Module.bridge(`kill ${pid}`)).at(-1), pids);
}

// ---------- loading, and the start card ----------
test("load: offline Claude plays behind the start card, in god mode", async (t, b) => {
  const p = await open(b, t);
  await gameSays(p, s => s.options.claude_plays === 1, 20000);
  const s = await game(p);
  t.check(s.options.model === "offline-claude", "offline Claude is who plays");
  t.check(s.options.god_mode === 1, "god mode is on from the start");
  t.check(s.monsters.length === 45, `the site's level has its 45 records (${s.monsters.length})`);
  t.check(/Connect AI/.test(await p.title()) && !/chocolate|psdoom/i.test(await p.title()), `the tab keeps the page's title (${await p.title()})`);
  t.check(await text(p, "#coach-intro [data-go=play]") === "PLAY", "the start card's PLAY button is ready");
  t.check(await text(p, "#coach-intro [data-go=watch]") === "WATCH CLAUDE PLAY", "the start card has WATCH CLAUDE PLAY");
  t.check(/Bring your own AI/.test(await text(p, "#coach-byo")), "with nothing set up, the card offers bring your own AI");
  t.check(/OFFLINE/.test(await text(p, "#ai-link")), "the header's AI SETTINGS says offline");
  t.check(/demo company/.test(await text(p, "#coach-intro p")), "the card explains the site's level");
  await p.mouse.move(5, 5);   // off the card: it fades on its own
  await until(p, () => document.getElementById("coach-intro").hidden, undefined, 20000);
  t.check(true, "the start card fades by itself after a few seconds");
  t.check((await game(p)).options.claude_plays === 1, "Claude plays on once the card is gone");
});

test("start card: WATCH CLAUDE PLAY hides the card with the pointer still on it", async (t, b) => {
  const p = await open(b, t);
  await p.click("#coach-intro [data-go=watch]");   // the pointer stays on the button
  await until(p, () => document.getElementById("coach-intro").hidden, undefined, 3000);
  t.check(true, "the card is gone within 3 s");
  await gameSays(p, s => s.options.claude_plays === 1);
  t.check(true, "Claude keeps playing");
  t.check(/WATCHING CLAUDE PLAY/.test(await text(p, "#coach-tip")), "the tip says Claude is playing");
});

test("start card: PLAY hands the marine to the visitor", async (t, b) => {
  const p = await open(b, t);
  await p.click("#coach-intro [data-go=play]");
  await gameSays(p, s => s.options.claude_plays === 0);
  t.check(!(await p.isVisible("#coach-intro")), "the card is gone");
  t.check(/STEP 1 OF 3/.test(await text(p, "#coach-tip")), "step 1's tip: grab your keys");
  t.check((await panel(p)).game.mode === "player", "the panel says the visitor plays");
  // the controls: W walks forward
  const before = (await game(p)).player;
  await p.keyboard.down("w");
  await p.waitForTimeout(1200);
  await p.keyboard.up("w");
  const after = (await game(p)).player;
  t.check(before.x !== after.x || before.y !== after.y, `W moves the marine (${before.x},${before.y} → ${after.x},${after.y})`);
});

test("start card: a click beside it goes back to the game", async (t, b) => {
  const p = await open(b, t);
  const box = await p.locator("#coach-intro").boundingBox();
  await p.mouse.click(box.x + 8, box.y + box.height / 2);   // the card's backdrop, left of the card
  await until(p, () => document.getElementById("coach-intro").hidden, undefined, 3000);
  t.check(true, "the card is gone");
});

// ---------- playing ----------
test("take over: a click on the game while Claude plays, keycards and all", async (t, b) => {
  const p = await open(b, t);
  await p.click("#coach-intro [data-go=watch]");
  await gameSays(p, s => s.player.keys.blue || s.player.keys.yellow, 90000);   // Claude got a keycard
  await p.waitForTimeout(500);
  await p.click("#canvas");
  await gameSays(p, s => s.options.claude_plays === 0);
  t.check(true, "Claude stops, the visitor plays");
  // with every keycard the level needs, step 2's tip comes up; with some, step 1's stays
  await tipSays(p, /STEP [12] OF 3/);
  t.check(true, `the steps' tip follows the keycards (${await text(p, "#coach-tip b")})`);
});

test("clearing the level: step 3, the red door, and the end card", async (t, b) => {
  const p = await open(b, t);
  await takeOver(p);
  const last = await killAll(p);
  t.check(/ALL RECORDS CLEARED! NOW TRY THE RED DOOR/.test(last), `the last kill's message (${last.trim()})`);
  const s = await panel(p);
  t.check(s.kill_total === 45 && s.standing === 0, `the panel counts every kill (${s.kill_total})`);
  await tipSays(p, /STEP 3 OF 3/);
  t.check(true, "step 3's tip: try the red door");
  t.check(!/Nothing yet/.test(await text(p, "#events")), "the panel lists what just happened");
  await p.evaluate(() => Module.bridge("vault"));
  await tipSays(p, /CONNECT AI SAID NO/);
  t.check((await panel(p)).vault.length === 1, "the red door's try shows in the panel");
  await p.waitForSelector("#coach-end:not([hidden])", {timeout: 10000});
  t.check(/LEVEL CLEAR · 45\/45 RECORDS/.test(await text(p, "#coach-end")), "the end card counts the records");
  const signup = p.locator("#coach-end a", {hasText: "TRY CONNECT AI FREE"});
  t.check(await signup.getAttribute("href") === "https://cloud.cdata.com/" && await signup.getAttribute("target") === "_blank",
          "TRY CONNECT AI FREE opens the sign-up in a new tab");
  t.check(await p.locator("#coach-end a", {hasText: "HOW IT WORKS"}).getAttribute("href") === "about.html", "HOW IT WORKS links the about page");
  t.check(/github\.com/.test(await p.locator("#coach-end a", {hasText: "GITHUB"}).getAttribute("href")), "GITHUB links the code");
  const reload = p.waitForEvent("load");
  await p.click("#coach-end [data-go=again]");
  await reload;
  await started(p);
  t.check((await panel(p)).kill_total === 0, "PLAY AGAIN starts over");
});

test("a minute into step 2 the end card offers a break, and KEEP PLAYING plays on", async (t, b) => {
  const p = await open(b, t);
  await p.click("#coach-intro [data-go=watch]");
  await gameSays(p, s => s.player.keys.blue && s.player.keys.yellow, 120000);   // Claude has every keycard
  await p.waitForTimeout(500);
  await p.click("#canvas");
  await tipSays(p, /STEP 2 OF 3/);
  const pid = (await game(p)).monsters[0].pid;
  await p.evaluate(pid => Module.bridge(`kill ${pid}`), pid);
  await p.waitForSelector("#coach-end:not([hidden])", {timeout: 75000});
  t.check(/SO FAR · 1\/45 RECORDS/.test(await text(p, "#coach-end")), "the card counts the records read so far");
  t.check(await p.isVisible("#coach-end a:has-text('TRY CONNECT AI FREE')"), "with the sign-up on it");
  const tic = () => game(p).then(s => s.tic);
  const paused = await tic();
  await p.waitForTimeout(1000);
  t.check(await tic() === paused, "the game waits while the card is up");
  await p.click("#coach-end [data-go=resume]");
  t.check(await p.isHidden("#coach-end"), "KEEP PLAYING closes the card");
  await p.waitForTimeout(1000);
  t.check(await tic() !== paused, "and the game runs again");
});

test("clearing the level without trying the red door still ends", async (t, b) => {
  const p = await open(b, t);
  await takeOver(p);
  await killAll(p);
  await p.waitForSelector("#coach-end:not([hidden])", {timeout: 40000});
  t.check(true, "the end card comes up about 30 s later");
});

test("dying: the level restarts, every record back", async (t, b) => {
  const p = await open(b, t);
  await takeOver(p);
  await p.evaluate(() => window.ConnectDoomReplay.send("OPTION 2 0"));   // god mode off
  await gameSays(p, s => s.options.god_mode === 0);
  await p.evaluate(pid => Module.bridge(`kill ${pid}`), (await game(p)).monsters[0].pid);
  await p.keyboard.down("w");   // off the start
  await p.waitForTimeout(1200);
  await p.keyboard.up("w");
  const moved = (await game(p)).player;
  // the marine dies: state.json says health 0 until the page sends RESTART
  await p.evaluate(() => {
    const read = Module.FS.readFile, send = window.ConnectDoomReplay.send;
    window.restarts = 0;
    Module.FS.readFile = (path, o) => {
      const r = read(path, o);
      if (window.restarts || path !== "/doom/agent/state.json") return r;
      const s = JSON.parse(r); s.player.health = 0; return JSON.stringify(s);
    };
    window.ConnectDoomReplay.send = line => { if (line === "RESTART") window.restarts++; return send(line); };
  });
  await tipSays(p, /YOU DIED · BACK TO THE START/, 8000);
  t.check((await panel(p)).kill_total === 0, "the panel's kill count starts over at once");
  await until(p, () => window.restarts === 1, undefined, 8000);
  await gameSays(p, s => s.player.health === 100);
  await p.waitForTimeout(1000);
  const s = await game(p);
  t.check(await p.evaluate(() => window.restarts) === 1, "a few seconds later, the page restarts the level, once");
  t.check(s.player.health === 100 && s.monsters.every(m => m.health > 0), "the marine is back at full health, every record alive");
  t.check(s.player.x !== moved.x || s.player.y !== moved.y, `the marine is back at the start (${moved.x},${moved.y} → ${s.player.x},${s.player.y})`);
  t.check((await panel(p)).kill_total === 0, "the panel's kill count starts over");
  t.check(/YOU DIED/.test(await text(p, "#coach-tip")), "the tip says what happened");
});

test("dying for real, then E at once: back in a fresh level", async (t, b) => {
  const p = await open(b, t);
  await p.click("#coach-intro [data-go=watch]");
  await eventually(async () => (await panel(p)).kill_total >= 1, "Claude in a room", 150000);   // among the monsters
  await p.click("#canvas");
  await gameSays(p, s => s.options.claude_plays === 0);
  await p.evaluate(() => window.ConnectDoomReplay.send("OPTION 2 0"));
  await gameSays(p, s => s.player.health <= 0, 180000);   // standing still, god mode off
  t.check(true, "the monsters kill the marine");
  await tipSays(p, /YOU DIED/, 2000);
  t.check((await panel(p)).kill_total === 0, "the page starts the level over at once");
  await p.keyboard.down("e");   // use, held as a person does (Doom reads keys once a tic): the marine respawns
  await p.waitForTimeout(150);
  await p.keyboard.up("e");
  await gameSays(p, s => s.player.health === 100, 2500);
  await gameSays(p, s => s.monsters.length === 45 && s.monsters.every(m => m.health > 0), 5000);
  t.check(true, "the marine is back, with every record");
});

test("the in-game Options menu: Esc > Options > God mode", async (t, b) => {
  const p = await open(b, t);
  await takeOver(p);
  // Esc, down to Options, in; down past Claude plays and the Model row to God mode
  for (const key of ["Escape", "ArrowDown", "Enter", "ArrowDown", "ArrowDown", "Enter"]) { await p.keyboard.press(key); await p.waitForTimeout(300); }
  await gameSays(p, s => s.options.god_mode === 0);
  t.check(true, "God mode turns off from the menu");
  await p.keyboard.press("Enter");
  await gameSays(p, s => s.options.god_mode === 1);
  t.check(true, "and back on");
});

// ---------- the AI settings ----------
test("AI settings: the game's switches", async (t, b) => {
  const p = await open(b, t);
  await p.click("#coach-intro [data-go=watch]");
  await gameSays(p, s => s.options.claude_plays === 1);
  await p.click("#ai-link");
  t.check(await settingsOpen(p), "AI SETTINGS opens the dialog");
  const sw = n => p.locator(`#ai-settings [data-option="${n}"]`);
  await until(p, () => !document.getElementById("game-switches").hidden &&
                       [...document.querySelectorAll("#ai-settings [data-option]")].every(b => b.textContent === "ON"));
  t.check(true, "God mode and Claude plays show ON");
  t.check(!(await p.isVisible("#game-model")), "no Model row with only offline Claude");
  await sw(2).click();
  await gameSays(p, s => s.options.god_mode === 0);
  await until(p, () => document.querySelector('#ai-settings [data-option="2"]').textContent === "OFF");
  t.check(true, "God mode switches off, in the game too");
  await sw(2).click();
  await gameSays(p, s => s.options.god_mode === 1);
  await until(p, () => document.querySelector('#ai-settings [data-option="2"]').textContent === "ON");
  await sw(0).click();
  t.check(await sw(0).textContent() === "OFF", "a switch shows its flip at once");
  await gameSays(p, s => s.options.claude_plays === 0);
  await eventually(async () => (await panel(p)).game.mode === "player", "the visitor's turn", 5000);
  t.check(await sw(0).textContent() === "OFF", "Claude plays off: the visitor plays, and the switch stays OFF");
  await freeMouse(p);
  await sw(0).click();
  await gameSays(p, s => s.options.claude_plays === 1);
  t.check(!(await settingsOpen(p)), "Claude plays on: the dialog closes to watch");
  await p.click("#ai-link");
  await p.click("#ai-settings [data-close]");
  t.check(!(await settingsOpen(p)), "CLOSE closes it");
  await p.click("#ai-link");
  await p.keyboard.press("Escape");
  t.check(!(await settingsOpen(p)), "Esc closes it");
  await p.click("#ai-link");
  await p.mouse.click(5, 5);   // the backdrop
  t.check(!(await settingsOpen(p)), "a click outside closes it");
});

test("AI settings: each provider's key, checked, kept and forgotten", async (t, b) => {
  const p = await open(b, t);
  await p.click("#coach-intro [data-go=settings]");
  t.check(await settingsOpen(p), "the start card's AI SETTINGS opens the dialog");
  const providers = [["anthropic", "console.anthropic.com", "sk-ant-…", "claude-sonnet-5-5", 2],
                     ["openai", "platform.openai.com", "sk-…", "gpt-5.6-sol", 2],
                     ["openrouter", "openrouter.ai", "sk-or-…", "anthropic/claude-sonnet-5.5", 1]];
  for (const [id, keySite, placeholder, model, count] of providers) {
    await p.click(`#ai-settings [data-provider=${id}]`);
    t.check(await p.getAttribute(`#ai-settings [data-provider=${id}]`, "aria-pressed") === "true", `${id}: its button is picked`);
    t.check((await p.getAttribute("#ai-get-key", "href")).includes(keySite) && await p.getAttribute("#ai-key", "placeholder") === placeholder,
            `${id}: the key field and the Get a key link are its own`);
    await p.fill("#ai-key", `${id}-key`);
    await p.press("#ai-key", "Tab");   // a change checks the key
    await until(p, () => /The key works/.test(document.getElementById("ai-msg").textContent));
    t.check((await text(p, "#ai-msg")).includes(`${count} models`), `${id}: the key works (${await text(p, "#ai-msg")})`);
    t.check(await p.inputValue("#ai-model") === model, `${id}: its model is picked (${await p.inputValue("#ai-model")})`);
    t.check((await saved(p)).keys[id] === `${id}-key`, `${id}: the key is kept in this browser`);
  }
  t.check(!(await p.locator("#ai-model option", {hasText: "no-tools"}).count()), "OpenRouter lists only models with tools");
  await p.click("#ai-check");
  await until(p, () => /The key works/.test(document.getElementById("ai-msg").textContent));
  t.check(true, "CHECK KEY checks it again");
  await p.click("#ai-settings [data-provider=openai]");
  await p.selectOption("#ai-model", "gpt-5.6-luna");
  t.check((await saved(p)).models.openai === "gpt-5.6-luna", "a model picked from the list is kept");
  t.check(/GPT-5.6-LUNA/.test(await text(p, "#ai-link")), "the header names the model now");
  await p.fill("#ai-key", "");
  await p.press("#ai-key", "Tab");
  t.check(/Key removed/.test(await text(p, "#ai-msg")) && !(await saved(p)).keys.openai, "an emptied key is removed");
  await p.click("#ai-check");
  t.check(/Paste a key first/.test(await text(p, "#ai-msg")), "CHECK KEY with no key asks for one");
  await p.click("#ai-settings [data-provider=offline]");
  t.check(/OFFLINE/.test(await text(p, "#ai-link")) && await p.isHidden("#ai-provider"), "OFFLINE: no model, no key field");
  // kept across a reload
  await p.click("#ai-settings [data-provider=anthropic]");
  await p.reload();
  await started(p);
  t.check(/CLAUDE-SONNET-5-5/.test(await text(p, "#ai-link")), "the settings are still there after a reload");
  await p.click("#ai-link");
  await p.click("#ai-forget");
  t.check(/Forgotten/.test(await text(p, "#ai-msg")) && /OFFLINE/.test(await text(p, "#ai-link")), "FORGET MY KEYS goes back to offline");
  const left = await saved(p);
  t.check(!left || !Object.keys(left.keys || {}).length, "and nothing of the keys stays in the browser");
});

test("AI settings: a refused key says why", async (t, b) => {
  const p = await open(b, t, {claude: "refuse"});
  await p.click("#ai-link");
  await p.click("#ai-settings [data-provider=anthropic]");
  await p.fill("#ai-key", "sk-ant-bad");
  await p.press("#ai-key", "Tab");
  await until(p, () => /HTTP 401/.test(document.getElementById("ai-msg").textContent));
  t.check(/invalid x-api-key/.test(await text(p, "#ai-msg")), `the provider's own words (${await text(p, "#ai-msg")})`);
});

test("AI settings: Connect AI, checked; set up, the builder opens", async (t, b) => {
  const p = await open(b, t, {seed: settings("anthropic", {connectAI: {user: "", pat: "", url: ""}})});
  t.check(/Almost there/.test(await text(p, "#coach-byo")), "half set up, the card says what's missing");
  await p.click("#coach-intro [data-go=settings]");
  await p.click("#cai-check");
  t.check(/Add your Connect AI email and token first/.test(await text(p, "#cai-msg")), "CHECK with nothing asks for the email and token");
  await p.fill("#cai-user", "test@example.com");
  await p.fill("#cai-pat", "test");
  await p.fill("#cai-url", "http://example.com/mcp");
  await p.click("#cai-check");
  t.check(/starts with https/.test(await text(p, "#cai-msg")), "a URL that isn't https:// is turned down");
  await p.fill("#cai-url", "https://mcp.cloud.cdata.com/mcp");
  await p.click("#cai-check");
  await until(p, () => /Connected/.test(document.getElementById("cai-msg").textContent));
  t.check(/2 read-only tools, 3 connections, Salesforce, Zendesk and Stripe/.test(await text(p, "#cai-msg")), `CHECK lists the connections (${await text(p, "#cai-msg")})`);
  t.check(/Ready/.test(await text(p, "#ai-status")) && await p.isVisible("#ai-next"), "the dialog says it's ready to build");
  t.check((await saved(p)).connectAI.user === "test@example.com" && (await saved(p)).connectAI.url === "", "Connect AI's own URL is kept as the default");
  await p.click("#ai-settings [data-close]");
  await until(p, () => document.getElementById("ai-build")?.open);
  t.check(true, "closing the settings, set up just now, opens the builder");
  t.check(/BUILD MY LEVEL/.test(await text(p, "#coach-byo")), "the start card offers BUILD MY LEVEL");
  await p.click("#ai-build [data-close]");
  await p.click("#ai-link");
  await p.click("#ai-next");
  t.check(await builderOpen(p), "BUILD A LEVEL FROM YOUR DATA in the settings opens the builder");
});

test("AI settings: Connect AI turning the token down", async (t, b) => {
  const p = await open(b, t, {connectAI: "deny"});
  await p.click("#ai-link");
  await p.fill("#cai-user", "test@example.com");
  await p.fill("#cai-pat", "wrong");
  await p.click("#cai-check");
  await until(p, () => /\bbad\b/.test(document.getElementById("cai-msg").className), undefined, 30000);
  t.check(/401|token|sign/i.test(await text(p, "#cai-msg")), `it says why (${await text(p, "#cai-msg")})`);
});

// ---------- the visitor's own model, playing ----------
for (const [provider, name] of [["anthropic", "CLAUDE-SONNET-5-5"], ["openai", "GPT-5.6-SOL"], ["openrouter", "CLAUDE-SONNET-5.5"]]) {
  test(`watch: the visitor's own model plays (${provider})`, async (t, b) => {
    const p = await open(b, t, {seed: settings(provider)});
    t.check(await text(p, "#coach-intro [data-go=watch]") === `WATCH ${name} PLAY`, `the Watch button names it (${name})`);
    await p.click("#coach-intro [data-go=watch]");
    await tipSays(p, `WATCHING ${name} PLAY`, 5000);
    await gameSays(p, s => s.options.claude_plays === 1);
    t.check(true, "it plays");
    await until(p, () => !document.getElementById("play").hidden);
    t.check(await text(p, "#s-model") === "SONNET-5-5", `BUILT BY names who built the level, not who plays (${await text(p, "#s-model")})`);
    const [who, title] = await p.evaluate(() => [document.getElementById("play-who"), document.getElementById("play-who").parentElement]
      .map(e => { const r = e.getBoundingClientRect(); return {left: Math.round(r.left), top: Math.round(r.top)}; }));
    t.check(who.left === title.left && Math.abs(who.top - title.top) <= 2,
            `the feed's heading reads as one title, the player's name first (${JSON.stringify([who, title])})`);
    await tipSays(p, /IS DONE/, 30000);
    t.check(/Your turn/.test(await text(p, "#coach-tip")), "done, it hands the marine back");
    await gameSays(p, s => s.options.claude_plays === 0);
    const feed = (await panel(p)).play_events.map(e => e.kind);
    t.check(feed.includes("call") && feed.includes("done"), `its moves are in the panel's feed (${[...new Set(feed)]})`);
    // the Model row: offline Claude or the visitor's model
    await freeMouse(p);
    await p.click("#ai-link");
    t.check(await p.isVisible("#game-model") && await p.locator("#game-model option").count() === 2, "the Model row offers both");
    await p.selectOption("#game-model", "0");
    await gameSays(p, s => s.options.model === "offline-claude");
    t.check(true, "picking offline Claude in the Model row switches the game's model");
  });
}

test("watch: a model's tool call that isn't JSON comes back to it as an error", async (t, b) => {
  const p = await open(b, t, {openaiBadArgs: true, seed: settings("openai")});
  await p.click("#coach-intro [data-go=watch]");
  await tipSays(p, /IS DONE/, 30000);
  const result = (await panel(p)).play_events.find(e => e.kind === "result");
  t.check(result?.error && /aren't valid JSON/.test(result.text), `the model is told, and can try again (${result?.text})`);
});

test("watch: the visitor's model reads records through Connect AI as it plays, and a click takes over", async (t, b) => {
  const p = await open(b, t, {claude: "query", seed: READY});
  await p.click("#coach-intro [data-go=watch]");
  await eventually(async () => (await panel(p)).play_events.some(e => e.kind === "result" && e.server !== "doom" && e.rows > 0),
                   "a Connect AI result in the feed", 30000);
  const result = (await panel(p)).play_events.find(e => e.kind === "result" && e.tool === "queryData");
  t.check(result && !result.error && result.rows === 3, `its query comes back with Connect AI's rows (${result?.rows} rows)`);
  t.check(p.connectAICalls.some(c => c.name === "queryData" && c.arguments.query === QUERY.query), "the query went to Connect AI as the model wrote it");
  // still playing (it thinks THINK_MS a move): a click on the game takes the marine
  const asked = () => p.evaluate(() => performance.getEntriesByType("resource").filter(r => r.name.includes("api.anthropic.com/v1/messages")).length);
  await p.click("#canvas");
  await gameSays(p, s => s.options.claude_plays === 0);
  await tipSays(p, /STEP [12] OF 3/);
  t.check(true, "a click on the game takes over from the visitor's model");
  const before = await asked();
  await p.waitForTimeout(2 * THINK_MS + 500);
  t.check(await asked() <= before + 1, "and its run stops: no more turns asked of the model");
});

test("watch: a provider overloaded at first is asked again", async (t, b) => {
  const p = await open(b, t, {claude: "busy", seed: settings("anthropic")});
  await p.click("#coach-intro [data-go=watch]");
  await tipSays(p, /IS DONE/, 30000);
  t.check(!/COULDN'T PLAY/.test(await text(p, "#coach-tip")), "it plays once the provider answers (the retry waits 2 s)");
});

test("watch: a model that turns the key down hands the game to offline Claude", async (t, b) => {
  const p = await open(b, t, {claude: "refuse", seed: settings("anthropic")});
  await p.click("[data-go=watch]");
  await tipSays(p, /COULDN'T PLAY/, 30000);
  t.check(/Offline Claude takes over/.test(await text(p, "#coach-tip")), "offline Claude takes over");
  await gameSays(p, s => s.options.claude_plays === 1);
  t.check(true, "and plays");
});

// ---------- building a level from the visitor's data ----------
test("builder: not set up, it points at the settings", async (t, b) => {
  const p = await open(b, t);
  await p.click("#build-link");
  t.check(await builderOpen(p), "the header's BUILD A LEVEL opens the builder");
  t.check(await p.isVisible("#build-setup") && await p.isHidden("#build-form"), "with nothing set up, it shows what's needed");
  t.check((await p.locator("#build-checklist li.todo").count()) === 2, "both steps are still to do");
  await p.click("#ai-build [data-settings]");
  t.check(!(await builderOpen(p)) && await settingsOpen(p), "OPEN AI SETTINGS swaps the builder for the settings");
});

test("builder: a level from the visitor's data, played, then back to the demo", async (t, b) => {
  const p = await open(b, t, {seed: READY});
  await until(p, () => /BUILD MY LEVEL/.test(document.getElementById("coach-byo").textContent));
  await p.click("#coach-byo [data-go=build]");
  t.check(/at risk/.test(await p.inputValue("#build-q")), "it suggests a question from the connections");
  const ideas = p.locator("#build-ideas [data-idea]");
  t.check(await ideas.count() >= 2, `it offers a few questions (${await ideas.count()})`);
  await ideas.nth(1).click();
  t.check(await p.inputValue("#build-q") === await ideas.nth(1).getAttribute("data-idea"), "a suggested question fills the box");
  await p.fill("#build-q", "");
  await p.click("#build-go");
  t.check(/Type a question first/.test(await text(p, "#build-msg")), "an empty question is turned down");
  await p.fill("#build-q", "Which customers are at risk?");
  const reloaded = p.waitForEvent("load", {timeout: 180000});
  await p.click("#build-go");
  await until(p, () => document.body.classList.contains("building"), undefined, 10000);
  t.check(true, "the building screen shows");
  const failed = until(p, () => /Couldn't/.test(document.getElementById("build-msg")?.textContent || ""), undefined, 180000)
    .then(() => p.textContent("#build-msg"));
  const why = await Promise.race([reloaded.then(() => null), failed]);
  if (why) throw new Error(`the level didn't build: ${why}`);
  await started(p);
  t.check(/read your Salesforce, Zendesk and Stripe/.test(await text(p, "#coach-intro p")), "the page plays the visitor's level");
  t.check(p.connectAICalls.some(c => c.name === "queryData" && c.arguments.query === QUERY.query), "the model read records through Connect AI to plan it");
  t.check(/Acme Corp is at risk/.test(await text(p, "#coach-intro p.what")), "its start card has the model's answer");
  await p.waitForTimeout(3000);
  const s = await panel(p);
  t.check(s.rooms_live.reduce((n, r) => n + r.left + r.killed, 0) === 9 && (await game(p)).monsters.length > 0,
          "a monster for each record: 3 rooms × 3 rows");
  t.check(!(await p.isVisible("#tabs [data-tab=board]")), "no leaderboard on the visitor's level");
  await p.click("#coach-byo [data-go=build]", {force: true});
  t.check(await builderOpen(p), "NEW LEVEL opens the builder");
  await p.click("#ai-build [data-close]");
  // still theirs after a reload (IndexedDB)
  await p.reload();
  await started(p);
  t.check(/read your Salesforce/.test(await text(p, "#coach-intro p")), "the visitor's level stays after a reload");
  const back = p.waitForEvent("load", {timeout: 60000});
  await p.click("#coach-byo [data-go=demo]", {force: true});
  await back;
  await started(p);
  t.check(/demo company/.test(await text(p, "#coach-intro p")), "DEMO LEVEL goes back to the site's level");
});

test("builder: a model that keeps calling Connect AI is stopped at 40 calls, and the level still builds", async (t, b) => {
  const p = await open(b, t, {claude: "greedy", seed: READY});
  await p.click("#build-link");
  const reloaded = p.waitForEvent("load", {timeout: 180000});
  await p.click("#build-go");
  await reloaded;
  await started(p);
  const queries = p.connectAICalls.filter(c => c.name === "queryData" && c.arguments.query === QUERY.query).length;
  t.check(queries === 40, `the briefing's queries stop at the cap (${queries}), then the records' own`);
  t.check(/read your Salesforce, Zendesk and Stripe/.test(await text(p, "#coach-intro p")), "the level builds from what it found");
});

test("builder: a model that won't plan says why, and the visitor can try again", async (t, b) => {
  const p = await open(b, t, {claude: "badplan", seed: READY});
  await p.click("#build-link");
  await p.click("#build-go");
  await until(p, () => /Couldn't build it/.test(document.getElementById("build-msg").textContent), undefined, 120000);
  t.check(await builderOpen(p) && !(await p.evaluate(() => document.body.classList.contains("building"))), "the builder comes back, the game too");
  t.check(!(await p.isDisabled("#build-go")), "BUILD MY LEVEL can be tried again");
});

// ---------- the panel ----------
test("panel: tabs, the leaderboard, and the live feed", async (t, b) => {
  const p = await open(b, t);
  await p.click("#tabs [data-tab=board]");
  t.check(await p.isVisible("#board") && await p.locator("#board-rows > *").count() > 0, "LEADERBOARD shows the models' runs");
  await p.click("#tabs [data-tab=live]");
  await until(p, () => document.body.classList.contains("in-game"), undefined, 20000);
  t.check(await p.isVisible("#rooms-live"), "LIVE FEED shows the rooms");
  for (const app of ["Salesforce", "Zendesk", "Stripe"])
    t.check((await text(p, "#rooms-live")).includes(app), `the ${app} room is listed`);
  await p.click("#coach-intro [data-go=watch]");
  await until(p, () => !document.getElementById("tab-brief").hidden, undefined, 20000);
  await p.click("#tab-brief");
  t.check(await p.evaluate(() => document.body.classList.contains("tab-brief")) && await p.isVisible("#feed"),
          "HOW CLAUDE BUILT IT shows the briefing's Connect AI calls");
  t.check(await p.locator("#feed > :not(.empty)").count() > 0, "with the calls in it");
  t.check(await p.isHidden("#newq"), "no question box with nothing set up");
});

test("panel: the question box builds a level", async (t, b) => {
  const p = await open(b, t, {seed: READY});
  await until(p, () => !document.getElementById("newq").hidden && !document.getElementById("newq-ideas").hidden, undefined, 20000);
  t.check(true, "set up, the question box and its suggested questions show");
  await p.locator("#newq").scrollIntoViewIfNeeded();   // the panel scrolls: the box is last
  const box = await p.locator("#newq").boundingBox();
  t.check(box.height > 150 && box.y + box.height <= 900 + 1, `the question box can be reached, at full height (${Math.round(box.y)}, ${Math.round(box.height)}px)`);
  await p.click("#newq-ideas [data-idea]");
  t.check((await p.inputValue("#newq-text")).length > 10, "a suggested question fills the box");
  await p.fill("#newq-text", "");
  await p.click("#newq-default");
  t.check((await p.inputValue("#newq-text")).length > 10, "Use the default question fills it too");
  await p.click("#newq-settings");
  t.check(await settingsOpen(p), "AI settings opens the settings");
  await p.click("#ai-settings [data-close]");
  await p.click("#newq-go");
  await until(p, () => document.body.classList.contains("building") || document.getElementById("ai-build")?.open, undefined, 10000);
  t.check(true, "BUILD MY LEVEL starts the build");
});

// ---------- the pages, the links, phones, and the embed ----------
test("links: every page loads, internal links work, external ones open new tabs", async (t, b) => {
  const p = await open(b, t);
  for (const page of ["", "about.html", "licenses.html", "embed.html"]) {
    if (page) await p.goto(SITE + page);
    const links = await p.evaluate(() => [...document.querySelectorAll("a[href]")].map(a =>
      ({href: a.href, raw: a.getAttribute("href"), target: a.target, rel: a.rel})));
    for (const l of links.filter(l => l.href.startsWith("http") && !l.href.startsWith(SITE)))
      t.check(l.target === "_blank" && /noopener/.test(l.rel), `${page || "index"}: ${l.raw} opens in a new tab`);
    for (const l of links.filter(l => l.href.startsWith(SITE) && !l.raw.startsWith("#"))) {
      const status = await p.evaluate(async u => (await fetch(u)).status, l.href);
      t.check(status === 200, `${page || "index"}: ${l.raw} is there (${status})`);
    }
  }
  await p.goto(SITE);
  await started(p);
  t.check(await p.locator(".weblinks a", {hasText: "BLOG POST"}).getAttribute("href") === "https://www.cdata.com/blog/doom-mcp-server",
          "BLOG POST links the writeup");
  await p.click(".weblinks a[href='about.html']");
  await p.waitForURL(/about\.html/);
  t.check(/Connect AI/.test(await p.title()), `ABOUT opens the about page (${await p.title()})`);
});

test("phones and small windows get a note, not the game", async (t, b) => {
  const phone = await open(b, t, {viewport: {width: 390, height: 844}, mobile: true, wait: false});
  const fetched = [];
  phone.on("request", r => fetched.push(r.url()));
  await phone.waitForSelector("#desktop-only:not([hidden])");
  t.check(/desktop browser/.test(await text(phone, "#desktop-why")), "a phone: play on a computer");
  t.check(await phone.isVisible("#desktop-only a:has-text('BLOG POST')"), "with the blog post to read instead");
  t.check(await phone.isHidden("#desktop-only [data-go=anyway]"), "and no way past it: a phone has no room for the game");
  await phone.waitForTimeout(1500);
  t.check(!fetched.some(u => /psdoom|freedoom2/.test(u)), "and nothing of the game downloads");
  const small = await open(b, t, {viewport: {width: 800, height: 500}, wait: false});
  await small.waitForSelector("#desktop-only:not([hidden])");
  t.check(/bigger window/.test(await text(small, "#desktop-why")), "a small window: make it bigger");
  await small.setViewportSize({width: 1440, height: 900});
  await started(small);
  t.check(await small.isHidden("#desktop-only"), "made bigger, the game starts");
});

test("a desktop that reports no mouse can say it has one and play", async (t, b) => {
  const ctx = await b.newContext({viewport: {width: 1440, height: 900}});
  t.contexts.push(ctx);
  const p = await ctx.newPage();
  p.on("pageerror", e => t.check(false, `no page error (${e.message})`));
  await p.route("https://plausible.joekarlsson.io/**", r => r.fulfill({status: 204, body: ""}));
  await p.addInitScript(() => {   // as a remote desktop reports it: no fine pointer anywhere
    const real = matchMedia.bind(window);
    window.matchMedia = q => q === "(any-pointer: fine)" ? {...real(q), matches: false} : real(q);
  });
  await p.goto(SITE);
  await p.waitForSelector("#desktop-only:not([hidden])");
  t.check(await p.isVisible("#desktop-only [data-go=anyway]"), "the note offers I HAVE A MOUSE: PLAY");
  await p.click("#desktop-only [data-go=anyway]");
  await started(p);
  t.check(await p.isHidden("#desktop-only"), "and the game starts");
});

test("embed: Claude starts playing behind the start card as it loads", async (t, b) => {
  const p = await open(b, t, {viewport: {width: 768, height: 712}, path: "embed.html", wait: false});
  await gameSays(p, s => s.options.claude_plays === 1, 120000);
  t.check(await p.isVisible("#coach-intro"), "with no click, Claude plays behind the start card");
  t.check(!(await p.isVisible("#coach-byo")), "the card has no bring your own AI: the embed plays the site's level");
  await until(p, () => document.getElementById("coach-intro").hidden, undefined, 12000);
  t.check(true, "the card fades after a few seconds");
  t.check(!(await p.isVisible("#side")) && (await p.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--gw"))) === "768px",
          "the game fills the column, with no panel");
  const full = p.locator(".weblinks a");
  t.check(await full.getAttribute("href") === SITE && await full.getAttribute("target") === "_blank", "FULL VERSION opens the site");
  t.check(/Click anywhere to play/.test(await text(p, "#coach-tip")), "the caption says click anywhere to play");
  const tic = () => game(p).then(s => s.tic);
  await p.click(".weblinks .pause");
  const paused = await tic();
  await p.waitForTimeout(1000);
  t.check(await tic() === paused && await text(p, ".weblinks .pause") === "RESUME" && /PAUSED/.test(await text(p, "#coach-tip")),
          "PAUSE stops the game on its frame");
  await p.click(".bar.top", {position: {x: 400, y: 20}});   // the logo strip, off its links
  await gameSays(p, s => s.options.claude_plays === 0);
  t.check(!(await p.evaluate(() => Module.paused)), "a click anywhere, not just on the game, takes over, paused or not");
  const before = (await game(p)).player;
  await p.keyboard.down("ArrowUp"); await p.waitForTimeout(500); await p.keyboard.up("ArrowUp");
  await p.waitForTimeout(1000);   // the marine slides to a stop
  const moved = (await game(p)).player;
  t.check(moved.x !== before.x || moved.y !== before.y, "playing, the arrows move the marine");
  await p.evaluate(() => { document.exitPointerLock(); document.dispatchEvent(new Event("pointerlockchange")); });   // Esc
  t.check(await p.evaluate(() => document.activeElement !== document.getElementById("canvas")), "Esc lets go of the keyboard too");
  await p.keyboard.down("ArrowUp"); await p.waitForTimeout(500); await p.keyboard.up("ArrowUp");
  await p.waitForTimeout(1000);
  const after = (await game(p)).player;
  t.check(after.x === moved.x && after.y === moved.y, "and the arrows no longer move the marine");
});

test("embed: reduced motion, nothing moves until asked", async (t, b) => {
  const p = await open(b, t, {viewport: {width: 768, height: 712}, path: "embed.html", wait: false, reducedMotion: "reduce"});
  await p.waitForSelector("#coach-intro [data-go=play]:not([disabled])", {timeout: 120000});
  await p.waitForTimeout(6000);
  t.check(await p.evaluate(() => Module.paused) && await p.isVisible("#coach-intro"), "the game waits on its first frame, the start card up");
  t.check(await text(p, ".weblinks .pause") === "RESUME", "the button says RESUME");
  await p.click("#coach-intro [data-go=watch]");
  await gameSays(p, s => s.options.claude_plays === 1);
  t.check(!(await p.evaluate(() => Module.paused)) && await p.isHidden("#coach-intro"), "WATCH CLAUDE PLAY starts Claude");
});

test("embed: after Claude clears the level, it plays it again", async (t, b) => {
  const p = await open(b, t, {viewport: {width: 768, height: 712}, path: "embed.html", wait: false});
  await gameSays(p, s => s.options.claude_plays === 1, 120000);
  await killAll(p);
  await p.waitForSelector("#coach-end:not([hidden])", {timeout: 60000});
  t.check(/LEVEL CLEAR/.test(await text(p, "#coach-end")), "Claude's level clear shows");
  await p.mouse.move(400, 20);   // off the card, on the logo strip
  await p.waitForSelector("#coach-end", {state: "hidden", timeout: 30000});
  await gameSays(p, s => s.options.claude_plays === 1 && s.player.kills === 0 && s.monsters.length === 45, 20000);
  t.check(true, "then Claude plays a fresh level, every record back");
});

test("embed: in a blog post, the game leaves the mouse alone until it's clicked", async (t, b) => {
  const p = await open(b, t, {viewport: {width: 1000, height: 800}, path: "about.html", wait: false});
  // the post on another site, as the blog is: the embed's a cross-origin iframe (both public, or
  // Chrome's local network checks block a public post's iframe of 127.0.0.1)
  await p.route("http://game.test/**", async r => r.fulfill({response: await r.fetch({url: r.request().url().replace("http://game.test/", SITE)})}));
  await p.route("http://blog.test/post.html", r => r.fulfill({contentType: "text/html", body: `<!doctype html><body style="margin: 0">
    <div style="height: 600px">The post</div><iframe src="http://game.test/embed.html" width="768" height="712" style="border: 0" allow="fullscreen"></iframe>
    <div style="height: 2000px">More of the post</div></body>`}));
  await p.goto("http://blog.test/post.html");
  const embed = await (await p.waitForSelector("iframe")).contentFrame();
  await p.evaluate(() => scrollTo(0, 400));
  await gameSays(embed, s => s.options.claude_plays === 1, 120000);
  t.check(await p.evaluate(() => scrollY) === 400, "the game starting doesn't scroll the post");
  t.check(await embed.evaluate(() => getComputedStyle(document.getElementById("canvas")).cursor) === "pointer",
          "over the game while Claude plays, the cursor shows");
  await p.mouse.move(500, 500);   // over the game
  await p.mouse.wheel(0, 300);
  await eventually(() => p.evaluate(() => scrollY > 400), "the post to scroll");
  t.check(true, "the wheel over the game scrolls the post");
  const tic = () => game(embed).then(s => s.tic);
  await p.evaluate(() => scrollTo(0, 2500));   // the game out of view
  await until(embed, () => Module.paused);
  const away = await tic();
  await p.waitForTimeout(1500);
  t.check(await tic() === away, "scrolled out of view, the game pauses");
  await p.evaluate(() => scrollTo(0, 400));
  await until(embed, () => !Module.paused);
  await eventually(async () => await tic() > away, "the game to run again");
  t.check(await tic() - away < 35 * 2, "back in view, it carries on, without running the time it was away");
  t.check((await game(embed)).options.claude_plays === 1, "and Claude still plays");
  await embed.click(".weblinks .fullscreen");
  await until(embed, () => !!document.fullscreenElement && document.querySelector(".weblinks .fullscreen").textContent === "EXIT FULLSCREEN");
  t.check(true, "FULLSCREEN fills the screen with the game, and its button says EXIT FULLSCREEN");
  t.check((await game(embed)).options.claude_plays === 1, "and Claude plays on");
  await embed.evaluate(() => document.exitFullscreen());
  await p.evaluate(() => scrollTo(0, 400));
  await p.mouse.click(500, 500);   // on the game: take over
  await gameSays(embed, s => s.options.claude_plays === 0);
  await embed.evaluate(() => { document.exitPointerLock(); document.dispatchEvent(new Event("pointerlockchange")); });   // Esc
  const y = await p.evaluate(() => scrollY);
  await p.keyboard.press("Space");
  await eventually(() => p.evaluate(y => scrollY > y, y), "Space to scroll the post");
  t.check(true, "after Esc, Space scrolls the post again");
});

test("embed: on a phone, a picture of the game", async (t, b) => {
  const p = await open(b, t, {viewport: {width: 390, height: 712}, mobile: true, path: "embed.html", wait: false});
  await p.waitForSelector("#desktop-only:not([hidden]) .shot");
  t.check(await p.evaluate(() => document.querySelector("#desktop-only .shot").naturalWidth > 0), "the screenshot loads");
  t.check(await p.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "nothing wider than the phone");
});

// ---------- run ----------
const server = spawn("python3", ["-m", "http.server", String(PORT), "--bind", "127.0.0.1", "-d", DIST], {stdio: "ignore"});
const browser = await chromium.launch(process.env.CHROMIUM ? {executablePath: process.env.CHROMIUM} : {});
const t0 = Date.now();
try {
  await new Promise(r => setTimeout(r, 1000));
  const queue = tests.filter(x => x.name.includes(ONLY));
  await Promise.all(Array.from({length: JOBS}, async () => {
    for (let x; (x = queue.shift());) {
      const t = {contexts: [], lines: [], failed: 0,
                 check(ok, what) { this.lines.push(`  ${ok ? "ok  " : "FAIL"} ${what}`); if (!ok) this.failed++; }};
      const began = Date.now();
      try { await x.run(t, browser); }
      catch (e) {
        const at = (e.stack || "").split("\n").find(l => l.includes("e2e.mjs"))?.match(/e2e\.mjs:(\d+)/)?.[1];
        // Playwright's call log ends with why it waited: what was covering the element, say
        const why = e.message.split("\n").filter(l => /^\s+-/.test(l)).slice(-3).map(l => l.trim()).join(" | ");
        t.check(false, `threw${at ? ` at e2e.mjs:${at}` : ""}: ${e.message.split("\n")[0]}${why ? ` (${why})` : ""}`);
      }
      finally { await Promise.all(t.contexts.map(c => c.close().catch(() => {}))); }
      console.log(`${t.failed ? "FAIL" : "ok  "} ${x.name} (${Math.round((Date.now() - began) / 1000)} s)\n${t.lines.join("\n")}`);
      results.push({name: x.name, failed: t.failed});
    }
  }));
} finally {
  await browser.close();
  server.kill();
}
const failed = results.filter(r => r.failed);
console.log(`\n${results.length - failed.length} of ${results.length} tests passed in ${Math.round((Date.now() - t0) / 1000)} s` +
            (failed.length ? `; failed: ${failed.map(r => r.name).join("; ")}` : ""));
process.exit(failed.length || !results.length ? 1 : 0);
