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

# Which days of the week to run (0=Monday through 6=Sunday)
# Default: weekdays only — do not run on weekends
ACTIVE_DAYS = [0, 1, 2, 3, 4]

# Maximum news bullets to extract per newsletter
# Keeps each source section focused and prevents one newsletter dominating the digest
MAX_BULLETS_PER_SOURCE = 3

# Maximum bullets across the entire digest (safety cap)
MAX_TOTAL_BULLETS = 15

# File names — do not change these unless you move the project
PROCESSED_LOG = "processed_emails.json"  # Tracks email IDs already processed — prevents duplicates
RUN_LOG = "digest_log.txt"  # One line written per run — your run history
ERROR_LOG = "digest_errors.log"  # Written only when something fails

# ══════════════════════════════════════════════════════
# END CONFIGURATION — DO NOT EDIT BELOW THIS LINE
# ══════════════════════════════════════════════════════

import argparse
import email.utils
import json
import os
import re
import subprocess
import sys
import threading
from datetime import date, datetime, time as dt_time
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


# --- MCP stdio (Gmail MCP server subprocess). Configure for unattended runs:
# GMAIL_MCP_COMMAND: executable, e.g. npx
# GMAIL_MCP_ARGS: space-separated args, e.g. -y @scope/package
# Optional overrides: GMAIL_MCP_TOOL_SEARCH, GMAIL_MCP_TOOL_READ, GMAIL_MCP_TOOL_SEND
GMAIL_MCP_COMMAND = os.environ.get("GMAIL_MCP_COMMAND", "npx")
GMAIL_MCP_ARGS = os.environ.get("GMAIL_MCP_ARGS", "-y @shinzolabs/gmail-mcp").split()
# Default flavor matches the default package (@shinzolabs/gmail-mcp). For servers that expose
# search_emails / read_email / send_message (common in other Gmail MCP builds), set
# GMAIL_MCP_FLAVOR=cursor or export GMAIL_MCP_TOOL_* overrides.
_FLAVOR = os.environ.get("GMAIL_MCP_FLAVOR", "shinzo").lower()
if _FLAVOR == "shinzo":
    GMAIL_MCP_TOOL_SEARCH = os.environ.get("GMAIL_MCP_TOOL_SEARCH", "list_messages")
    GMAIL_MCP_TOOL_READ = os.environ.get("GMAIL_MCP_TOOL_READ", "get_message")
    GMAIL_MCP_TOOL_SEND = os.environ.get("GMAIL_MCP_TOOL_SEND", "send_message")
else:
    GMAIL_MCP_TOOL_SEARCH = os.environ.get("GMAIL_MCP_TOOL_SEARCH", "search_emails")
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


def is_active_day() -> bool:
    return datetime.now(_ny_tz()).weekday() in ACTIVE_DAYS


class _MCPStdioSession:
    """Minimal MCP client over stdio (JSON-RPC + Content-Length framing)."""

    def __init__(self, argv: List[str]) -> None:
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._stderr_buf: List[str] = []
        self._err_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._err_thread.start()
        self._lock = threading.Lock()
        self._next_id = 1
        self._initialize()

    def _drain_stderr(self) -> None:
        if self._proc.stderr is None:
            return
        try:
            for line in iter(self._proc.stderr.readline, b""):
                if not line:
                    break
                self._stderr_buf.append(line.decode("utf-8", errors="replace"))
        except Exception:
            pass

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def _read_one_message(self) -> Dict[str, Any]:
        assert self._proc.stdout is not None
        headers: List[str] = []
        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP stdout closed unexpectedly")
            if line in (b"\r\n", b"\n"):
                break
            headers.append(line.decode("utf-8", errors="replace"))
        cl = None
        for h in headers:
            if h.lower().startswith("content-length:"):
                cl = int(h.split(":", 1)[1].strip())
                break
        if cl is None:
            raise RuntimeError(f"Missing Content-Length in MCP headers: {headers!r}")
        body = b""
        while len(body) < cl:
            chunk = self._proc.stdout.read(cl - len(body))
            if not chunk:
                raise RuntimeError("Incomplete MCP body")
            body += chunk
        return json.loads(body.decode("utf-8"))

    def _write_message(self, payload: Dict[str, Any]) -> None:
        assert self._proc.stdin is not None
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii")
        self._proc.stdin.write(header + raw)
        self._proc.stdin.flush()

    def _initialize(self) -> None:
        req_id = self._next_id
        self._next_id += 1
        self._write_message(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "ai-news-digest", "version": "1.0.0"},
                },
            }
        )
        while True:
            msg = self._read_one_message()
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(f"MCP initialize error: {msg['error']}")
                break
        self._write_message(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
        )

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            self._write_message(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}},
                }
            )
            while True:
                msg = self._read_one_message()
                if msg.get("method") == "notifications/message":
                    continue
                if msg.get("id") == req_id:
                    if "error" in msg:
                        raise RuntimeError(f"MCP tools/call error: {msg['error']}")
                    return msg.get("result")


