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

// ---------- the fakes ----------
// Connect AI: three connections, and the same rows for any query; `deny` refuses the token
const CATALOGS = "TABLE_CATALOG,DATA_SOURCE,DRIVER,PERMISSIONS\nSalesforce1,Salesforce,Salesforce,\"Select, Insert, Update\"\n" +
  "Zendesk1,Zendesk,Zendesk,Select\nStripe1,Stripe,Stripe,Select\n\nQuery ID: c1";
const ROWS = "Id,Label,Detail,Hot\n1,Acme Corp,$45000 overdue,1\n2,Globex,$9000 due,0\n3,Initech,$1200 due,0\n\nQuery ID: q1";
const PLAN = {answer: "Acme Corp is at risk.", rooms: [["Salesforce1", "Salesforce", "Deals slipping"],
  ["Zendesk1", "Zendesk", "Angry tickets"], ["Stripe1", "Stripe", "Unpaid invoices"]]
  .map(([connection, app, title]) => ({connection, app, title, query: `SELECT [Id] FROM [${connection}].[dbo].[t]`}))};

function fakeConnectAI(mode) {
  return async route => {
    if (mode === "deny") return route.fulfill({status: 401, body: "Unauthorized", headers: CORS});
    const body = JSON.parse(route.request().postData());
    const reply = result => route.fulfill({json: {jsonrpc: "2.0", id: body.id, result}, headers: CORS});
    if (!("id" in body)) return route.fulfill({status: 202, body: "", headers: CORS});
    if (body.method === "initialize") return reply({protocolVersion: "2025-06-18", capabilities: {}});
    if (body.method === "tools/list") return reply({tools: ["getCatalogs", "queryData"].map(name =>
      ({name, description: name, inputSchema: {type: "object", properties: {query: {type: "string"}}}}))});
    return reply({content: [{type: "text", text: body.params.name === "getCatalogs" ? CATALOGS : ROWS}]});
  };
}

// Claude: `refuse` turns the key down; `badplan` answers the briefing with prose, not a plan; else it
// plans the level when briefing, and looks around once when playing
function fakeClaude(mode) {
  return async route => {
    if (mode === "refuse") return route.fulfill({status: 401, json: {error: {message: "invalid x-api-key"}}, headers: CORS});
    if (route.request().url().includes("/models")) return route.fulfill({json: {data: [{id: "claude-sonnet-5-5"}, {id: "claude-opus-5-5"}]}, headers: CORS});
    const body = JSON.parse(route.request().postData()), first = body.messages.length === 1;
    const content = /Doom level/.test(body.system || "")
      ? [{type: "text", text: mode === "badplan" ? "I'd rather not plan a level." : JSON.stringify(PLAN)}]
      : first ? [{type: "text", text: "Looking around."}, {type: "tool_use", id: "t1", name: "doom__look", input: {}}]
      : [{type: "text", text: "Done."}];
    return route.fulfill({json: {content, stop_reason: "end_turn", usage: {input_tokens: 1, output_tokens: 1}}, headers: CORS});
  };
}

