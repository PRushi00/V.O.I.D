"""OpenAI-compatible and Anthropic candidates for the benchmark (evaluation only; not V.O.I.D provider adapters).

Keys are read in-process from an environment variable or the OS keyring (service "void") and are only ever placed in the
request header; they are never printed, logged or written. ``base=`` lets the same code target any OpenAI-compatible gateway
(for example a local OmniRoute) so a gateway's added latency can be compared with the direct API on identical prompts.
"""
from __future__ import annotations

import json
import os
import time

import requests

from void.providers.base import ToolCall


def _key(env_name: str, keyring_name: str) -> str | None:
    v = os.environ.get(env_name)
    if v:
        return v
    try:
        import keyring
        return keyring.get_password("void", keyring_name)
    except Exception:
        return None


def _sse(resp):
    for raw in resp.iter_lines():
        if not raw:
            continue
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        if line.startswith("data:"):
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                yield json.loads(data)
            except json.JSONDecodeError:
                continue


def _http_error(resp, scrub):
    code = resp.status_code
    kind = "quota" if code == 429 else ("auth" if code in (401, 403) else "transient")
    return {"class": "HTTPError", "code": code, "kind": kind, "msg": scrub(resp.text[:240])}


class OpenAICandidate:
    provider = "openai-compatible"

    def __init__(self, model, base="https://api.openai.com/v1", effort=None, extra=None, timeout_s=90.0, Result=None, scrub=lambda s: s):
        self.model, self.base, self.effort, self.extra, self.timeout_s = model, base.rstrip("/"), effort, extra or {}, timeout_s
        self.Result, self.scrub = Result, scrub
        host = base.split("//")[-1].split("/")[0]
        self.label = f"openai:{model}@{host}" + (f":effort={effort}" if effort else "")

    @staticmethod
    def _messages(messages):
        out = []
        for m in messages:
            role = m.get("role")
            if role in ("system", "user"):
                out.append({"role": role, "content": m.get("content", "")})
            elif role == "assistant":
                d = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    d["tool_calls"] = [{"id": tc.get("id") or f"call_{i}", "type": "function",
                                        "function": {"name": tc["name"], "arguments": json.dumps(tc.get("arguments", {}))}}
                                       for i, tc in enumerate(m["tool_calls"])]
                out.append(d)
            elif role == "tool":
                out.append({"role": "tool", "tool_call_id": m.get("tool_call_id") or "call_0", "content": m.get("content", "")})
        return out

    def run(self, messages, tools):
        r = self.Result()
        key = _key("OPENAI_API_KEY", "openai_api_key")
        if not key:
            r.error = {"class": "NoCredential", "code": None, "kind": "auth", "msg": "no OpenAI key in env/keyring"}
            r.total_s = 0.0
            return r
        body = {"model": self.model, "messages": self._messages(messages), "stream": True,
                "stream_options": {"include_usage": True}, **self.extra}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                             for t in tools]
        if self.effort:
            body["reasoning_effort"] = self.effort
        t0 = time.perf_counter()
        texts, calls = [], {}
        try:
            with requests.post(f"{self.base}/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"},
                               stream=True, timeout=self.timeout_s) as resp:
                if resp.status_code >= 400:
                    r.error = _http_error(resp, self.scrub)
                else:
                    for ev in _sse(resp):
                        if ev.get("usage"):
                            u = ev["usage"]
                            r.usage = {"prompt": u.get("prompt_tokens"), "output": u.get("completion_tokens"),
                                       "thoughts": (u.get("completion_tokens_details") or {}).get("reasoning_tokens")}
                        for ch in ev.get("choices") or []:
                            d = ch.get("delta") or {}
                            if (d.get("content") or d.get("tool_calls")) and r.ttft_s is None:
                                r.ttft_s = time.perf_counter() - t0
                            if d.get("content"):
                                texts.append(d["content"])
                            for tc in d.get("tool_calls") or []:
                                slot = calls.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": ""})
                                slot["id"] = tc.get("id") or slot["id"]
                                fn = tc.get("function") or {}
                                slot["name"] += fn.get("name") or ""
                                slot["args"] += fn.get("arguments") or ""
        except Exception as exc:
            name = type(exc).__name__
            r.error = {"class": name, "code": None, "kind": "timeout" if "Timeout" in name else "transient",
                       "msg": self.scrub(str(exc))[:200]}
        r.text = "".join(texts) or None
        for i in sorted(calls):
            c = calls[i]
            try:
                args = json.loads(c["args"]) if c["args"] else {}
            except json.JSONDecodeError:
                args = {}
            r.tool_calls.append(ToolCall(name=c["name"], arguments=args, id=c["id"]))
        r.total_s = time.perf_counter() - t0
        return r