_mcp_session: Optional[_MCPStdioSession] = None


def _get_mcp_session() -> _MCPStdioSession:
    global _mcp_session
    if _mcp_session is None:
        argv = [GMAIL_MCP_COMMAND] + GMAIL_MCP_ARGS
        _mcp_session = _MCPStdioSession(argv)
    return _mcp_session


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
    flavor = os.environ.get("GMAIL_MCP_FLAVOR", "").lower()
    if flavor == "shinzo":
        return {"q": query, "maxResults": max_results}
    return {"query": query, "maxResults": max_results}


def _mcp_read_args(message_id: str) -> Dict[str, Any]:
    flavor = os.environ.get("GMAIL_MCP_FLAVOR", "").lower()
    if flavor == "shinzo":
        return {"messageId": message_id}
    return {"messageId": message_id}


def _mcp_send_args(to_list: List[str], subject: str, body: str) -> Dict[str, Any]:
    flavor = os.environ.get("GMAIL_MCP_FLAVOR", "").lower()
    if flavor == "shinzo":
        return {
            "to": ",".join(to_list),
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
    NY window. Uses Gmail MCP (stdio subprocess). Each MCP call is wrapped in try/except by caller pattern in main;
    this function wraps search + per-message read.
    """
    ny = _ny_tz()
    now_ny = datetime.now(ny)
    day = now_ny.date()

    session = _get_mcp_session()
    query = "in:inbox newer_than:5d"
    try:
        raw_result = session.call_tool(
            GMAIL_MCP_TOOL_SEARCH, _mcp_search_args(query, max_results=500)
        )
    except Exception as e:
        raise RuntimeError(f"Gmail MCP search failed: {e}") from e

    summaries = _search_result_to_entries(raw_result)

    raw_emails: List[Dict[str, Any]] = []
    win_lo = dt_time(START_HOUR, START_MINUTE)
    win_hi = dt_time(END_HOUR, END_MINUTE)

    for summ in summaries:
        mid = summ.get("id", "").strip()
        if not mid:
            continue
        date_hdr = summ.get("date", "")
        if date_hdr.isdigit() and len(date_hdr) >= 10:
            try:
                ms = int(date_hdr)
                recv = datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc).astimezone(
                    ny
                )
            except (OSError, ValueError):
                try:
                    recv = _parse_received_at(date_hdr)
                except Exception:
                    continue
        else:
            try:
                recv = _parse_received_at(date_hdr)
            except Exception:
                continue
        if not _in_window(recv, day, win_lo, win_hi):
            continue
        try:
            r = session.call_tool(GMAIL_MCP_TOOL_READ, _mcp_read_args(mid))
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
            recv2 = recv
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


def _score_story(text: str) -> int:
    t = text.lower()
    if re.search(r"\b(sponsor|advertisement|partner|advertise with us)\b", t):
        return 1
    majors = [
        "openai",
        "anthropic",
        "google deepmind",
        "deepmind",
        "google",
        "meta",
        "apple",
        "microsoft",
        "amazon",
        "nvidia",
        "tesla",
    ]
    for m in majors:
        if m in t and re.search(r"\b(launch|announc|released|release|unveil|introduc|ipo|acqui|merg)\b", t):
            return 10
    if re.search(r"\b(acquisition|merger|ipo)\b", t) or re.search(
        r"\$\s*[1-9][0-9]{2,}\s*million|\$\s*[0-9.]+\s*billion", t
    ):
        if re.search(r"\$\s*[0-9.]+\s*billion", t) or re.search(
            r"\$\s*[1-9][0-9]{2,}\s*million", t
        ):
            return 9
    if re.search(
        r"\b(llama|mistral|gemma|falcon|open[- ]source model|weights available|apache 2)\b", t
    ) or re.search(r"\barxiv\b|\bresearch paper\b|\bnew paper\b", t):
        return 8
    if re.search(
        r"\b(regulation|regulator|policy|government|court|lawsuit|legal ruling|congress|eu ai)\b", t
    ):
        return 7
    if re.search(r"\b(funding|series [a-e]|raised|seed round)\b", t):
        return 6
    if re.search(r"\b(says|argues|analysis|according to)\b", t) and re.search(
        r"\b(professor|researcher|ceo|cto|analyst)\b", t
    ):
        return 5
    if re.search(r"\b(how to|tutorial|tips|listicle|recap|roundup)\b", t):
        return 3
    return 4


def _split_segments(clean_text: str) -> List[str]:
    if not clean_text.strip():
        return []
    # Newsletter item titles often end with "(N MINUTE READ)" — strong boundary for TLDR-style blocks
    clean_text = re.sub(
        r"\n(?=[A-Z0-9][A-Z0-9\s\-\,\.\'\"\(\)]{12,}\(\d+\s+MINUTE\s+READ\))",
        "\n\n\n",
        clean_text,
        flags=re.MULTILINE,
    )
    parts = re.split(r"\n{3,}", clean_text)
    segments: List[str] = []
    for p in parts:
        sub = re.split(r"(?=^(?:[🔥📌👀💡⚡🚀📊🧠🎁]\s))", p, flags=re.MULTILINE)
        for s in sub:
            s = s.strip()
            if not s:
                continue
            lines = s.splitlines()
            buf: List[str] = []
            for line in lines:
                if re.match(r"^[A-Z0-9][A-Z0-9\s\&\-\,\.\'\"]{6,}$", line.strip()) and len(
                    line.strip()
                ) < 120:
                    if buf:
                        segments.append("\n".join(buf).strip())
                        buf = []
                    buf.append(line.strip())
                    continue
                if re.match(r"^\d+\.\s+", line.strip()):
                    if buf:
                        segments.append("\n".join(buf).strip())
                        buf = []
                buf.append(line)
            if buf:
                segments.append("\n".join(buf).strip())
    extra: List[str] = []
    for seg in segments:
        for piece in re.split(r"\n(?=\d+\.\s+)", seg):
            piece = piece.strip()
            if piece:
                extra.append(piece)
    segments = extra
    out: List[str] = []
    for seg in segments:
        for piece in re.split(r"\n(?=[•\-]\s+)", seg):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return [x for x in out if len(x) >= 20]


def _headline_summary(segment: str) -> Tuple[str, str]:
    lines = [ln.strip() for ln in segment.splitlines() if ln.strip()]
    if not lines:
        return "", ""
    headline = lines[0][:120]
    rest = " ".join(lines[1:])
    rest = re.sub(r"\s+", " ", rest).strip()
    summary = rest[:280]
    if summary and summary[-1] not in ".!?":
        summary += "."
    return headline, summary


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


def parse_email(raw_email: Dict[str, Any]) -> Dict[str, Any]:
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

    segments = _split_segments(body)
    stories: List[Dict[str, Any]] = []
    for seg in segments:
        headline, summary = _headline_summary(seg)
        if not headline:
            continue
        blob = headline + " " + summary
        score = _score_story(blob)
        if score <= 1:
            continue
        stories.append(
            {
                "headline": headline,
                "summary": summary,
                "importance_score": score,
                "company_caps": _company_caps(headline, seg),
                "dedupe_key": re.sub(r"[^a-z0-9]+", " ", headline.lower()).strip(),
            }
        )

    return {
        "id": eid,
        "sender_name": sender_name,
        "sender_email": sender_email,
        "subject": subject,
        "received_at": recv,
        "stories": stories,
    }


def rank_and_cap(
    parsed_emails: List[Dict[str, Any]]
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Returns (top_story dict or None, list of source buckets with capped stories)."""
    buckets: List[Dict[str, Any]] = []
    all_scored: List[Tuple[int, str, Dict[str, Any], Dict[str, Any]]] = []
    for pe in parsed_emails:
        stories = sorted(
            pe.get("stories") or [], key=lambda s: s["importance_score"], reverse=True
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
            all_scored.append((st2["importance_score"], uid, st2, b))
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
            flat.append((s["importance_score"], s["_story_uid"], s, b))
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
    items.sort(key=lambda x: (-x[1]["importance_score"], x[0].get("sender_name", "")))

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
        if s["importance_score"] > ws["importance_score"]:
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
            sum(s["importance_score"] for s in x.get("stories", []))
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
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("📊 DIGEST STATS")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append(f"📬 Emails processed: {stats.get('emails_processed', 0)}")
    lines.append(f"🗞️ Sources today: {', '.join(srcs) if srcs else '(none)'}")
    lines.append("⏱️ Time window scanned: 4:30am – 10:00am EST")
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
    session = _get_mcp_session()
    results = {"ok": [], "fail": []}
    for recipient in DIGEST_RECIPIENTS:
        try:
            session.call_tool(
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


def send_digest(digest_text: str) -> Dict[str, Any]:
    ny = _now_ny()
    subject = (
        f"🤖 AI Morning Digest — {ny.strftime('%A, %B ')}"
        f"{int(ny.strftime('%d'))}, {ny.strftime('%Y')}"
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


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Morning Digest agent")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and build digest, print to stdout, do not send email or update logs",
    )
    args = parser.parse_args()
    dry = args.dry_run

    # STEP 1 — DAY CHECK
    if os.environ.get("DIGEST_FORCE_RUN", "").strip() not in ("1", "true", "yes"):
        if not is_active_day():
            day_name = datetime.now(_ny_tz()).strftime("%A")
            print(f"Today is {day_name} — not a weekday. Skipping run.")
            return 0

    stats: Dict[str, Any] = {
        "emails_found": 0,
        "emails_processed": 0,
        "bullets_generated": 0,
        "recipients": len(DIGEST_RECIPIENTS),
        "recipients_attempted": len(DIGEST_RECIPIENTS),
        "parse_failures": 0,
        "duplicate_merges": 0,
        "source_names": [],
        "status": "SUCCESS",
    }

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
            try:
                alert_res = _send_email_mcp(subj, body)
                if not dry:
                    stats["recipients_ok"] = len(alert_res.get("ok", []))
            except Exception as se:
                _error_log_write("main", f"Could not send alert email: {se}", "")
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
        # STEP 4 — PARSE EACH EMAIL
        for raw in unproc:
            try:
                parsed = parse_email(raw)
                parsed_list.append(parsed)
                processed_ids.append(parsed["id"])
            except Exception as e:
                stats["parse_failures"] += 1
                _error_log_write(
                    "parse_email",
                    str(e),
                    extra=f"subject={raw.get('subject','')} from={raw.get('from_header','')}",
                )

        # STEP 5 — RANK AND CAP
        top_story, ranked = rank_and_cap(parsed_list)
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
                    f"[dry-run] TOP STORY: score={ts.get('importance_score')} "
                    f"headline={ts.get('headline','')[:100]!r}"
                )
            for b in ranked:
                print(
                    f"[dry-run] Source {b.get('sender_name')!r}: "
                    f"{len(b.get('stories',[]))} stories after rank/dedupe"
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
        global _mcp_session
        if _mcp_session is not None:
            _mcp_session.close()
            _mcp_session = None


if __name__ == "__main__":
    raise SystemExit(main())
