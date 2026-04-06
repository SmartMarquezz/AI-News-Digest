# ══════════════════════════════════════════════════════
# ⚙️  CONFIGURATION — THE ONLY BLOCK YOU EVER NEED TO EDIT
# ══════════════════════════════════════════════════════

# Gmail account where newsletters arrive AND where digest is sent FROM
SOURCE_EMAIL = "learnmindsethub@gmail.com"

# Everyone who gets the morning digest
# HOW TO ADD A RECIPIENT: add a new line → "newemail@example.com",
# HOW TO REMOVE A RECIPIENT: delete their line
# You can have 1 or 100 recipients here — no other code changes needed
DIGEST_RECIPIENTS = [
    "learnmindsethub@gmail.com",
]

# Time window to scan for emails (EST, 24-hour clock)
# All newsletters arrive between these hours on weekdays
START_HOUR = 4  # 4am
START_MINUTE = 30  # :30 → so 4:30am
END_HOUR = 10  # 10am
END_MINUTE = 0  # :00 → so 10:00am

# When digest.py is allowed to run (weekdays; schedule cron to match). This is separate from the
# email window above: we still list and filter messages using 4:30am–10:00am, but the job runs once
# after that window closes so every eligible email is included in one digest.
RUN_DIGEST_HOUR = 10
RUN_DIGEST_MINUTE = 5
RUN_DIGEST_GRACE_MINUTES = 120  # latest start time after RUN_DIGEST_* (late wake / long runs)

# Maximum news bullets to extract per newsletter
# Keeps each source section focused and prevents one newsletter dominating the digest
MAX_BULLETS_PER_SOURCE = 3

# Maximum bullets across the entire digest (safety cap)
MAX_TOTAL_BULLETS = 15

# File names — do not change these unless you move the project
PROCESSED_LOG = "processed_emails.json"  # Tracks email IDs already processed — prevents duplicates
RUN_LOG = "digest_log.txt"  # One line written per run — your run history
ERROR_LOG = "digest_errors.log"  # Written only when something fails

# LLM Settings — Ollama local inference, 100% free
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL = "llama3.2"
OLLAMA_TIMEOUT_SECONDS = 120

import os

GMAIL_MCP_CREDENTIALS = os.path.expanduser("~/.config/gmail-mcp/credentials.json")

# ══════════════════════════════════════════════════════
# END CONFIGURATION — DO NOT EDIT BELOW THIS LINE
# ══════════════════════════════════════════════════════


def is_within_allowed_window():
    now = datetime.now()
    if now.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        return False
    run_start = now.replace(
        hour=RUN_DIGEST_HOUR,
        minute=RUN_DIGEST_MINUTE,
        second=0,
        microsecond=0,
    )
    run_end = run_start + timedelta(minutes=RUN_DIGEST_GRACE_MINUTES)
    return run_start <= now <= run_end


import argparse
import base64
import email.utils
import asyncio
import json
import aiohttp
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, time as dt_time, timedelta
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

# --- Timezone: America/New_York (handles EST/EDT for labels and comparisons)
NY_TZ_NAME = "America/New_York"


def _ny_tz():
    if ZoneInfo is None:
        raise RuntimeError("zoneinfo (Python 3.9+) required")
    return ZoneInfo(NY_TZ_NAME)


# --- MCP HTTP (gmail-mcp server running on localhost:3000)
# Optional overrides: GMAIL_MCP_TOOL_SEARCH, GMAIL_MCP_TOOL_READ, GMAIL_MCP_TOOL_SEND
# Default flavor matches the default package (@shinzolabs/gmail-mcp). For servers that expose
# read_email / send_email (common in other Gmail MCP builds), set GMAIL_MCP_FLAVOR=cursor or
# export GMAIL_MCP_TOOL_* overrides.
_FLAVOR = os.environ.get("GMAIL_MCP_FLAVOR", "shinzo").lower()
if _FLAVOR == "shinzo":
    GMAIL_MCP_TOOL_SEARCH = os.environ.get("GMAIL_MCP_TOOL_SEARCH", "list_messages")
    GMAIL_MCP_TOOL_READ = os.environ.get("GMAIL_MCP_TOOL_READ", "get_message")
    GMAIL_MCP_TOOL_SEND = os.environ.get("GMAIL_MCP_TOOL_SEND", "send_message")
else:
    GMAIL_MCP_TOOL_SEARCH = os.environ.get("GMAIL_MCP_TOOL_SEARCH", "list_messages")
    GMAIL_MCP_TOOL_READ = os.environ.get("GMAIL_MCP_TOOL_READ", "read_email")
    GMAIL_MCP_TOOL_SEND = os.environ.get("GMAIL_MCP_TOOL_SEND", "send_email")


def _now_ny() -> datetime:
    return datetime.now(_ny_tz())


def _error_log_write(func_name: str, message: str, extra: str = "") -> None:
    ts = _now_ny().strftime("%Y-%m-%d %H:%M:%S %Z")
    line = f"[{ts}] | {func_name} | {message}"
    if extra:
        line += f" | {extra}"
    line += "\n"
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        print(f"CRITICAL: could not write {ERROR_LOG}: {e}", file=sys.stderr)


def log_error(func_name: str, message: str) -> None:
    _error_log_write(func_name, message)


