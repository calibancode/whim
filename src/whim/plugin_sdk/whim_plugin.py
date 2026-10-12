# SPDX-License-Identifier: MIT
#
# MIT License
#
# Copyright (c) 2026 Whim contributors
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""whim_plugin: a tiny, standard-library-only helper for writing whim plugins.

A plugin is a small MCP stdio server. This helper speaks the needed subset of the
protocol (initialize, tools/list, tools/call) so a plugin is just:

    from whim_plugin import Plugin

    plugin = Plugin("wordcount")

    @plugin.tool
    def count(text: str) -> dict:
        "Count the words and characters in a text."
        return {"words": len(text.split()), "characters": len(text)}

    plugin.run()

Parameters become the tool's JSON schema from their type hints (str, int, float, bool,
list, dict); parameters with defaults are optional. The docstring is the description.
Return any JSON-serialisable value; raise an exception to report an error. Inside the
sandbox, /data is the plugin's own persistent folder; there is no network and no access
to the character's files unless that permission was granted.
"""

import inspect
import json
import sys
import traceback

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}


class Plugin:
    def __init__(self, name, version="0.1.0"):
        self.name, self.version, self.tools = name, version, {}

    def tool(self, fn=None, *, description=None):
        def register(f):
            try:  # resolves string annotations (`from __future__ import annotations`)
                sig = inspect.signature(f, eval_str=True)
            except Exception:  # a name that only exists for type checkers
                sig = inspect.signature(f)
            props, required = {}, []
            for pname, param in sig.parameters.items():
                schema = {"type": TYPES.get(param.annotation, "string")}
                if param.default is inspect.Parameter.empty:
                    required.append(pname)
                else:
                    schema["default"] = param.default
                props[pname] = schema
            self.tools[f.__name__] = {
                "fn": f,
                "spec": {"name": f.__name__,
                         "description": (description or inspect.getdoc(f) or "").strip(),
                         "inputSchema": {"type": "object", "properties": props, "required": required,
                                         "additionalProperties": False}},
            }
            return f
        return register(fn) if fn is not None else register

    def _handle(self, msg):
        method, params = msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": self.name, "version": self.version}}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [t["spec"] for t in self.tools.values()]}
        if method == "tools/call":
            tool = self.tools.get(params.get("name"))
            if tool is None:
                raise LookupError(f"unknown tool {params.get('name')!r}")
            try:
                value = tool["fn"](**(params.get("arguments") or {}))
            except Exception as e:  # reported to the caller as a tool error, not a crash
                return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            out = {"content": [{"type": "text", "text": text}], "isError": False}
            if isinstance(value, dict):
                out["structuredContent"] = value
            return out
        raise LookupError(f"method not found: {method}")

    def run(self):
        for line in sys.stdin:
            if not line.strip():
                continue
            msg = json.loads(line)
            if "id" not in msg:
                continue  # notifications need no answer
            try:
                reply = {"jsonrpc": "2.0", "id": msg["id"], "result": self._handle(msg)}
            except LookupError as e:
                reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": str(e)}}
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}}
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
