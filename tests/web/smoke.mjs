// The browser version, end to end, in headless Chromium: web/dist (web/build.sh) served locally, with
// Claude's API and the Connect AI MCP server faked, so it needs no keys and no network.
//   cd tests/web && npm ci && npx playwright install chromium && node smoke.mjs
// CHROMIUM=<path> runs a Chromium of your own instead of Playwright's.
import {spawn} from "node:child_process";
import {fileURLToPath} from "node:url";
import {chromium} from "playwright-core";

const DIST = fileURLToPath(new URL("../../web/dist", import.meta.url));
const PORT = Number(process.env.PORT || 8799), SITE = `http://127.0.0.1:${PORT}/`;
const CORS = {"access-control-allow-origin": "*"};

// Connect AI: three connections, and the same rows for any query
const CATALOGS = "TABLE_CATALOG,DATA_SOURCE,DRIVER,PERMISSIONS\nSalesforce1,Salesforce,Salesforce,\"Select, Insert, Update\"\n" +
  "Zendesk1,Zendesk,Zendesk,Select\nStripe1,Stripe,Stripe,Select\n\nQuery ID: c1";
const ROWS = "Id,Label,Detail,Hot\n1,Acme Corp,$45000 overdue,1\n2,Globex,$9000 due,0\n3,Initech,$1200 due,0\n\nQuery ID: q1";
const PLAN = {answer: "Acme Corp is at risk.", rooms: [["Salesforce1", "Salesforce", "Deals slipping"],
  ["Zendesk1", "Zendesk", "Angry tickets"], ["Stripe1", "Stripe", "Unpaid invoices"]]
  .map(([connection, app, title]) => ({connection, app, title, query: `SELECT [Id] FROM [${connection}].[dbo].[t]`}))};

async function fakeConnectAI(route) {
  const body = JSON.parse(route.request().postData());
  const reply = result => route.fulfill({json: {jsonrpc: "2.0", id: body.id, result}, headers: CORS});
  if (!("id" in body)) return route.fulfill({status: 202, body: "", headers: CORS});
  if (body.method === "initialize") return reply({protocolVersion: "2025-06-18", capabilities: {}});
  if (body.method === "tools/list") return reply({tools: ["getCatalogs", "queryData"].map(name =>
    ({name, description: name, inputSchema: {type: "object", properties: {query: {type: "string"}}}}))});
  return reply({content: [{type: "text", text: body.params.name === "getCatalogs" ? CATALOGS : ROWS}]});
}

// Claude: refuses the key in `refuse` mode; else plans the level when briefing, and looks around once when playing
function fakeClaude(mode) {
  return async route => {
    if (route.request().url().includes("/models")) return route.fulfill({json: {data: [{id: "claude-sonnet-5-5"}]}, headers: CORS});
    if (mode === "refuse") return route.fulfill({status: 401, json: {error: {message: "invalid x-api-key"}}, headers: CORS});
    const body = JSON.parse(route.request().postData()), first = body.messages.length === 1;
    const content = /Doom level/.test(body.system || "")
      ? [{type: "text", text: JSON.stringify(PLAN)}]
      : first ? [{type: "text", text: "Looking around."}, {type: "tool_use", id: "t1", name: "doom__look", input: {}}]
      : [{type: "text", text: "Done."}];
    return route.fulfill({json: {content, stop_reason: "end_turn", usage: {input_tokens: 1, output_tokens: 1}}, headers: CORS});
  };
}

const failures = [];
function check(ok, what) {
  console.log(`${ok ? "ok  " : "FAIL"} ${what}`);
  if (!ok) failures.push(what);
}

