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
import time
import urllib.error
import urllib.request

import claude_run

GATEWAY_URL = os.environ.get("CONNECT_AI_GATEWAY_URL", "https://cloud.cdata.com/gateway/v1")
MAX_TOKENS = 4096          # per reply; thinking models (Gemini) spend some of it before they answer
RETRY_WAITS = (2, 5, 10)   # seconds before each retry of a rate limit or a server error


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


def run(prompt, model, tools, log, *, key, max_turns, post=complete):
    """Play one run: send `prompt`, run each tool the model calls, and send the results back
    until it answers without calling a tool or `max_turns` replies are used up.

    tools: {"server__tool": (fn(args) -> text, description, JSON schema of the arguments)}; a
    tool's text starting with "Error:" is a failed call. log: a claude_run.EventLog. Returns
    {"result": the final text, "turns", "tokens": {"input", "output"}}."""
    specs = [{"type": "function", "function": {"name": name, "description": desc, "parameters": schema}}
             for name, (_, desc, schema) in tools.items()]
    messages = [{"role": "user", "content": prompt}]
    log("session", model=model, tools=sorted(f"mcp__{name}" for name in tools))
    started, turns, final = time.time(), 0, ""
    tokens = {"input": 0, "output": 0}
    while turns < max_turns:
        reply = post(key, {"model": model, "max_tokens": MAX_TOKENS, "messages": messages, "tools": specs})
        turns += 1
        usage = reply.get("usage") or {}
        tokens["input"] += usage.get("prompt_tokens") or 0
        tokens["output"] += usage.get("completion_tokens") or 0
        msg = reply["choices"][0]["message"]
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
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": out})
    log("done", cost_usd=None, duration_ms=round((time.time() - started) * 1000), turns=turns, text=final,
        tokens=tokens)
    return {"result": final, "turns": turns, "tokens": tokens}
