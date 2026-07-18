#!__TOOL_PYTHON__
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


TOP_HELP = """Usage: browser-act [OPTIONS] COMMAND [ARGS]...
--format {json,text}
stealth-extract <url>
browser open <id> [url]
wait stable [--timeout]
get html
get markdown
state
scroll <direction> [--amount]
session close [name]
"""
HELP = {
    "stealth-extract": "--content-type --timeout --render-wait --custom-proxy --output",
    "browser": "open list create update delete renew import-profile",
    "wait": "stable selector --timeout",
    "get": "html markdown text value",
    "state": "usage: browser-act state [-h]",
    "scroll": "direction --amount",
    "session": "list close",
}


def _record(argv: list[str]) -> None:
    record = Path(sys.argv[0]).parent.parent / "argv.jsonl"
    with record.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(argv) + "\n")


def _last_browser_id() -> str:
    record = Path(sys.argv[0]).parent.parent / "argv.jsonl"
    for line in reversed(record.read_text(encoding="utf-8").splitlines()):
        argv = json.loads(line)
        if len(argv) >= 7 and argv[4:6] == ["browser", "open"]:
            return argv[6]
    return ""


def main() -> int:
    argv = sys.argv[1:]
    _record(argv)
    if argv == ["--version"]:
        print(f"browser-act {os.environ.get('FAKE_BROWSERACT_VERSION', '1.0.6')}")
        return 0
    if argv == ["--help"]:
        print(TOP_HELP)
        return 0
    if len(argv) == 2 and argv[1] == "--help" and argv[0] in HELP:
        print(HELP[argv[0]])
        return 0
    if argv[:2] != ["--format", "json"]:
        return 2
    rest = argv[2:]
    if rest and rest[0] == "stealth-extract":
        if len(rest) != 8 or rest[2] != "--content-type" or rest[4] != "--timeout" or rest[6] != "--render-wait":
            return 2
        content_type = rest[3]
        url = rest[1]
        if "oversized-stdout" in url:
            sys.stdout.write("x" * (4 * 1024 * 1024 + 1))
        elif "oversized-stderr" in url:
            sys.stderr.write("secret-diagnostic-" * 5000)
            print(json.dumps({"content": "stealth_extract", "final_url": "https://redirect.example/final"}))
        elif "real-106-url-only" in url:
            print(json.dumps({"url": url, "content_type": content_type, "content": "stealth_extract"}))
        elif "duplicate-content" in url:
            print('{"content":"first","content":"second","final_url":"https://redirect.example/final"}')
        elif "duplicate-final-url" in url:
            print('{"content":"stealth_extract","data":{"final_url":"https://redirect.example/one","final_url":"https://redirect.example/two"}}')
        elif "ambiguous-url" in url:
            print(json.dumps({"content": "stealth_extract", "final_url": "https://redirect.example/final",
                              "current_url": "https://redirect.example/current", "url": url}))
        else:
            print(json.dumps({"content": f"<{content_type}>stealth_extract</{content_type}>", "final_url": "https://redirect.example/final"}))
        return 0
    if len(rest) < 3 or rest[:1] != ["--session"]:
        return 2
    command = rest[2:]
    if command[:2] == ["browser", "open"] and len(command) == 4:
        if command[2] == "open-timeout":
            import time
            time.sleep(2)
        if command[2] == "open-malformed":
            print("not-json"); return 0
        print(json.dumps({"data": {"ok": True}})); return 0
    if command[:2] == ["wait", "stable"] and len(command) == 4 and command[2] == "--timeout":
        print(json.dumps({"data": {"ok": True}})); return 0
    if command[:1] == ["scroll"] and len(command) == 4 and command[2] == "--amount":
        print(json.dumps({"data": {"ok": True}})); return 0
    if command[:1] == ["get"] and len(command) == 2 and command[1] in {"html", "markdown"}:
        print(json.dumps({"content": f"<{command[1]}>interactive_read</{command[1]}>"})); return 0
    if command == ["state"]:
        if _last_browser_id() == "duplicate-state":
            print('{"url":"https://redirect.example/one","url":"https://redirect.example/two"}')
        else:
            print(json.dumps({"ok": True, "url": "https://redirect.example/interactive",
                              "title": "Example", "text": "interactive_read"}))
        return 0
    if command[:2] == ["session", "close"] and len(command) == 3:
        if _last_browser_id() == "close-failure":
            return 1
        print(json.dumps({"data": {"ok": True}})); return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