def start_ollama() -> None:
    ollama_bin = shutil.which("ollama")
    if not ollama_bin:
        for cand in ("/usr/local/bin/ollama", "/opt/homebrew/bin/ollama"):
            if os.path.exists(cand):
                ollama_bin = cand
                break
    if not ollama_bin:
        _error_log_write("start_ollama", "Could not find 'ollama' binary in PATH")
        return
    subprocess.Popen(
        [ollama_bin, "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(4)


def stop_ollama() -> None:
    subprocess.run(
        ["pkill", "-f", "ollama"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def check_ollama_running() -> bool:
    try:
        req = urllib.request.Request(f"{OLLAMA_BASE_URL}/api/tags")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = [m["name"] for m in data.get("models", [])]
            if not any(OLLAMA_MODEL in m for m in models):
                print(f"WARNING: Model '{OLLAMA_MODEL}' not found.")
                print(f"Available: {models}")
                print(f"Run: ollama pull {OLLAMA_MODEL}")
                return False
            return True
    except Exception:
        print("ERROR: Ollama is not responding after start attempt.")
        return False


OLLAMA_SYSTEM_PROMPT = """SYSTEM PROMPT:

You are a senior intelligence analyst with 20 years of experience briefing executives, researchers, and decision-makers on what actually matters in the world of AI, technology, and science. You do not summarize — you analyze. Your job is to read every newsletter provided, synthesize across all of them, and produce a morning briefing that a busy, highly intelligent professional can act on in under 3 minutes.

Your thinking process (internal, not shown):

Read ALL newsletter content before writing a single word

Ask yourself: "Would a smart person need to know this to make better decisions today?"

Identify which stories appear across multiple sources — cross-source confirmation elevates importance

Separate signal from noise: ignore product launches, sponsored content, listicles, opinion hot takes, and anything that is fundamentally an ad dressed as news

Elevate: verified research findings, regulatory decisions, major capability shifts, geopolitical tech developments, scientific breakthroughs, and market-moving events
Output format — strict, no deviation:
For each item, output exactly this structure:
📰 [Newsletter name the story came from]
[Sharp, specific headline — not a clickbait title, not a vague summary. One sentence that tells the whole story.]
Why it matters: [2-3 sentences explaining the real-world consequence. Who is affected? What changes because of this? What should the reader watch for next? Never restate the headline — add meaning.]
🔗 [Source URL if available, otherwise omit]

Rules:

Produce between 5 and 8 items maximum. If nothing genuinely important happened, say so honestly rather than padding with filler.

Rank items by significance, not by the order they appeared in emails.

If the same story appeared in multiple newsletters, mention that — it signals importance. Merge them into one item.

Never use phrases like "In this newsletter..." or "The author discusses..." — you are an analyst, not a summarizer.

Never hallucinate details not present in the source material.

If a development is early-stage or unverified, say so explicitly.

Write in plain, direct, confident prose. No bullet sub-lists inside items. No emojis except the ones specified in the format.

End the briefing with one sentence: "The thread across today's news:" followed by a single synthesizing observation that connects 2-3 of the top stories into a bigger pattern if one exists. If no pattern exists, omit this section entirely.
"""

# Per-email API: model must still emit JSON for parse_email(); prose briefing format applies to editorial judgment inside fields.
_OLLAMA_JSON_SUFFIX = """
---
SINGLE-EMAIL REQUEST (this call): You are given ONE newsletter below. Apply the SYSTEM PROMPT standards to extract the strongest stories from this email only. Respond with ONLY valid JSON — no markdown code blocks, no prose before or after the JSON object.

NEWSLETTER SOURCE: {sender_name}
SUBJECT: {subject}

FULL EMAIL TEXT:
{cleaned_body}

INSTRUCTIONS:
Return ONLY a valid JSON object. No explanation before or after. No markdown code blocks. Just the raw JSON.

{{
  "stories": [
    {{
      "headline": "COMPANY NAME IN CAPS: one tight sentence describing what happened",
      "summary": "One to two sentences: what happened and exactly why it matters. Be specific. Write like you are briefing a busy CEO.",
      "importance": <integer 1-10>,
      "category": "<one of: MODEL_RELEASE, FUNDING, ACQUISITION, REGULATION, RESEARCH, PRODUCT_LAUNCH, MARKET_MOVE, INSIGHT>"
    }}
  ],
  "is_entirely_promotional": <true or false>
}}

If zero stories are worth including, return: {{"stories": [], "is_entirely_promotional": false}}
If the entire email is ads with no real news, return: {{"stories": [], "is_entirely_promotional": true}}
"""


def build_analysis_prompt(cleaned_body: str, sender_name: str, subject: str) -> str:
    return OLLAMA_SYSTEM_PROMPT + _OLLAMA_JSON_SUFFIX.format(
        sender_name=sender_name, subject=subject, cleaned_body=cleaned_body
    )


def analyze_email_with_ollama(cleaned_body: str, sender_name: str, subject: str) -> Dict[str, Any]:
    prompt = build_analysis_prompt(cleaned_body, sender_name, subject)

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_predict": 1200,
            "top_p": 0.9,
        },
    }

    raw_text = ""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{OLLAMA_BASE_URL}/api/generate",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_SECONDS) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            raw_text = result.get("response", "").strip()

            if raw_text.startswith("```"):
                parts = raw_text.split("```")
                raw_text = parts[1] if len(parts) > 1 else raw_text
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]
            raw_text = raw_text.strip()

            parsed = json.loads(raw_text)
            return parsed

    except urllib.error.URLError as e:
        log_error("analyze_email_with_ollama", f"Ollama not reachable: {e}")
        return {"stories": [], "is_entirely_promotional": False, "error": "ollama_unreachable"}
    except json.JSONDecodeError as e:
        log_error(
            "analyze_email_with_ollama",
            f"JSON parse failed: {e}. Raw: {raw_text[:300]}",
        )
        return {"stories": [], "is_entirely_promotional": False, "error": "json_parse_failed"}
    except Exception as e:
        log_error("analyze_email_with_ollama", f"Unexpected error: {e}")
        return {"stories": [], "is_entirely_promotional": False, "error": str(e)}


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: List[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def get_text(self) -> str:
        return "".join(self._chunks)


def _strip_html(html: str) -> str:
    s = _HTMLStripper()
    try:
        s.feed(html)
        s.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return s.get_text()


def _collapse_blank_lines(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = text.splitlines()
    out: List[str] = []
    for line in lines:
        out.append(line.strip())
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def _remove_urls(text: str) -> str:
    return re.sub(r"https?://\S+", "", text)


def _remove_newsletter_footers(text: str) -> str:
    patterns = [
        r"\n\s*Unsubscribe\b.*",
        r"\n\s*View in browser\b.*",
        r"\n\s*View Online\b.*",
        r"\n\s*You received this\b.*",
        r"\n\s*Our mailing address\b.*",
        r"\n\s*©\s*2\d{3}\b.*",
        r"\n\s*Privacy Policy\b.*",
        r"\n\s*Manage your subscriptions\b.*",
        r"\n\s*Links:\s*\n[-]+.*",
    ]
    low = text
    for p in patterns:
        low = re.sub(p, "\n", low, flags=re.IGNORECASE | re.DOTALL)
    return low


def _remove_image_artifacts(text: str) -> str:
    return re.sub(r"\[(?:image|logo|img)\]", "", text, flags=re.IGNORECASE)


def _ensure_gmail_mcp_running() -> Optional[subprocess.Popen]:
    """Start Gmail MCP HTTP server if not already running on port 3000."""
    # Check if port 3000 is already in use
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("localhost", 3000)) == 0:
            return None  # already running
    # Start it
    npx_bin = shutil.which("npx")
    if not npx_bin:
        for cand in ("/opt/homebrew/bin/npx", "/usr/local/bin/npx"):
            if os.path.exists(cand):
                npx_bin = cand
                break
    if not npx_bin:
        _error_log_write("_ensure_gmail_mcp_running", "Could not find 'npx' binary in PATH")
        return None
    proc = subprocess.Popen(
        [npx_bin, "@shinzolabs/gmail-mcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)  # give it time to bind
    return proc


def _finalize_mcp_tool_result(obj: Any) -> Any:
    """Raise if Gmail MCP embedded a tool-level error (OAuth, etc.) instead of API data."""
    if isinstance(obj, dict) and obj.get("error"):
        raise RuntimeError(f"Gmail MCP tool error: {obj['error']}")
    return obj


class _MCPHTTPSession:
    """Talks to an already-running MCP HTTP server (e.g. gmail-mcp on port 3000)."""

    def __init__(self, base_url: str = "http://localhost:3000/mcp"):
        self.base_url = base_url
        self._session_id: str | None = None

    async def __aenter__(self):
        async with aiohttp.ClientSession() as s:
            resp = await s.post(
                self.base_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2024-11-05",
                                 "capabilities": {},
                                 "clientInfo": {"name": "digest", "version": "1.0"}}},
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            self._session_id = resp.headers.get("Mcp-Session-Id")
        return self

    async def __aexit__(self, *_):
        pass  # HTTP server manages its own lifecycle

    async def call(self, tool: str, params: dict) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id

        async with aiohttp.ClientSession() as s:
            resp = await s.post(
                self.base_url,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": tool, "arguments": params}},
                headers=headers,
            )
            text = await resp.text()

        # Response may be SSE-wrapped: strip "data: " prefix lines
        for line in text.splitlines():
            if line.startswith("data: "):
                import json as _json
                payload = _json.loads(line[6:])
                if "error" in payload:
                    raise RuntimeError(payload["error"].get("message", str(payload["error"])))
                result = payload.get("result", {})
                content = result.get("content", [{}])
                if content and "text" in content[0]:
                    import json as _json2
                    try:
                        inner = _json2.loads(content[0]["text"])
                    except (json.JSONDecodeError, TypeError):
                        return {"raw": content[0]["text"]}
                    return _finalize_mcp_tool_result(inner)
                return _finalize_mcp_tool_result(result)
        return {}


def _mcp_result_to_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts: List[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
            return "\n".join(parts)
        if "text" in result:
            return str(result["text"])
    return json.dumps(result, ensure_ascii=False)


def _mcp_search_args(query: str, max_results: int) -> Dict[str, Any]:
    return {
        "q": query,
        "maxResults": max_results,
        "includeSpamTrash": False,
    }


def _mcp_read_args(message_id: str) -> Dict[str, Any]:
    flavor = os.environ.get("GMAIL_MCP_FLAVOR", "shinzo").lower()
    if flavor == "shinzo":
        return {"id": message_id}
    return {"messageId": message_id}


def _mcp_send_args(to_list: List[str], subject: str, body: str) -> Dict[str, Any]:
    flavor = os.environ.get("GMAIL_MCP_FLAVOR", "shinzo").lower()
    if flavor == "shinzo":
        return {
            "to": to_list,
            "subject": subject,
            "body": body,
            "mimeType": "text/plain",
        }
    return {"to": to_list, "subject": subject, "body": body, "mimeType": "text/plain"}


def _parse_search_json(blob: Any) -> List[Dict[str, str]]:
    entries: List[Dict[str, str]] = []
    if isinstance(blob, dict):
        msgs = blob.get("messages") or blob.get("messageList") or []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            mid = m.get("id") or m.get("messageId") or ""
            hdrs = m.get("payload", {}).get("headers", []) if isinstance(m.get("payload"), dict) else []
            subj = from_h = date_h = ""
            if isinstance(hdrs, list):
                for h in hdrs:
                    if not isinstance(h, dict):
                        continue
                    n = (h.get("name") or "").lower()
                    v = h.get("value") or ""
                    if n == "subject":
                        subj = v
                    elif n == "from":
                        from_h = v
                    elif n == "date":
                        date_h = v
            entries.append(
                {
                    "id": str(mid),
                    "subject": subj,
                    "from": from_h,
                    "date": date_h or m.get("internalDate", ""),
                }
            )
    elif isinstance(blob, list):
        for m in blob:
            if isinstance(m, dict):
                entries.extend(_parse_search_json(m))
    return [e for e in entries if e.get("id")]


def _parse_search_lines(text: str) -> List[Dict[str, str]]:
    """Parse common 'ID: / Subject: / From: / Date:' blocks from MCP text output."""
    entries: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            if current.get("id"):
                entries.append(current)
                current = {}
            continue
        m = re.match(r"^(ID|Subject|From|Date):\s*(.*)$", line, re.I)
        if m:
            key, val = m.group(1).lower(), m.group(2).strip()
            if key == "id":
                if current.get("id"):
                    entries.append(current)
                    current = {}
                current["id"] = val
            else:
                current[key] = val
    if current.get("id"):
        entries.append(current)
    return entries


def _search_result_to_entries(raw_result: Any) -> List[Dict[str, str]]:
    if isinstance(raw_result, dict) and isinstance(raw_result.get("messages"), list):
        parsed = _parse_search_json(raw_result)
        if parsed:
            return parsed
    text = _mcp_result_to_text(raw_result).strip()
    if not text:
        return []
    try:
        j = json.loads(text)
        parsed = _parse_search_json(j)
        if parsed:
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return _parse_search_lines(text)


def _parse_read_email_block(text: str) -> Tuple[str, str, str, str, str]:
    """Returns (id_guess, subject, from_hdr, date_hdr, body)."""
    def _decode_b64url(data: str) -> str:
        if not data:
            return ""
        pad = "=" * ((4 - len(data) % 4) % 4)
        try:
            raw = base64.urlsafe_b64decode((data + pad).encode("utf-8"))
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _extract_body_from_payload(payload: Dict[str, Any]) -> str:
        parts: List[str] = []

        def _walk(node: Dict[str, Any]) -> None:
            mime = str(node.get("mimeType") or "").lower()
            body = node.get("body", {}) if isinstance(node.get("body"), dict) else {}
            data = body.get("data") if isinstance(body, dict) else None
            decoded = _decode_b64url(str(data)) if data else ""
            if decoded:
                if mime == "text/plain":
                    parts.append(decoded)
                elif mime == "text/html":
                    parts.append(_strip_html(decoded))
                elif not mime:
                    parts.append(decoded)
            for ch in node.get("parts", []) if isinstance(node.get("parts"), list) else []:
                if isinstance(ch, dict):
                    _walk(ch)

        _walk(payload)
        merged = "\n\n".join(p.strip() for p in parts if p and p.strip()).strip()
        return merged

    jtxt = text.strip()
    if jtxt.startswith("{"):
        try:
            obj = json.loads(jtxt)
            if isinstance(obj, dict):
                payload = obj.get("payload", {}) if isinstance(obj.get("payload"), dict) else {}
                hdrs = payload.get("headers", []) if isinstance(payload.get("headers"), list) else []
                subject = from_hdr = date_hdr = ""
                for h in hdrs:
                    if not isinstance(h, dict):
                        continue
                    n = (h.get("name") or "").lower()
                    v = str(h.get("value") or "").strip()
                    if n == "subject":
                        subject = v
                    elif n == "from":
                        from_hdr = v
                    elif n == "date":
                        date_hdr = v
                body = _extract_body_from_payload(payload)
                if not body:
                    body = str(obj.get("snippet") or "").strip()
                return (str(obj.get("threadId") or ""), subject, from_hdr, date_hdr, body)
        except (json.JSONDecodeError, TypeError):
            pass
    subject = ""
    from_hdr = ""
    date_hdr = ""
    thread_id = ""
    lines = text.splitlines()
    body_start = 0
    for i, line in enumerate(lines):
        if re.match(r"^Subject:\s*", line, re.I):
            subject = re.sub(r"^Subject:\s*", "", line, flags=re.I).strip()
        elif re.match(r"^From:\s*", line, re.I):
            from_hdr = re.sub(r"^From:\s*", "", line, flags=re.I).strip()
        elif re.match(r"^Date:\s*", line, re.I):
            date_hdr = re.sub(r"^Date:\s*", "", line, flags=re.I).strip()
        elif re.match(r"^Thread ID:\s*", line, re.I):
            thread_id = re.sub(r"^Thread ID:\s*", "", line, flags=re.I).strip()
        elif from_hdr and subject and date_hdr and line.strip() == "":
            body_start = i + 1
            break
    body = "\n".join(lines[body_start:]).strip()
    return (thread_id, subject, from_hdr, date_hdr, body)


def _parse_from_header(from_hdr: str) -> Tuple[str, str]:
    m = re.match(r"^(.*?)\s*<([^>]+)>$", from_hdr.strip())
    if m:
        name = m.group(1).strip().strip('"')
        return name or m.group(2).strip(), m.group(2).strip()
    return from_hdr.strip(), from_hdr.strip()


def _parse_received_at(date_hdr: str) -> datetime:
    dt = email.utils.parsedate_to_datetime(date_hdr)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(_ny_tz())


def _in_window(dt_ny: datetime, day_date: date, win_lo: dt_time, win_hi: dt_time) -> bool:
    if dt_ny.date() != day_date:
        return False
    t = dt_ny.time()
    return win_lo <= t <= win_hi


def fetch_emails_in_window() -> List[Dict[str, Any]]:
    """
    Fetch full content of every inbox message whose Date falls inside today's
    NY window. Uses Gmail MCP (HTTP server). Each MCP call is wrapped in try/except by caller pattern in main;
    this function wraps search + per-message read.
    """
    async def _run() -> List[Dict[str, Any]]:
        ny = _ny_tz()
        now_ny = datetime.now(ny)
        day = now_ny.date()

        after_d = day.strftime("%Y/%m/%d")
        before_d = (day + timedelta(days=1)).strftime("%Y/%m/%d")
        query = f"after:{after_d} before:{before_d}"

        raw_emails: List[Dict[str, Any]] = []
        win_lo = dt_time(START_HOUR, START_MINUTE)
        win_hi = dt_time(END_HOUR, END_MINUTE)

        async with _MCPHTTPSession() as session:
            try:
                raw_result = await session.call(
                    GMAIL_MCP_TOOL_SEARCH,
                    _mcp_search_args(query, max_results=500),
                )
            except Exception as e:
                raise RuntimeError(f"Gmail MCP search failed: {e}") from e

            summaries = _search_result_to_entries(raw_result)

            for summ in summaries:
                mid = summ.get("id", "").strip()
                if not mid:
                    continue
                date_hdr = summ.get("date", "")
                recv: Optional[datetime] = None
                if date_hdr:
                    if date_hdr.isdigit() and len(date_hdr) >= 10:
                        try:
                            ms = int(date_hdr)
                            recv = datetime.fromtimestamp(
                                ms / 1000.0, tz=datetime.timezone.utc
                            ).astimezone(ny)
                        except (OSError, ValueError):
                            try:
                                recv = _parse_received_at(date_hdr)
                            except Exception:
                                recv = None
                    else:
                        try:
                            recv = _parse_received_at(date_hdr)
                        except Exception:
                            recv = None
                if recv is not None and not _in_window(recv, day, win_lo, win_hi):
                    continue
                try:
                    r = await session.call(
                        GMAIL_MCP_TOOL_READ,
                        _mcp_read_args(mid),
                    )
                except Exception as e:
                    _error_log_write(
                        "fetch_emails_in_window",
                        str(e),
                        extra=f"messageId={mid} subject={summ.get('subject','')}",
                    )
                    continue
                body_text = _mcp_result_to_text(r)
                _, subj, from_h, date_h, body = _parse_read_email_block(body_text)
                try:
                    recv2 = _parse_received_at((date_h or date_hdr).strip() or date_hdr)
                except Exception:
                    if recv is not None:
                        recv2 = recv
                    else:
                        continue
                if not _in_window(recv2, day, win_lo, win_hi):
                    continue
                raw_emails.append(
                    {
                        "id": mid,
                        "search_summary": summ,
                        "read_text": body_text,
                        "subject": subj or summ.get("subject", ""),
                        "from_header": from_h or summ.get("from", ""),
                        "date_header": date_h or date_hdr,
                        "body": body,
                    }
                )
        return raw_emails

    return asyncio.run(_run())


def filter_unprocessed(emails: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    processed: List[str] = []
    try:
        if os.path.exists(PROCESSED_LOG):
            with open(PROCESSED_LOG, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                processed = [str(x) for x in data]
            else:
                _error_log_write(
                    "filter_unprocessed",
                    "processed_emails.json was not a JSON array — resetting",
                    extra="",
                )
                processed = []
    except (json.JSONDecodeError, OSError) as e:
        _error_log_write(
            "filter_unprocessed",
            f"Could not load {PROCESSED_LOG}: {e} — starting fresh",
            extra="",
        )
        processed = []
        try:
            with open(PROCESSED_LOG, "w", encoding="utf-8") as f:
                json.dump([], f)
        except OSError as w:
            _error_log_write("filter_unprocessed", f"Could not recreate {PROCESSED_LOG}: {w}", "")
    ps = set(processed)
    return [e for e in emails if e.get("id") not in ps]


def _company_caps(headline: str, segment: str) -> str:
    m = re.match(r"^([^:]+):", headline)
    if m:
        core = m.group(1).strip()
    else:
        core = headline
    core = re.sub(r"^\W+", "", core)
    words = re.findall(r"[A-Za-z][A-Za-z0-9]+", core)
    if not words:
        words = re.findall(r"[A-Za-z][A-Za-z0-9]+", segment[:200])
    pick = " ".join(words[:5]).upper()
    if len(pick) > 80:
        pick = pick[:80].rsplit(" ", 1)[0]
    return pick or "NEWS"


def parse_email(raw_email: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """Returns (parsed_dict or None, skip_reason). skip_reason is '' on success."""
    eid = str(raw_email["id"])
    from_hdr = raw_email.get("from_header", "")
    sender_name, sender_email = _parse_from_header(from_hdr)
    subject = raw_email.get("subject", "")
    date_hdr = raw_email.get("date_header", "")
    try:
        recv = _parse_received_at(date_hdr)
    except (TypeError, ValueError):
        recv = datetime.now(_ny_tz())
    body = raw_email.get("body", "")
    body = _strip_html(body)
    body = _remove_urls(body)
    body = _remove_newsletter_footers(body)
    body = _remove_image_artifacts(body)
    body = _collapse_blank_lines(body)

    llm = analyze_email_with_ollama(body, sender_name, subject)
    err = llm.get("error")
    if err == "ollama_unreachable":
        _error_log_write(
            "parse_email",
            "Ollama unreachable",
            extra=f"subject={subject} from={from_hdr}",
        )
        return None, "ollama_unreachable"
    if err:
        _error_log_write(
            "parse_email",
            f"LLM error: {err}",
            extra=f"subject={subject} from={from_hdr}",
        )
        return None, "llm_error"
    if llm.get("is_entirely_promotional"):
        _error_log_write(
            "parse_email",
            "Skipped: is_entirely_promotional",
            extra=f"subject={subject} from={from_hdr}",
        )
        return None, "promotional"

    stories: List[Dict[str, Any]] = []
    for s in llm.get("stories") or []:
        headline = (s.get("headline") or "").strip()
        summary = (s.get("summary") or "").strip()
        if not headline and not summary:
            continue
        imp = int(s.get("importance", 5))
        stories.append(
            {
                "headline": headline,
                "summary": summary,
                "importance": imp,
                "importance_score": imp,
                "company_caps": _company_caps(headline or summary, headline + " " + summary),
                "dedupe_key": re.sub(
                    r"[^a-z0-9]+", " ", (headline or summary).lower()
                ).strip(),
                "category": s.get("category", ""),
            }
        )

    return (
        {
            "id": eid,
            "sender_name": sender_name,
            "sender_email": sender_email,
            "subject": subject,
            "received_at": recv,
            "stories": stories,
        },
        "",
    )


def cap_and_select(
    parsed_emails: List[Dict[str, Any]]
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Returns (top_story dict or None, list of source buckets with capped stories)."""
    buckets: List[Dict[str, Any]] = []
    all_scored: List[Tuple[int, str, Dict[str, Any], Dict[str, Any]]] = []
    for pe in parsed_emails:
        stories = sorted(
            pe.get("stories") or [], key=lambda s: s.get("importance", s.get("importance_score", 0)), reverse=True
        )[:MAX_BULLETS_PER_SOURCE]
        if not stories:
            continue
        b = {
            "sender_name": pe["sender_name"],
            "sender_email": pe["sender_email"],
            "email_id": pe["id"],
            "stories": [],
        }
        for idx, st in enumerate(stories):
            st2 = dict(st)
            uid = f"{pe['id']}:{idx}"
            st2["_story_uid"] = uid
            b["stories"].append(st2)
            all_scored.append((st2.get("importance", st2.get("importance_score", 0)), uid, st2, b))
        buckets.append(b)

    if not all_scored:
        return None, []

    _top_score, top_uid, _top_story_obj, top_bucket = max(all_scored, key=lambda x: x[0])
    top_story_obj = next(s for s in top_bucket["stories"] if s.get("_story_uid") == top_uid)
    top_payload = {
        "story": dict(top_story_obj),
        "source_name": top_bucket["sender_name"],
        "source_email": top_bucket["sender_email"],
    }
    top_payload["story"].pop("_story_uid", None)

    for b in buckets:
        b["stories"] = [s for s in b["stories"] if s.get("_story_uid") != top_uid]

    flat: List[Tuple[int, str, Dict[str, Any], Dict[str, Any]]] = []
    for b in buckets:
        for s in b["stories"]:
            flat.append(
                (
                    s.get("importance", s.get("importance_score", 0)),
                    s["_story_uid"],
                    s,
                    b,
                )
            )
    flat.sort(key=lambda x: (-x[0], x[1]))
    kept_uids = {uid for _, uid, _, _ in flat[:MAX_TOTAL_BULLETS]}
    for b in buckets:
        b["stories"] = [s for s in b["stories"] if s.get("_story_uid") in kept_uids]
    for b in buckets:
        for s in b["stories"]:
            s.pop("_story_uid", None)

    return top_payload, buckets


def _dedupe_tokset(dedupe_key: str) -> frozenset:
    return frozenset(w for w in dedupe_key.split() if len(w) > 3)


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def deduplicate_stories(
    ranked_emails: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], int]:
    """Merge near-duplicate stories across sources; returns (updated buckets, merge_count)."""
    merge_count = 0
    items: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for b in ranked_emails:
        for s in list(b.get("stories", [])):
            items.append((b, s))
    items.sort(
        key=lambda x: (
            -x[1].get("importance", x[1].get("importance_score", 0)),
            x[0].get("sender_name", ""),
        )
    )

    clusters: List[Dict[str, Any]] = []

    for b, s in items:
        ts = _dedupe_tokset(s.get("dedupe_key") or "")
        matched: Optional[Dict[str, Any]] = None
        for cl in clusters:
            if _jaccard(ts, cl["toks"]) >= 0.55:
                matched = cl
                break
        if matched is None:
            clusters.append(
                {
                    "toks": ts,
                    "winner_bucket": b,
                    "winner_story": s,
                    "sources": {b["sender_name"]},
                }
            )
            continue

        matched["sources"].add(b["sender_name"])
        wb = matched["winner_bucket"]
        ws = matched["winner_story"]
        if s is ws and b is wb:
            continue
        si = s.get("importance", s.get("importance_score", 0))
        wi = ws.get("importance", ws.get("importance_score", 0))
        if si > wi:
            if ws in wb.get("stories", []):
                wb["stories"].remove(ws)
            matched["winner_bucket"] = b
            matched["winner_story"] = s
            matched["toks"] = ts
        else:
            if s in b.get("stories", []):
                b["stories"].remove(s)
        merge_count += 1

    for cl in clusters:
        names = sorted(cl["sources"])
        if len(names) > 1:
            st = cl["winner_story"]
            summ = (st.get("summary") or "").rstrip()
            suffix = f" (covered by {len(names)} sources)"
            if suffix not in summ:
                if len(summ) + len(suffix) > 280:
                    st["summary"] = summ[: max(0, 280 - len(suffix) - 1)] + "…" + suffix
                else:
                    st["summary"] = summ + suffix

    for b in ranked_emails:
        b["stories"] = [s for s in b.get("stories", []) if s]

    return ranked_emails, merge_count


def _fmt_bullet(company: str, summary: str) -> str:
    s = re.sub(r"\s+", " ", summary).strip()
    return f"• {company}: {s}"


def _top_story_two_sentences(summary: str, headline: str) -> str:
    text = re.sub(r"\s+", " ", (summary or headline or "").strip()).strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1]}"
    if len(text) > 200:
        mid = text.rfind(" ", 40, 180)
        if mid == -1:
            mid = 120
        return f"{text[:mid].strip()}. {text[mid:].strip()}"
    return text


def build_digest(
    top_story: Optional[Dict[str, Any]],
    ranked_emails: List[Dict[str, Any]],
    stats: Dict[str, Any],
) -> str:
    ny = _ny_tz()
    now = datetime.now(ny)

    lines: List[str] = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🔥 TOP STORY")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    if top_story:
        st = top_story["story"]
        comp = st.get("company_caps", "NEWS")
        summ = _top_story_two_sentences(
            (st.get("summary") or "").strip(), (st.get("headline") or "").strip()
        )
        lines.append(_fmt_bullet(comp, summ))
        lines.append(f"  📰 Source: {top_story['source_name']}")
    lines.append("")
    for b in sorted(
        ranked_emails,
        key=lambda x: -(
            sum(s.get("importance", s.get("importance_score", 0)) for s in x.get("stories", []))
            / max(1, len(x.get("stories", [])))
        ),
    ):
        stories = [s for s in b.get("stories", []) if s]
        if not stories:
            continue
        name_u = (b.get("sender_name") or "NEWSLETTER").upper()
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"📰 {name_u}")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("")
        for st in stories:
            lines.append(_fmt_bullet(st.get("company_caps", "NEWS"), st.get("summary", "")))
        lines.append("")

    srcs = stats.get("source_names", [])
    dup = stats.get("duplicate_merges", 0)
    failed_parse = stats.get("parse_failures", 0)
    skipped_promo = stats.get("emails_skipped_promotional", 0)
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("📊 DIGEST STATS")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append(f"📬 Emails processed: {stats.get('emails_processed', 0)}")
    lines.append(f"🗞️ Sources today: {', '.join(srcs) if srcs else '(none)'}")
    lines.append("⏱️ Time window scanned: 4:30am – 10:00am EST")
    lines.append(
        f"🤖 Intelligence: Ollama local LLM ({OLLAMA_MODEL}) — 100% private, zero cost"
    )
    lines.append("⏰ Active window: Mon–Fri 4:30am–10:00am only")
    lines.append(f"⏭️  Emails skipped (fully promotional): {skipped_promo}")
    if failed_parse:
        lines.append(
            f"⚠️ {failed_parse} email(s) failed to parse — see digest_errors.log"
        )
    lines.append(f"🔁 Duplicate stories merged: {dup}")
    lines.append(f"📤 Digest sent to: {stats.get('recipients', 0)} recipient(s)")
    done = now.strftime("%I:%M %p %Z").replace("  ", " ")
    lines.append(f"✅ Agent completed at: {done}")
    return "\n".join(lines).strip() + "\n"


def _send_email_mcp(subject: str, body: str) -> Dict[str, Any]:
    async def _run() -> Dict[str, Any]:
        results = {"ok": [], "fail": []}
        async with _MCPHTTPSession() as session:
            for recipient in DIGEST_RECIPIENTS:
                try:
                    await session.call(
                        GMAIL_MCP_TOOL_SEND,
                        _mcp_send_args([recipient], subject, body),
                    )
                    results["ok"].append(recipient)
                except Exception as e:
                    _error_log_write(
                        "_send_email_mcp",
                        str(e),
                        extra=f"recipient={recipient}",
                    )
                    results["fail"].append((recipient, str(e)))
        return results

    return asyncio.run(_run())


def send_digest(digest_text: str) -> Dict[str, Any]:
    ny = _now_ny()
    subject = (
        f"AI Morning Digest - {ny.strftime('%A, %B ')}{int(ny.strftime('%d'))}, {ny.strftime('%Y')} "
        f"[{ny.strftime('%Y%m%d')}]"
    )
    return _send_email_mcp(subject, digest_text)


def update_processed_log(processed_ids: List[str]) -> None:
    cur: List[str] = []
    try:
        if os.path.exists(PROCESSED_LOG):
            with open(PROCESSED_LOG, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                cur = [str(x) for x in data]
    except (json.JSONDecodeError, OSError):
        cur = []
    cur_set = set(cur)
    for pid in processed_ids:
        cur_set.add(pid)
    try:
        with open(PROCESSED_LOG, "w", encoding="utf-8") as f:
            json.dump(sorted(cur_set), f, indent=2)
    except OSError as e:
        _error_log_write("update_processed_log", str(e), "")


def log_run(stats: Dict[str, Any]) -> None:
    ts = _now_ny().strftime("%Y-%m-%d %H:%M:%S %Z")
    line = (
        f"[{ts}] | emails_found={stats.get('emails_found', 0)} "
        f"| emails_processed={stats.get('emails_processed', 0)} "
        f"| bullets_generated={stats.get('bullets_generated', 0)} "
        f"| recipients={stats.get('recipients_ok', stats.get('recipients_attempted', 0))} "
        f"| status={stats.get('status', 'UNKNOWN')}\n"
    )
    try:
        with open(RUN_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        _error_log_write("log_run", str(e), "")


def _digest_log_lines_for_date(date_str: str) -> List[str]:
    out: List[str] = []
    if not os.path.exists(RUN_LOG):
        return out
    try:
        with open(RUN_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"[{date_str}"):
                    out.append(line)
    except OSError:
        pass
    return out


def _log_line_status(line: str) -> str:
    m = re.search(r"\|\s*status=(\S+)", line)
    return m.group(1).strip() if m else "UNKNOWN"


def _digest_run_logged_success_today() -> bool:
    """True if digest_log already has any SUCCESS line for today's NY date."""
    today = _now_ny().strftime("%Y-%m-%d")
    for line in _digest_log_lines_for_date(today):
        if _log_line_status(line) == "SUCCESS":
            return True
    return False


def _tail_file(path: str, max_chars: int = 6000) -> str:
    if not os.path.exists(path):
        return f"(no {path})"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = f.read()
        return data[-max_chars:] if len(data) > max_chars else data
    except OSError as e:
        return f"(could not read {path}: {e})"


def run_digest_post_check() -> int:
    """Optional 10:10 cron: email if today's digest did not log SUCCESS (or no log at all)."""
    today = _now_ny().strftime("%Y-%m-%d")
    lines = _digest_log_lines_for_date(today)
    if not lines:
        time.sleep(120)
        lines = _digest_log_lines_for_date(today)

    mcp_proc = _ensure_gmail_mcp_running()
    try:
        if not lines:
            subj = f"⚠️ Digest post-check — no run logged ({today})"
            body = (
                f"No line was written to {RUN_LOG} for {today} after waiting for the digest job. "
                f"The agent may not have run (Mac asleep, cron, or it exited before logging—e.g. Ollama down).\n\n"
                f"Expected digest cron: ~{RUN_DIGEST_HOUR:02d}:{RUN_DIGEST_MINUTE:02d} weekdays.\n\n"
                f"--- {ERROR_LOG} (tail) ---\n"
                f"{_tail_file(ERROR_LOG)}"
            )
            _send_email_mcp(subj, body)
            return 0

        last = lines[-1]
        st = _log_line_status(last)
        if st == "SUCCESS":
            print("Digest post-check: OK (latest log status SUCCESS).", flush=True)
            return 0

        if st == "FAILED":
            subj = f"⚠️ Digest post-check — run FAILED ({today})"
            body = (
                f"Latest line in {RUN_LOG} for today:\n{last}\n\n"
                f"--- {ERROR_LOG} (tail) ---\n"
                f"{_tail_file(ERROR_LOG)}"
            )
        elif st == "PARTIAL":
            subj = f"📋 Digest post-check — PARTIAL send ({today})"
            body = (
                f"Latest line for today:\n{last}\n\n"
                f"Some recipients may have failed. Review {ERROR_LOG}.\n\n"
                f"--- {ERROR_LOG} (tail) ---\n"
                f"{_tail_file(ERROR_LOG)}"
            )
        else:
            subj = f"⚠️ Digest post-check — unclear status ({today})"
            body = f"Latest line:\n{last}\n\n{_tail_file(ERROR_LOG)}"

        _send_email_mcp(subj, body)
        return 0
    finally:
        if mcp_proc is not None:
            try:
                mcp_proc.terminate()
                mcp_proc.wait(timeout=5)
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Morning Digest agent")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and build digest, print to stdout, do not send email or update logs",
    )
    parser.add_argument(
        "--post-check",
        action="store_true",
        help="Check digest_log for today's run; email if missing or not SUCCESS (for ~10:10 cron).",
    )
    parser.add_argument(
        "--catch-up",
        action="store_true",
        help="Weekday backup: run full digest only if digest_log has no SUCCESS line for today yet.",
    )
    args = parser.parse_args()
    dry = args.dry_run

    _force = os.environ.get("DIGEST_FORCE_RUN", "").strip().lower() in ("1", "true", "yes")

    if args.post_check:
        if not _force and datetime.now().weekday() >= 5:
            return 0
        return run_digest_post_check()

    if args.catch_up:
        if not _force and datetime.now().weekday() >= 5:
            return 0
        if _digest_run_logged_success_today():
            print(
                "Catch-up: digest already logged SUCCESS today; skipping duplicate run.",
                flush=True,
            )
            return 0
        # Allow run outside the normal window (e.g. Mac woke late); weekdays only above.
    elif not is_within_allowed_window() and not _force:
        _end_mins = RUN_DIGEST_HOUR * 60 + RUN_DIGEST_MINUTE + RUN_DIGEST_GRACE_MINUTES
        _end_h, _end_mi = divmod(_end_mins, 60)
        print(
            f"Outside allowed window (Mon–Fri digest run {RUN_DIGEST_HOUR:02d}:{RUN_DIGEST_MINUTE:02d}–"
            f"{_end_h:02d}:{_end_mi:02d} local). Exiting."
        )
        return 0

    stats: Dict[str, Any] = {
        "emails_found": 0,
        "emails_processed": 0,
        "emails_skipped_promotional": 0,
        "bullets_generated": 0,
        "recipients": len(DIGEST_RECIPIENTS),
        "recipients_attempted": len(DIGEST_RECIPIENTS),
        "parse_failures": 0,
        "duplicate_merges": 0,
        "source_names": [],
        "status": "SUCCESS",
    }

    try:
        mcp_proc = _ensure_gmail_mcp_running()
        start_ollama()
        if not check_ollama_running():
            subj = "⚠️ Digest Agent Error — Ollama not running"
            body = (
                f"Ollama did not become ready at {OLLAMA_BASE_URL} after start_ollama() "
                f"at {_now_ny().strftime('%I:%M %p %Z')}. Check digest_errors.log."
            )
            if not dry:
                _send_email_mcp(subj, body)
                stats["status"] = "FAILED"
                log_run(stats)
            else:
                print(subj)
                print(body)
            return 1

        try:
            # STEP 2 — FETCH EMAILS
            try:
                raw_list = fetch_emails_in_window()
            except Exception as e:
                _error_log_write("fetch_emails_in_window", str(e), "")
                subj = f"⚠️ Digest Agent Error — {_now_ny().strftime('%B %d, %Y')}"
                body = (
                    f"The digest agent failed to read emails from {SOURCE_EMAIL} at "
                    f"{_now_ny().strftime('%I:%M %p %Z')}. Error: {e}. "
                    f"Please check digest_errors.log for details."
                )
                if dry:
                    print(subj, file=sys.stderr)
                    print(body, file=sys.stderr)
                try:
                    alert_res = _send_email_mcp(subj, body)
                    if not dry:
                        stats["recipients_ok"] = len(alert_res.get("ok", []))
                except Exception as se:
                    _error_log_write("main", f"Could not send alert email: {se}", "")
                    if dry:
                        print(f"(Alert email not sent: {se})", file=sys.stderr)
                    if not dry:
                        stats["recipients_ok"] = 0
                if not dry:
                    stats["status"] = "FAILED"
                    log_run(stats)
                return 1

            stats["emails_found"] = len(raw_list)
            # STEP 3 — DEDUPLICATE
            unproc = filter_unprocessed(raw_list)
            stats["emails_found_window_unprocessed"] = len(unproc)

            ny = _now_ny()
            if len(raw_list) == 0:
                subj = f"⚠️ No newsletters found — {ny.strftime('%B %d, %Y')}"
                body = (
                    f"No emails were received in {SOURCE_EMAIL} between 4:30am–10:00am EST today "
                    f"({ny.strftime('%B %d, %Y')}). The agent ran and checked successfully at "
                    f"{ny.strftime('%I:%M %p %Z')}."
                )
                if dry:
                    print(subj)
                    print(body)
                    return 0
                send_res = _send_email_mcp(subj, body)
                stats["recipients_ok"] = len(send_res.get("ok", []))
                if len(send_res.get("fail", [])) == len(DIGEST_RECIPIENTS):
                    _error_log_write(
                        "main",
                        "All sends failed for no-newsletters notice",
                        extra=body,
                    )
                    stats["status"] = "FAILED"
                    log_run(stats)
                    return 1
                stats["emails_processed"] = 0
                stats["bullets_generated"] = 0
                log_run(stats)
                return 0

            if len(unproc) == 0:
                print(
                    "All emails in today's window were already processed; no new digest needed."
                )
                if not dry:
                    stats["status"] = "SUCCESS"
                    stats["emails_processed"] = 0
                    stats["bullets_generated"] = 0
                    stats["recipients_ok"] = 0
                    log_run(stats)
                return 0

            parsed_list: List[Dict[str, Any]] = []
            processed_ids: List[str] = []
            ollama_unreachable_count = 0

            # STEP 4 — PARSE EACH EMAIL (LLM)
            for raw in unproc:
                try:
                    parsed, skip_reason = parse_email(raw)
                except Exception as e:
                    stats["parse_failures"] += 1
                    _error_log_write(
                        "parse_email",
                        str(e),
                        extra=f"subject={raw.get('subject','')} from={raw.get('from_header','')}",
                    )
                    continue

                if parsed is None:
                    if skip_reason == "promotional":
                        stats["emails_skipped_promotional"] += 1
                        processed_ids.append(str(raw["id"]))
                    elif skip_reason == "ollama_unreachable":
                        ollama_unreachable_count += 1
                    elif skip_reason == "llm_error":
                        stats["parse_failures"] += 1
                    continue

                parsed_list.append(parsed)
                processed_ids.append(parsed["id"])

            if unproc and not parsed_list and ollama_unreachable_count == len(unproc):
                subj = "⚠️ Digest Agent Error — Ollama not running"
                body = (
                    f"Ollama was unreachable for every newsletter email at "
                    f"{_now_ny().strftime('%I:%M %p %Z')}. No digest was produced. "
                    f"See digest_errors.log."
                )
                if not dry:
                    _send_email_mcp(subj, body)
                    stats["status"] = "FAILED"
                    log_run(stats)
                else:
                    print(subj)
                    print(body)
                return 1

            # STEP 5 — CAP AND SELECT
            top_story, ranked = cap_and_select(parsed_list)
            # STEP 6 — DEDUPLICATE STORIES
            ranked, dmerge = deduplicate_stories(ranked)
            stats["duplicate_merges"] = dmerge
            stats["source_names"] = sorted({p["sender_name"] for p in parsed_list})
            bullets = 0
            if top_story:
                bullets += 1
            for b in ranked:
                bullets += len(b.get("stories", []))
            stats["bullets_generated"] = bullets
            stats["emails_processed"] = len(parsed_list)

            # STEP 7 — BUILD DIGEST
            digest_text = build_digest(top_story, ranked, stats)

            if dry:
                print(
                    "SUBJECT: 🤖 AI Morning Digest — "
                    f"{ny.strftime('%A, %B ')}{int(ny.strftime('%d'))}, {ny.strftime('%Y')}"
                )
                print()
                print(digest_text)
                print()
                print(
                    f"[dry-run] emails_fetched={len(raw_list)} unprocessed={len(unproc)} "
                    f"parsed={len(parsed_list)} bullets={bullets}"
                )
                if top_story:
                    ts = top_story["story"]
                    print(
                        f"[dry-run] TOP STORY: importance={ts.get('importance', ts.get('importance_score'))} "
                        f"headline={ts.get('headline','')[:100]!r}"
                    )
                for b in ranked:
                    print(
                        f"[dry-run] Source {b.get('sender_name')!r}: "
                        f"{len(b.get('stories',[]))} stories after cap/dedupe"
                    )
                return 0

            # STEP 8 — SEND DIGEST
            send_res = send_digest(digest_text)
            stats["recipients_ok"] = len(send_res.get("ok", []))
            if len(send_res.get("ok", [])) == 0:
                _error_log_write(
                    "main",
                    "All digest sends failed; full digest preserved below",
                    extra=digest_text[:20000],
                )
                stats["status"] = "FAILED"
                log_run(stats)
                return 1

            # STEP 9 — UPDATE PROCESSED LOG
            update_processed_log(processed_ids)
            if send_res.get("fail"):
                stats["status"] = "PARTIAL"
                _error_log_write(
                    "main",
                    "Some recipients failed",
                    extra=str(send_res["fail"]),
                )
            # STEP 10 — LOG THE RUN
            log_run(stats)
            return 0
        finally:
            pass

    finally:
        if mcp_proc is not None:
            try:
                mcp_proc.terminate()
                mcp_proc.wait(timeout=5)
            except Exception:
                pass
        stop_ollama()


if __name__ == "__main__":
    raise SystemExit(main())
