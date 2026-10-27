// Runs panel/index.html's script in Node with a minimal DOM, feeds it a /state.json, and
// prints JSON: every element by id after the render, plus the value of an optional expression.
//   node panel_harness.js <index.html> <state.json> [expression]
const fs = require("fs");
const vm = require("vm");

const [html, statePath, expr] = process.argv.slice(2);
const script = fs.readFileSync(html, "utf8").match(/<script>([\s\S]*)<\/script>/)[1];
const state = JSON.parse(fs.readFileSync(statePath, "utf8"));

class El {
  constructor(tag) {
    this.tag = tag; this.children = []; this.textContent = ""; this.innerHTML = ""; this.hidden = false;
    this.className = ""; this.scrollHeight = 0; this.scrollTop = 0; this.clientHeight = 0; this.parent = null;
    this.subs = {};
    this.vars = {};   // CSS variables set with style.setProperty
    this.style = { setProperty: (k, v) => { this.vars[k] = v; } };
    const el = this;
    this.classList = {
      add: c => { if (!el.classList.contains(c)) el.className = `${el.className} ${c}`.trim(); },
      remove: c => { el.className = el.className.split(/\s+/).filter(x => x !== c).join(" "); },
      contains: c => el.className.split(/\s+/).includes(c),
      toggle: (c, force = !el.classList.contains(c)) => force ? el.classList.add(c) : el.classList.remove(c),
    };
  }
  // .empty placeholders exist only while innerHTML says so; other selectors get a stable child
  querySelector(sel) {
    if (sel.includes(".empty")) return this.innerHTML.includes('class="empty"') ? { remove: () => { this.innerHTML = ""; } } : null;
    return this.subs[sel] ??= new El(sel);
  }
  appendChild(n) { n.parent = this; this.children.push(n); return n; }
  prepend(n) { n.parent = this; this.children.unshift(n); return n; }
  after(n) { const p = this.parent; n.parent = p; p.children.splice(p.children.indexOf(this) + 1, 0, n); return n; }
  insertBefore(n, ref) { n.parent = this; this.children.splice(Math.max(this.children.indexOf(ref), 0), 0, n); return n; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); }
  addEventListener(type, fn) { this.listener = fn; }
  dump() {
    return { text: this.textContent, html: this.innerHTML, hidden: this.hidden, className: this.className, vars: this.vars,
             children: this.children.map(c => c.dump()),
             subs: Object.fromEntries(Object.entries(this.subs).map(([k, v]) => [k, v.innerHTML])) };
  }
}

const byId = {};
const cssVars = {};
const ctx = {
  document: { getElementById: id => byId[id] ??= new El(id), createElement: tag => new El(tag), body: byId.body = new El("body"),
              documentElement: { style: { setProperty: (k, v) => { cssVars[k] = v; } } } },
  location: { search: process.env.PANEL_SEARCH || "" },     // e.g. ?stage=... for the full-screen layout
  URLSearchParams, console,
  fetch: async (url, opts) => {
    if (url === "/ask") {           // the question box: answered as the server would, and recorded
      ctx.asked = JSON.parse(opts.body);
      return { ok: !state.ask_error, json: async () => state.ask_error ? { error: state.ask_error } : ctx.asked };
    }
    if (!url.startsWith("/state.json")) throw new Error(`unexpected fetch ${url}`);
    return { json: async () => state };
  },
  setInterval: () => 0,
};
vm.createContext(ctx);
vm.runInContext(script, ctx);

(async () => {
  await vm.runInContext("tick()", ctx);           // a second tick must not duplicate anything
  const out = { ids: Object.fromEntries(Object.entries(byId).map(([k, v]) => [k, v.dump()])), css: cssVars };
  if (expr) out.value = await vm.runInContext(expr, ctx);
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
