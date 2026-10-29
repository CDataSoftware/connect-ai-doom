// Your own AI in the browser: a visitor's model (Claude, ChatGPT, or any model on OpenRouter) plays
// the level with the Doom tools, prompt and moves every model gets on a Mac (play.py, gateway_run.py),
// and answers questions about the visitor's own data through the Connect AI MCP server. The settings,
// keys included, stay in this browser (localStorage) and go straight to each provider: the site has
// no server. Without them, the page plays offline, as it always has (replay.js).
window.ConnectDoomAI = (() => {
  const STORAGE_KEY = "connect-doom-ai";
  const PROVIDERS = {
    anthropic: {name: "Claude", company: "Anthropic", keyUrl: "https://console.anthropic.com/settings/keys",
                placeholder: "sk-ant-…", defaultModel: "claude-sonnet-5-5"},
    openai: {name: "ChatGPT", company: "OpenAI", keyUrl: "https://platform.openai.com/api-keys",
             placeholder: "sk-…", defaultModel: "gpt-5.6-sol"},
    openrouter: {name: "OpenRouter", company: "OpenRouter", keyUrl: "https://openrouter.ai/keys",
                 placeholder: "sk-or-…", defaultModel: "anthropic/claude-sonnet-5.5"},
  };
  const OFFLINE = "offline-claude";   // the Options menu's Model row: Claude's recorded run (replay.js)
  const MCP_URL = "https://mcp.cloud.cdata.com/mcp";
  const READ_ONLY_OPS = "get_catalogs,get_schemas,get_tables,get_columns,get_instructions,queryData";   // claude_run.py
  const PAT_URL = "https://cloud.cdata.com/settings?defaultTab=PersonalAccessTokens";
  // Connect AI's free trial sign-up, as cdata.com's Start Free Trial button opens it: its source
  // tag must not be empty (www.cdata.com/ai/signup/ sends an empty one, and the sign-up fails)
  const SIGNUP_URL = "https://cloud-login.cdata.com/authorize?prompt=login&screen_hint=signup&response_type=code" +
    "&client_id=8dtJbDAVHT0exzv9cPDuxxFBhzqsj78m&redirect_uri=https://cloud.cdata.com&ext-utm_source=connect-ai-doom";
  const QUICKSTART_URL = "https://docs.cloud.cdata.com/en/Quick-Start-Guide";

  // gateway_run.py's limits, so a model plays here as it does on a Mac
  const MAX_TOKENS = 4096, MAX_TURNS = 200, MAX_MOVES = 100, RETRY_WAITS = [2, 5, 10];
  const KEEP_RESULTS = 6, RESULT_CHARS = 8000, TRIMMED_CHARS = 300, EMPTY_RETRIES = 2;
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  class AIError extends Error {}

  // ---------- the settings ----------
  const blank = () => ({provider: "offline", keys: {}, models: {}, connectAI: {user: "", pat: "", url: ""}});
  let unsaved = null;   // the settings, when this browser won't store them (a private window, blocked storage)

  function load() {
    if (unsaved) return structuredClone(unsaved);
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
      if (saved) return {...blank(), ...saved, connectAI: {...blank().connectAI, ...saved.connectAI}};
    } catch (e) { /* storage blocked or unreadable: offline */ }
    return blank();
  }

  // Throws when the browser won't store them: they last until the page closes, and the panel says so.
  function save(settings) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
      unsaved = null;
    } catch (e) {
      unsaved = structuredClone(settings);
      throw e;
    }
  }

  function clear() {
    unsaved = null;
    try { localStorage.removeItem(STORAGE_KEY); } catch (e) { /* storage blocked: nothing was saved */ }
  }

  // The model that plays and answers: {provider, key, model}, or null to play offline.
  function active(settings) {
    const provider = settings.provider, key = settings.keys[provider];
    if (!PROVIDERS[provider] || !key) return null;
    return {provider, key, model: settings.models[provider] || PROVIDERS[provider].defaultModel};
  }

  const hasConnectAI = settings => !!(settings.connectAI.user && settings.connectAI.pat);

  // ---------- the providers ----------
  // A request the browser itself refused, or that never got an answer: a page can't tell which.
  const unreachable = (what, url) => new AIError(`${what} didn't answer at ${new URL(url).host}: it's down, ` +
    "this network blocks it, or it doesn't accept requests from web pages.");

  async function request(url, init, what) {
    for (const wait of [...RETRY_WAITS, null]) {
      let r;
      try {
        r = await fetch(url, init);
      } catch (e) {
        if (init.signal?.aborted) throw e;
        if (wait === null) throw unreachable(what, url);
        await sleep(wait * 1000);
        continue;
      }
      if (r.ok) return r.json();
      const text = await r.text();
      if ((r.status === 429 || r.status >= 500) && wait !== null) { await sleep(wait * 1000); continue; }
      let message = text;
      try { const body = JSON.parse(text); message = body.error?.message || body.error || body.message || text; } catch (e) {}
      throw new AIError(`${what}: HTTP ${r.status}: ${String(message).slice(0, 300)}`);
    }
  }

  const headers = (provider, key) => provider === "anthropic"
    ? {"x-api-key": key, "anthropic-version": "2023-06-01", "anthropic-dangerous-direct-browser-access": "true",
       "Content-Type": "application/json"}
    : {"Authorization": `Bearer ${key}`, "Content-Type": "application/json",
       ...(provider === "openrouter" ? {"HTTP-Referer": location.origin, "X-Title": "Connect AI × DOOM"} : {})};

  // The models a key may use, for the settings' model list. Checking the key on the way.
  async function listModels(provider, key) {
    const what = PROVIDERS[provider].company, h = headers(provider, key);
    if (provider === "anthropic") {
      const body = await request("https://api.anthropic.com/v1/models?limit=100", {headers: h}, what);
      return body.data.map(m => m.id);
    }
    if (provider === "openai") {
      const body = await request("https://api.openai.com/v1/models", {headers: h}, what);
      return body.data.filter(m => /^(gpt-|o\d)/.test(m.id) && !/audio|realtime|transcribe|tts|image|search|embedding/.test(m.id))
        .sort((a, b) => b.created - a.created).map(m => m.id);
    }
    await request("https://openrouter.ai/api/v1/key", {headers: h}, what);   // its model list is public: check the key here
    const body = await request("https://openrouter.ai/api/v1/models", {}, what);
    return body.data.filter(m => (m.supported_parameters || []).includes("tools") && !m.id.endsWith(":batch")).map(m => m.id);
  }

  // One reply to an OpenAI-format conversation (gateway_run.py's), as {message: {content, tool_calls},
  // finish_reason, usage: {input, output}}. Anthropic's Messages API gets the conversation translated.
  async function complete(ai, messages, tools, signal) {
    const what = PROVIDERS[ai.provider].company;
    if (ai.provider !== "anthropic") {
      const url = ai.provider === "openai" ? "https://api.openai.com/v1/chat/completions" : "https://openrouter.ai/api/v1/chat/completions";
      const body = {model: ai.model, messages, ...(tools.length ? {tools} : {}),
                    [ai.provider === "openai" ? "max_completion_tokens" : "max_tokens"]: MAX_TOKENS};
      const reply = await request(url, {method: "POST", headers: headers(ai.provider, ai.key), body: JSON.stringify(body), signal}, what);
      const choice = reply.choices?.[0];
      if (!choice) throw new AIError(`${what} sent no reply.`);
      return {message: choice.message, finish_reason: choice.finish_reason,
              usage: {input: reply.usage?.prompt_tokens || 0, output: reply.usage?.completion_tokens || 0}};
    }
    const system = messages.filter(m => m.role === "system").map(m => m.content).join("\n");
    const turns = [];
    for (const m of messages) {
      if (m.role === "user") turns.push({role: "user", content: m.content});
      else if (m.role === "assistant") turns.push({role: "assistant", content: [
        ...(m.content ? [{type: "text", text: m.content}] : []),
        ...(m.tool_calls || []).map(c => ({type: "tool_use", id: c.id, name: c.function.name,
                                            input: JSON.parse(c.function.arguments || "{}")}))]});
      else if (m.role === "tool") {
        const result = {type: "tool_result", tool_use_id: m.tool_call_id, content: m.content};
        const last = turns.at(-1);
        if (last?.role === "user" && Array.isArray(last.content) && last.content[0]?.type === "tool_result") last.content.push(result);
        else turns.push({role: "user", content: [result]});
      }
    }
    const body = {model: ai.model, max_tokens: MAX_TOKENS, messages: turns, ...(system ? {system} : {}),
                  ...(tools.length ? {tools: tools.map(t => ({name: t.function.name, description: t.function.description,
                                                               input_schema: t.function.parameters}))} : {})};
    const reply = await request("https://api.anthropic.com/v1/messages",
      {method: "POST", headers: headers("anthropic", ai.key), body: JSON.stringify(body), signal}, what);
    const text = reply.content.filter(b => b.type === "text").map(b => b.text).join("\n");
    const calls = reply.content.filter(b => b.type === "tool_use")
      .map(b => ({id: b.id, type: "function", function: {name: b.name, arguments: JSON.stringify(b.input)}}));
    return {message: {content: text, ...(calls.length ? {tool_calls: calls} : {})},
            finish_reason: reply.stop_reason, usage: {input: reply.usage?.input_tokens || 0, output: reply.usage?.output_tokens || 0}};
  }

  // gateway_run.compact: the tool results before the last KEEP_RESULTS cut short, in place.
  function compact(messages) {
    const results = messages.filter(m => m.role === "tool");
    for (const m of results.slice(0, Math.max(0, results.length - KEEP_RESULTS)))
      if (m.content.length > TRIMMED_CHARS && !m.content.endsWith("[trimmed]")) m.content = m.content.slice(0, TRIMMED_CHARS) + " ... [trimmed]";
  }

  // claude_run.catalog_of: the connection a call is about, its catalogName or the query's first [catalog]
  const catalogOf = args => args?.catalogName || (String(args?.query || "").match(/\[([^\]]+)\]\s*\.\s*\[/) || [])[1] || null;
  // claude_run.count_rows: the rows in a CSV result (the text ends with a 'Query ID' line)
  const countRows = text => Math.max(text.split("\nQuery ID:")[0].trim().split("\n").length - 1, 0);

  // gateway_run.run: send `prompt`, run each tool the model calls, and send the results back until it
  // answers without a tool, the turns run out or until() says so. tools: {"server__tool": {run(args)
  // -> text, description, schema}}; text starting "Error:" is a failed call. log(kind, data) feeds the panel.
  async function runAgent(ai, {prompt, system, tools, log, signal, until = () => false}) {
    const specs = Object.entries(tools).map(([name, t]) => ({type: "function",
      function: {name, description: t.description, parameters: t.schema}}));
    const messages = [...(system ? [{role: "system", content: system}] : []), {role: "user", content: prompt}];
    log("session", {model: ai.model, tools: Object.keys(tools).map(n => `mcp__${n}`).sort()});
    const started = Date.now(), tokens = {input: 0, output: 0};
    let turns = 0, empty = 0, final = "", stop = null;
    while (turns < MAX_TURNS && !signal.aborted && !until()) {
      compact(messages);
      const reply = await complete(ai, messages, specs, signal);
      tokens.input += reply.usage.input;
      tokens.output += reply.usage.output;
      const msg = reply.message, text = (msg.content || "").trim(), calls = msg.tool_calls || [];
      stop = reply.finish_reason;
      if (!text && !calls.length && empty < EMPTY_RETRIES) { empty += 1; continue; }   // a glitch: ask again
      turns += 1;
      if (text) { log("text_delta", {text}); log("thought", {text: text.slice(0, 2000)}); }
      messages.push({role: "assistant", content: msg.content || null, ...(calls.length ? {tool_calls: calls} : {})});
      if (!calls.length) { final = text; break; }
      for (const call of calls) {
        if (signal.aborted) break;
        const name = call.function.name, [server, tool] = name.includes("__") ? name.split("__", 2) : ["", name];
        let args = {};
        try { args = JSON.parse(call.function.arguments || "{}"); } catch (e) {}
        log("call", {id: call.id, server, tool, args, connection: catalogOf(args)});
        const start = Date.now();
        let out;
        try { out = tools[name] ? await tools[name].run(args) : `Error: there's no tool called ${name}.`; }
        catch (e) { if (signal.aborted) break; out = `Error: ${e.message}`; }
        const error = out.startsWith("Error:");
        log("result", {id: call.id, tool, connection: catalogOf(args), error, text: out.slice(0, 4000),
                       rows: error ? 0 : countRows(out), ms: Date.now() - start});
        messages.push({role: "tool", tool_call_id: call.id, content: out.slice(0, RESULT_CHARS)});
      }
    }
    log("done", {cost_usd: null, duration_ms: Date.now() - started, turns, text: final, tokens, stop});
    return final;
  }

  // ---------- the Connect AI MCP server (connect_ai_mcp.py) ----------
  // Connect AI's MCP server takes ?ops= to offer only the read-only tools; any other URL (an MCP
  // Gateway tool server's Agent URL) is used as it is.
  function mcpEndpoint(url) {
    const u = new URL(url || MCP_URL);
    if (u.host === new URL(MCP_URL).host && !u.searchParams.has("ops")) u.searchParams.set("ops", READ_ONLY_OPS);
    return u.toString();
  }

  class ConnectAI {
    constructor({user, pat, url}) {
      this.url = mcpEndpoint(url);
      this.auth = `Basic ${btoa(`${user}:${pat}`)}`;
      this.session = null;
      this.nextId = 1;
    }

    async rpc(method, params, notify = false) {
      const body = {jsonrpc: "2.0", method, ...(params ? {params} : {}), ...(notify ? {} : {id: this.nextId++})};
      let r;
      try {
        r = await fetch(this.url, {method: "POST", body: JSON.stringify(body), headers: {
          "Authorization": this.auth, "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
          ...(this.session ? {"Mcp-Session-Id": this.session} : {})}});
      } catch (e) {
        throw unreachable("The MCP server", this.url);
      }
      this.session = r.headers.get("Mcp-Session-Id") || this.session;
      const raw = await r.text();
      if (!r.ok) {
        if (r.status === 401) throw new AIError("Connect AI refused the email and token. Check them, or make a new token.");
        throw new AIError(`Connect AI: HTTP ${r.status}: ${raw.slice(0, 300)}`);
      }
      if (notify || !raw.trim()) return null;
      const json = raw.trimStart().startsWith("{") ? raw
        : raw.split("\n").filter(l => l.startsWith("data:")).map(l => l.slice(5)).at(-1);   // server-sent events: the last data line
      const reply = JSON.parse(json);
      if (reply.error) throw new AIError(`Connect AI: ${reply.error.message || JSON.stringify(reply.error)}`);
      return reply.result;
    }

    async start() {
      await this.rpc("initialize", {protocolVersion: "2025-06-18", capabilities: {},
                                    clientInfo: {name: "connect-doom-web", version: "1"}});
      await this.rpc("notifications/initialized", null, true);
      return (await this.rpc("tools/list")).tools;
    }

    async call(tool, args) {
      const result = await this.rpc("tools/call", {name: tool, arguments: args});
      const text = (result.content || []).filter(c => c.type === "text").map(c => c.text).join("\n");
      return result.isError && !text.startsWith("Error:") ? `Error: ${text}` : text;
    }
  }

  // The visitor's Connect AI connections, [{name, source}] (getCatalogs' CSV: TABLE_CATALOG,
  // DATA_SOURCE,...), for the settings and the question box; null until checked. An MCP Gateway
  // tool server may not offer getCatalogs: then none are listed.
  let connections = null;

  async function checkConnectAI(settings) {
    const client = new ConnectAI(settings.connectAI), tools = await client.start();
    const catalogs = tools.find(t => /get_?catalogs$/i.test(t.name));
    connections = [];
    if (catalogs) {
      const text = await client.call(catalogs.name, {});
      if (!text.startsWith("Error:"))
        connections = text.split("\nQuery ID:")[0].trim().split(/\r?\n/).slice(1).map(line => line.split(","))
          .filter(([name]) => name).map(([name, source]) => ({name, source: source || name}));
    }
    return {tools: tools.length, connections};
  }

  // Connect AI's read-only tools for a model, as {"connectai__<tool>": ...}.
  async function connectAITools(settings) {
    const client = new ConnectAI(settings.connectAI), listed = await client.start(), tools = {};
    for (const t of listed)
      tools[`connectai__${t.name}`] = {run: args => client.call(t.name, args), description: t.description || "",
                                       schema: t.inputSchema || {type: "object", properties: {}}};
    return tools;
  }

  // ---------- playing the level (play.py's prompt and tools) ----------
  const PROMPT = `You're playing Doom, and every monster is a live business record that Connect AI found
for this question: {question}

The level: a lobby with keycards, a hall, and one room per app behind a keycard door.
{rooms}
Urgent records (urgent: true, the biggest monster in each room) matter most. Clear the urgent
records first, then whatever is left.

Use the doom tools: look, go_to, attack. Pick up the keycards first. Then go room by room.
Opening a room's door brings its monsters rushing out. When go_to says monsters are in the way,
attack the closest_pids it lists, in order (the ones in sight come first), then go_to again; don't
call go_to again before you've fought.
People watch your feed live next to the game. Before EVERY tool call, write ONE short line, as if
narrating a stream, saying what you're doing and why. Before an attack, name the record and why it
matters (for example: "Desert Power's renewal ticket is urgent, taking it down."); before a move,
say where and why (for example: "Salesforce is cleared, heading to Zendesk next."). After a few
kills in a room, you may use the Connect AI tools once to check something related in another app.
{vault}
You have {moves} moves: each look, go_to or attack is one (the Connect AI tools are free). Keep
going until the rooms are clear or the moves run out. End with a two-line summary.`;
  const SYSTEM = "You play a game through the tools you're given, narrating each step for an audience " +
                 "watching live. Use only those tools.";
  const DOOM_TOOLS = {
    look: ["See where you are, your keycards, each room's app and monsters left, and the monsters you can see " +
           "right now, each with the business record it stands for.", {}],
    go_to: ["Walk somewhere. place: 'keycards' (pick up the keycards in the lobby), 'room 1'..'room 3' or the " +
            "app's name, 'vault' (the PII vault's red door), or 'lobby'. Doors open on arrival if you hold the right keycard.",
            {place: {type: "string"}}],
    attack: ["Aim at a visible monster by pid and fire until it dies (about 10 s at most).", {pid: {type: "integer"}}],
  };

  // play(settings, level, log, onConnectAIOut): the settings' model plays the level ({mission, records}).
  // Returns {done, stop} at once; done resolves with the model's summary, or rejects with an AIError
  // (a bad key, a model refused): web.js hands the game to offline Claude then. Connect AI's tools come
  // along when the settings have a token; when Connect AI won't answer, the model plays without them.
  function play(settings, level, log, onConnectAIOut) {
    const ai = active(settings), m = level.mission, controller = new AbortController();
    const doom = window.ConnectDoomReplay.doomTools(level);
    let moves = 0;
    const tools = {};
    for (const [name, [description, props]] of Object.entries(DOOM_TOOLS))
      tools[`doom__${name}`] = {description, schema: {type: "object", properties: props, required: Object.keys(props)},
        run: async args => {
          if (++moves > MAX_MOVES) return "Error: no moves left.";
          return JSON.stringify(await doom.tools[name](args));
        }};
    const rooms = m.rooms.map((r, i) => `  room ${i + 1}: ${r.app}, ${r.title} ` +
      `(${r.writable ? "yellow door, read + write" : "blue door, read only"})`).join("\n");
    const vault = (m.denied || []).length ? "There is also a PII vault behind a red door. Try it once, near the end, " +
                                            "to see what Connect AI does." : "";
    const prompt = PROMPT.replace("{question}", m.question).replace("{rooms}", rooms).replace("{vault}", vault)
      .replace("{moves}", MAX_MOVES);

    async function run() {
      log("question", {text: `${ai.model} plays: ${m.question}`});
      if (hasConnectAI(settings)) {
        try { Object.assign(tools, await connectAITools(settings)); }
        catch (e) { onConnectAIOut(e.message); }
      }
      return runAgent(ai, {prompt, system: SYSTEM, tools, log, signal: controller.signal, until: () => moves >= MAX_MOVES});
    }
    // Done, the marine goes back to the player; failed, web.js hands it to offline Claude
    const done = run().then(summary => { doom.stop(); return summary; }, e => {
      doom.stop(false);
      if (controller.signal.aborted) return "";   // stopped
      throw e;
    });
    return {done, stop(handBack = true) { controller.abort(); doom.stop(handBack); }};
  }

  // ---------- a question about the visitor's own data ----------
  const ASK_SYSTEM = "You answer questions about the user's business data with the Connect AI tools, which are " +
    "read-only. Explore only as much as you need: list catalogs, then the few tables you need, then query. People " +
    "watch your steps live: before each tool call, write one short line saying what you're doing. If Connect AI " +
    "refuses a request, say so plainly; don't work around it. Finish with a plain answer of two to four sentences.";

  // ask(settings, question, log): the settings' model answers through Connect AI. {done, stop}; done
  // resolves with the answer.
  function ask(settings, question, log) {
    const ai = active(settings), controller = new AbortController();
    const done = (async () => {
      log("question", {text: question});
      const tools = await connectAITools(settings);
      return runAgent(ai, {prompt: question, system: ASK_SYSTEM, tools, log, signal: controller.signal});
    })();
    return {done, stop() { controller.abort(); }};
  }

  return {PROVIDERS, OFFLINE, PAT_URL, SIGNUP_URL, QUICKSTART_URL, MCP_URL, AIError, connections: () => connections, checkConnectAI, load, save, clear, active, hasConnectAI, listModels,
          play, ask};
})();
