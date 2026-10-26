"""Running any model through Connect AI's LLM Gateway, for Claude plays with another model (play.py).

The gateway (https://cloud.cdata.com/gateway/v1) takes OpenAI's chat completions for every
provider behind it: Anthropic, OpenAI, Gemini, and OpenAI-compatible ones. run() is a small agent
loop over it: the model calls tools (play.py hands it the Doom tools and Connect AI's read-only
ones), and each step goes to the same event log claude_run.run() writes, so the side panel shows
a Gemini run just as it shows Claude's. Requests aren't streamed: the gateway drops Gemini's
streamed replies in Anthropic's format, and plain requests work for every model. It authenticates
with a gateway virtual key (cgw_sk_...), not the Connect AI token.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import claude_run

GATEWAY_URL = os.environ.get("CONNECT_AI_GATEWAY_URL", "https://cloud.cdata.com/gateway/v1")
MAX_TOKENS = 4096          # per reply; thinking models (Gemini) spend some of it before they answer
RETRY_WAITS = (2, 5, 10)   # seconds before each retry of a rate limit or a server error
# Each turn sends the whole conversation again, so the older tool results are cut short: the
# last KEEP_RESULTS go in full (up to RESULT_CHARS), earlier ones as their first TRIMMED_CHARS.
# A long run otherwise sends hundreds of thousands of tokens of stale looks and query results.
KEEP_RESULTS, RESULT_CHARS, TRIMMED_CHARS = 6, 8000, 300
# A reply with no text and no tool call is a glitch, not the model stopping (Sonnet through the
# gateway sends one now and then, right after a "Blocked" result): ask again, up to this many times.
EMPTY_RETRIES = 2


class GatewayError(RuntimeError):
    pass


def complete(key, body, timeout=120, sleep=time.sleep):
    """POST one chat completion; return the reply. Rate limits, server errors and a dropped
    connection are retried a few times; anything else (a bad key, a model off the allowlist)
    raises GatewayError with the gateway's message."""
    req = urllib.request.Request(f"{GATEWAY_URL}/chat/completions", json.dumps(body).encode(),
                                 {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    for wait in (*RETRY_WAITS, None):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                text = e.read().decode(errors="replace")
            if e.code != 429 and e.code < 500 or wait is None:
                try:
                    text = json.loads(text)["error"]["message"]
                except (ValueError, KeyError, TypeError):
                    pass
                raise GatewayError(f"HTTP {e.code}: {text[:300]}")
        except (urllib.error.URLError, TimeoutError) as e:
            if wait is None:
                raise GatewayError(f"The LLM Gateway didn't answer: {e}")
        sleep(wait)


def models(key, cache, timeout=4):
    """The models this key may use (GET /models: the project's allowlist), for the Options
    menu's Model row. Saved in `cache`; when the gateway doesn't answer in time, the last list."""
    req = urllib.request.Request(f"{GATEWAY_URL}/models", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            names = [m["id"] for m in json.loads(resp.read()).get("data", []) if m.get("id")]
        cache.write_text(json.dumps(names) + "\n")
        return names
    except (OSError, ValueError, KeyError):
        try:
            return json.loads(cache.read_text())
        except (OSError, ValueError):
            return []


def compact(messages):
    """Cut the tool results before the last KEEP_RESULTS short, in place. The model's own
    messages stay as they were (Gemini keeps its reasoning in its tool call ids)."""
    results = [m for m in messages if m["role"] == "tool"]
    for m in results[:-KEEP_RESULTS] if len(results) > KEEP_RESULTS else []:
        if len(m["content"]) > TRIMMED_CHARS and not m["content"].endswith("[trimmed]"):
            m["content"] = m["content"][:TRIMMED_CHARS] + " ... [trimmed]"


def run(prompt, model, tools, log, *, key, max_turns, post=complete, system=None):
    """Play one run: send `prompt`, run each tool the model calls, and send the results back
    until it answers without calling a tool or `max_turns` replies are used up.

    tools: {"server__tool": (fn(args) -> text, description, JSON schema of the arguments)}; a
    tool's text starting with "Error:" is a failed call. log: a claude_run.EventLog. Returns
    {"result": the final text, "turns", "tokens": {"input", "output"}}."""
    specs = [{"type": "function", "function": {"name": name, "description": desc, "parameters": schema}}
             for name, (_, desc, schema) in tools.items()]
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    log("session", model=model, tools=sorted(f"mcp__{name}" for name in tools))
    started, turns, final, empty, stop = time.time(), 0, "", 0, None
    tokens = {"input": 0, "output": 0}
    while turns < max_turns:
        compact(messages)
        reply = post(key, {"model": model, "max_tokens": MAX_TOKENS, "messages": messages, "tools": specs})
        usage = reply.get("usage") or {}
        tokens["input"] += usage.get("prompt_tokens") or 0
        tokens["output"] += usage.get("completion_tokens") or 0
        msg, stop = reply["choices"][0]["message"], reply["choices"][0].get("finish_reason")
        if not (msg.get("content") or "").strip() and not msg.get("tool_calls") and empty < EMPTY_RETRIES:
            empty += 1                  # not a turn the model played: the same request again
            log("empty_reply", turn=turns + 1, finish_reason=reply["choices"][0].get("finish_reason"))
            continue
        turns += 1
        text = (msg.get("content") or "").strip()
        if text:
            log("text_delta", text=text)
            log("thought", text=text[:2000])
        calls = msg.get("tool_calls") or []
        # Sent back as it came: Gemini keeps its reasoning in the tool call ids
        messages.append({"role": "assistant", "content": msg.get("content"), **({"tool_calls": calls} if calls else {})})
        if not calls:
            final = text
            break
        for call in calls:
            name = call["function"]["name"]
            server, _, tool = name.rpartition("__")
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            log("call", id=call["id"], server=server, tool=tool, args=args, connection=claude_run.catalog_of(args))
            start = time.time()
            try:
                out = tools[name][0](args) if name in tools else f"Error: there's no tool called {name}."
            except Exception as e:
                out = f"Error: {e}"
            error = out.startswith("Error:")
            log("result", id=call["id"], tool=tool, connection=claude_run.catalog_of(args), error=error,
                rows=0 if error else claude_run.count_rows(out), text=out[:4000],
                ms=round((time.time() - start) * 1000))
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": out[:RESULT_CHARS]})
    log("done", cost_usd=None, duration_ms=round((time.time() - started) * 1000), turns=turns, text=final,
        tokens=tokens, empty_replies=empty, stop=stop)    # stop: why the last reply ended (content_filter: blocked)
    return {"result": final, "turns": turns, "tokens": tokens, "empty_replies": empty}


PLAIN_PROMPT_TOKENS = 100   # a one-line prompt costs a few dozen; past this the gateway added context


def check(key, post=complete, timeout=10):
    """`./raid.sh doctor`'s look at the gateway: (status, text) lines, status ok, error or note.
    The key and its models, a one-line prompt to each (a model the project lists may still be
    refused), and whether the gateway pads prompts with context (the routing rule's Workspace
    Context, User Context, Search Context or Data Source Instructions): then every model's
    prompt differs from the game's, and costs more."""
    req = urllib.request.Request(f"{GATEWAY_URL}/models", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            names = [m["id"] for m in json.loads(resp.read()).get("data", []) if m.get("id")]
    except urllib.error.HTTPError as e:
        with e:
            return [("error", f"the LLM Gateway refused the key (HTTP {e.code}): issue a new one, see the README")]
    except (OSError, ValueError) as e:
        return [("note", f"the LLM Gateway didn't answer ({e}); --model and the menu's models need it")]
    lines = [("ok", f"LLM Gateway key: {len(names)} models ({', '.join(names)})")]
    padded = None
    for name in names:
        try:
            reply = post(key, {"model": name, "max_tokens": 2000, "messages": [{"role": "user", "content": "Reply with: ok"}]})
        except GatewayError as e:
            why = str(e).split(": ", 1)[-1].split(". ")[0].rstrip(".")   # the gateway's first sentence
            lines.append(("note", f"{name} is refused ({why}). Leave it off a bench"))
            continue
        lines.append(("ok", f"{name} answers"))
        tokens = (reply.get("usage") or {}).get("prompt_tokens") or 0
        if tokens > PLAIN_PROMPT_TOKENS:
            padded = tokens
    if padded:
        lines.append(("note", f"the gateway adds context to every prompt ({padded} tokens for one line): turn off the "
                              "routing rule's Workspace Context, User Context, Search Context and Data Source "
                              "Instructions, so every model gets the game's prompt alone"))
    return lines


def main(args):
    """gateway_run.py models: the key's models, comma-separated, for the Options menu's Model row
    (raid.sh). The last list comes back at once, and a fresh one is fetched in the background for
    the next game, so a launch never waits on the network; only the first one waits, 4 s at most.
    gateway_run.py refresh: just the fetch. gateway_run.py check: check() for ./raid.sh doctor,
    one tab-separated status and text a line."""
    state = Path(os.environ.get("CONNECT_DOOM_STATE", Path(__file__).resolve().parent / "state"))
    cache, key = state / "gateway-models.json", os.environ["CONNECT_AI_GATEWAY_KEY"]
    if args == ["refresh"]:
        models(key, cache)
        return
    if args == ["check"]:
        for status, text in check(key):
            print(f"{status}\t{text}")
        return
    try:
        names = json.loads(cache.read_text())
    except (OSError, ValueError):
        names = None
    if names is None:
        names = models(key, cache)
    else:
        subprocess.Popen([sys.executable, __file__, "refresh"], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(",".join(names))


if __name__ == "__main__":
    main(sys.argv[1:])