class AnthropicCandidate:
    provider = "anthropic"

    def __init__(self, model, base="https://api.anthropic.com", extra=None, timeout_s=90.0, Result=None, scrub=lambda s: s):
        self.model, self.base, self.extra, self.timeout_s = model, base.rstrip("/"), extra or {}, timeout_s
        self.Result, self.scrub = Result, scrub
        self.label = f"anthropic:{model}@{base.split('//')[-1].split('/')[0]}"

    @staticmethod
    def _messages(messages):
        system, out = [], []
        for m in messages:
            role = m.get("role")
            if role == "system":
                system.append(m.get("content", ""))
            elif role == "user":
                out.append({"role": "user", "content": m.get("content", "")})
            elif role == "assistant":
                blocks = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for i, tc in enumerate(m.get("tool_calls") or []):
                    blocks.append({"type": "tool_use", "id": tc.get("id") or f"toolu_{i}", "name": tc["name"], "input": tc.get("arguments", {})})
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            elif role == "tool":
                out.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": m.get("tool_call_id") or "toolu_0",
                                                          "content": m.get("content", "")}]})
        return "\n\n".join(system), out

    def run(self, messages, tools):
        r = self.Result()
        key = _key("ANTHROPIC_API_KEY", "anthropic_api_key")
        if not key:
            r.error = {"class": "NoCredential", "code": None, "kind": "auth", "msg": "no Anthropic key in env/keyring"}
            r.total_s = 0.0
            return r
        system, msgs = self._messages(messages)
        body = {"model": self.model, "max_tokens": 1024, "system": system, "messages": msgs, "stream": True, **self.extra}
        if tools:
            body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
        t0 = time.perf_counter()
        texts, blocks = [], {}
        try:
            with requests.post(f"{self.base}/v1/messages", json=body, headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                               stream=True, timeout=self.timeout_s) as resp:
                if resp.status_code >= 400:
                    r.error = _http_error(resp, self.scrub)
                else:
                    for ev in _sse(resp):
                        t = ev.get("type")
                        if t == "content_block_start":
                            cb = ev.get("content_block") or {}
                            blocks[ev.get("index", 0)] = {"type": cb.get("type"), "id": cb.get("id"), "name": cb.get("name"), "json": ""}
                            if cb.get("type") == "tool_use" and r.ttft_s is None:
                                r.ttft_s = time.perf_counter() - t0
                        elif t == "content_block_delta":
                            d = ev.get("delta") or {}
                            if d.get("type") == "text_delta" and d.get("text"):
                                if r.ttft_s is None:
                                    r.ttft_s = time.perf_counter() - t0
                                texts.append(d["text"])
                            elif d.get("type") == "input_json_delta":
                                blocks.setdefault(ev.get("index", 0), {"type": "tool_use", "json": ""})["json"] += d.get("partial_json", "")
                        elif t == "message_start":
                            r.usage["prompt"] = ((ev.get("message") or {}).get("usage") or {}).get("input_tokens")
                        elif t == "message_delta":
                            r.usage["output"] = (ev.get("usage") or {}).get("output_tokens")
        except Exception as exc:
            name = type(exc).__name__
            r.error = {"class": name, "code": None, "kind": "timeout" if "Timeout" in name else "transient",
                       "msg": self.scrub(str(exc))[:200]}
        r.text = "".join(texts) or None
        for i in sorted(blocks):
            b = blocks[i]
            if b.get("type") == "tool_use":
                try:
                    args = json.loads(b["json"]) if b["json"] else {}
                except json.JSONDecodeError:
                    args = {}
                r.tool_calls.append(ToolCall(name=b.get("name") or "", arguments=args, id=b.get("id")))
        r.total_s = time.perf_counter() - t0
        return r