// OpenAI and OpenRouter (both OpenAI's chat format): the model looks around once, then stops
function fakeChat(models) {
  return async route => {
    const url = route.request().url();
    if (url.endsWith("/key")) return route.fulfill({json: {data: {label: "test"}}, headers: CORS});
    if (url.endsWith("/models")) return route.fulfill({json: {data: models}, headers: CORS});
    const body = JSON.parse(route.request().postData()), looked = body.messages.some(m => m.role === "tool");
    const message = looked ? {role: "assistant", content: "Done."}
      : {role: "assistant", content: "Looking around.", tool_calls: [{id: "c1", type: "function", function: {name: "doom__look", arguments: "{}"}}]};
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

async function open(browser, t, {claude = "plan", connectAI = "ok", seed = null, viewport = {width: 1440, height: 900},
                                 mobile = false, path = "", wait = true} = {}) {
  const ctx = await browser.newContext({viewport, ...(mobile ? {isMobile: true, hasTouch: true} : {})});
  t.contexts.push(ctx);
  const p = await ctx.newPage();
  p.on("pageerror", e => t.check(false, `no page error (${e.message})`));
  await p.route("https://plausible.joekarlsson.io/**", r => r.fulfill({status: 204, body: ""}));
  await p.route("https://api.anthropic.com/**", fakeClaude(claude));
  await p.route("https://api.openai.com/**", fakeChat(OPENAI_MODELS));
  await p.route("https://openrouter.ai/**", fakeChat(OPENROUTER_MODELS));
  await p.route("https://mcp.cloud.cdata.com/**", fakeConnectAI(connectAI));
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
  await p.waitForTimeout(3000);
  const s = await game(p);
  t.check(await p.evaluate(() => window.restarts) === 1, "the page restarts the level once");
  t.check(s.player.health === 100 && s.monsters.every(m => m.health > 0), "the marine is back at full health, every record alive");
  t.check(s.player.x !== moved.x || s.player.y !== moved.y, `the marine is back at the start (${moved.x},${moved.y} → ${s.player.x},${s.player.y})`);
  t.check((await panel(p)).kill_total === 0, "the panel's kill count starts over");
  t.check(/YOU DIED/.test(await text(p, "#coach-tip")), "the tip says what happened");
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
    await tipSays(p, /IS DONE/, 30000);
    t.check(/Your turn/.test(await text(p, "#coach-tip")), "done, it hands the marine back");
    await gameSays(p, s => s.options.claude_plays === 0);
    const feed = (await panel(p)).play_events.map(e => e.kind);
    t.check(feed.includes("call") && feed.includes("done"), `its moves are in the panel's feed (${[...new Set(feed)]})`);
    // the Model row: offline Claude or the visitor's model
    await p.click("#ai-link");
    t.check(await p.isVisible("#game-model") && await p.locator("#game-model option").count() === 2, "the Model row offers both");
    await p.selectOption("#game-model", "0");
    await gameSays(p, s => s.options.model === "offline-claude");
    t.check(true, "picking offline Claude in the Model row switches the game's model");
  });
}

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
  await phone.waitForTimeout(1500);
  t.check(!fetched.some(u => /psdoom|freedoom2/.test(u)), "and nothing of the game downloads");
  const small = await open(b, t, {viewport: {width: 800, height: 500}, wait: false});
  await small.waitForSelector("#desktop-only:not([hidden])");
  t.check(/bigger window/.test(await text(small, "#desktop-why")), "a small window: make it bigger");
  await small.setViewportSize({width: 1440, height: 900});
  await started(small);
  t.check(await small.isHidden("#desktop-only"), "made bigger, the game starts");
});

test("embed: nothing downloads before its click, then Claude plays the game alone", async (t, b) => {
  const p = await open(b, t, {viewport: {width: 768, height: 712}, path: "embed.html", wait: false});
  const fetched = [];
  p.on("request", r => fetched.push(r.url()));
  await p.waitForSelector("#poster button");
  await p.waitForTimeout(1000);
  t.check(!fetched.some(u => /psdoom|freedoom2/.test(u)), "the embed downloads no game before its click");
  await p.click("#poster button");
  await gameSays(p, s => s.options.claude_plays === 1, 120000);
  t.check(!(await p.isVisible("#coach-intro")) && !(await p.isVisible("#poster")), "the click starts Claude playing, no start card");
  t.check(!(await p.isVisible("#side")) && (await p.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--gw"))) === "768px",
          "the game fills the column, with no panel");
  const full = p.locator(".weblinks a");
  t.check(await full.getAttribute("href") === SITE && await full.getAttribute("target") === "_blank", "PLAY THE FULL VERSION opens the site");
  await p.click("#canvas");
  await gameSays(p, s => s.options.claude_plays === 0);
  t.check(true, "a click on the game takes over");
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
        t.check(false, `threw${at ? ` at e2e.mjs:${at}` : ""}: ${e.message.split("\n")[0]}`);
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