async function page(browser, mode) {
  const p = await browser.newPage({viewport: {width: 1440, height: 900}});
  p.on("pageerror", e => check(false, `no page error (${e.message})`));
  await p.route("https://api.anthropic.com/**", fakeClaude(mode));
  await p.route("https://mcp.cloud.cdata.com/**", fakeConnectAI);
  await p.addInitScript(() => {   // the visitor's settings, once: Claude and Connect AI, both faked
    if (sessionStorage.getItem("seeded")) return;
    sessionStorage.setItem("seeded", "1");
    localStorage.setItem("connect-doom-ai", JSON.stringify({provider: "anthropic", keys: {anthropic: "sk-ant-test"},
      models: {}, connectAI: {user: "test@example.com", pat: "test", url: ""}}));
  });
  await p.goto(SITE);
  await p.waitForSelector("#coach-intro [data-go=play]:not([disabled])", {timeout: 120000});
  return p;
}
const game = p => p.evaluate(() => JSON.parse(Module.FS.readFile("/doom/agent/state.json", {encoding: "utf8"})));
const text = (p, sel) => p.textContent(sel).then(t => t.replace(/\s+/g, " ").trim());

const server = spawn("python3", ["-m", "http.server", String(PORT), "--bind", "127.0.0.1", "-d", DIST], {stdio: "ignore"});
const browser = await chromium.launch(process.env.CHROMIUM ? {executablePath: process.env.CHROMIUM} : {});
try {
  await new Promise(r => setTimeout(r, 1000));

  // 1. On its own, offline Claude plays the site's level
  let p = await page(browser, "plan");
  await p.waitForTimeout(8000);
  const start = await game(p);
  check(start.options.claude_plays === 1 && start.options.model === "offline-claude", "offline Claude plays on load");
  check(start.monsters.length > 0, `the site's level has monsters (${start.monsters.length})`);
  check(await p.isVisible("#build-link"), "the header has Build a level");

  // 2. The visitor's model takes over from Watch; refused, offline Claude takes over again
  await p.close();
  p = await page(browser, "refuse");
  await p.click("[data-go=watch]");
  await p.waitForFunction(() => /COULDN'T PLAY/.test(document.getElementById("coach-tip").textContent), null, {timeout: 30000});
  check(/Offline Claude takes over/.test(await text(p, "#coach-tip")), "a refused model hands the game to offline Claude");
  await p.close();

  // 3. A level from the visitor's data: built in the page, then played
  p = await page(browser, "plan");
  await p.waitForSelector("#coach-byo [data-go=build]", {timeout: 30000});
  await p.click("#coach-byo [data-go=build]");
  check(/at risk/.test(await p.inputValue("#build-q")), "the build dialog suggests a question from the connections");
  const reloaded = p.waitForEvent("load", {timeout: 180000});
  await p.click("#build-go");
  await p.waitForFunction(() => document.body.classList.contains("building"), null, {timeout: 10000});
  check(true, "the building screen shows");
  // the page reloads into the level, or the dialog says why it couldn't build it
  const failed = p.waitForFunction(() => /Couldn't/.test(document.getElementById("build-msg")?.textContent || ""),
                                   null, {timeout: 180000}).then(() => p.textContent("#build-msg"));
  const why = await Promise.race([reloaded.then(() => null), failed]);
  if (why) throw new Error(`the level didn't build: ${why}`);
  await p.waitForSelector("#coach-intro [data-go=play]:not([disabled])", {timeout: 120000});
  check(/read your Salesforce, Zendesk and Stripe/.test(await text(p, "#coach-intro p")), "the page plays the visitor's level");
  check(/Acme Corp is at risk/.test(await text(p, "#coach-intro p.what")), "its start card has the model's answer");
  await p.waitForTimeout(3000);
  // the level's records, killed or not (offline Claude is already playing it)
  const records = await p.evaluate(async () => (await (await fetch("/state.json")).json()).rooms_live.reduce((n, r) => n + r.left + r.killed, 0));
  check(records === 9 && (await game(p)).monsters.length > 0, `a monster for each record: 3 rooms × 3 rows (${records})`);
  check(!(await p.isVisible("#tabs [data-tab=board]")), "no leaderboard on the visitor's level");

  // 4. Back to the site's level
  const back = p.waitForEvent("load", {timeout: 60000});
  await p.click("#coach-byo [data-go=demo]", {force: true});
  await back;
  await p.waitForSelector("#coach-intro [data-go=play]:not([disabled])", {timeout: 120000});
  check(/demo company/.test(await text(p, "#coach-intro p")), "Demo level goes back to the site's level");
} finally {
  await browser.close();
  server.kill();
}
console.log(failures.length ? `${failures.length} failed` : "all passed");
process.exit(failures.length ? 1 : 0);
