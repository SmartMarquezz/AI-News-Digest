# AI & Tech News Digest

This project runs a small Python agent that, on weekdays, reads every newsletter in `learnmindsethub@gmail.com` that arrived between **4:30am and 10:00am** (America/New_York), pulls out the highest-signal items using rules-based text processing (no external AI APIs), and emails you **one** plain-text digest grouped by newsletter. A processed-ID log stops the same email from being summarized twice.

## Add a new recipient

In `digest.py`, inside `DIGEST_RECIPIENTS`, add one line with the email in quotes and a trailing comma:

```python
DIGEST_RECIPIENTS = [
    "learnmindsethub@gmail.com",
    "newemail@example.com",
]
```

## Remove a recipient

Delete that person’s line from `DIGEST_RECIPIENTS` in `digest.py`.

## Change the time window

In `digest.py`, edit these four variables only:

```python
START_HOUR = 4
START_MINUTE = 30
END_HOUR = 10
END_MINUTE = 0
```

## Change which days it runs

Edit `ACTIVE_DAYS` in `digest.py`. Days are `0 = Monday` through `6 = Sunday`. The default `[0, 1, 2, 3, 4]` is weekdays only.

## Run the script manually

```bash
cd "/path/to/AI News Digest"
python3 digest.py
```

Preview the digest without sending email or updating logs:

```bash
python3 digest.py --dry-run
```

On a weekend, the script exits unless you set (for testing only):

```bash
export DIGEST_FORCE_RUN=1
python3 digest.py --dry-run
```

## Gmail MCP (required for live runs)

`digest.py` does **not** use SMTP, IMAP, or the Gmail Python client libraries. It talks to your **Gmail MCP server** over the standard MCP stdio protocol (same idea as in Cursor): it starts a subprocess (`npx …` by default), then calls tools such as search/list, read, and send.

Defaults match the common **Shinzo Labs** server (`list_messages`, `get_message`, `send_message`). Override if your Cursor Gmail MCP uses different tool names:

| Environment variable | Purpose |
|----------------------|---------|
| `GMAIL_MCP_COMMAND` | Executable (default `npx`) |
| `GMAIL_MCP_ARGS` | Space-separated args (default `-y @shinzolabs/gmail-mcp`) |
| `GMAIL_MCP_FLAVOR` | `shinzo` (default) or `cursor` for `search_emails` / `read_email` / `send_email` |
| `GMAIL_MCP_TOOL_SEARCH`, `GMAIL_MCP_TOOL_READ`, `GMAIL_MCP_TOOL_SEND` | Explicit tool names |

Copy the `command` and `args` from your Cursor MCP settings if yours differ. The same OAuth / credential files the server expects must be available when the script runs (for example under `~/.gmail-mcp` for Shinzo’s server).

## Log files

| File | What it is |
|------|------------|
| `processed_emails.json` | Gmail message IDs already digested; prevents duplicates. |
| `digest_log.txt` | One line per run (counts and status). |
| `digest_errors.log` | Timestamped errors (fetch, parse, send). |

## If the digest stops arriving

1. Open `digest_errors.log` and read the latest entries.
2. Run `python3 digest.py` manually and watch the terminal.
3. Confirm Gmail MCP still works in Cursor (send/search a test message).
4. Confirm your scheduled environment sets the same `GMAIL_MCP_*` variables and can run `npx` (or your server command) non-interactively.

## Cursor Automation (weekdays at 10:00am)

1. In Cursor, open **Automations** (or your workspace automation UI).
2. Create a **schedule** trigger: **Monday–Friday** at **10:00 AM** in your local timezone (adjust if you want exactly America/New_York).
3. Add an action **Run command** (or **Shell**), working directory = this project folder:

   ```bash
   python3 digest.py
   ```

4. Ensure the automation environment includes the same Gmail MCP server configuration (`GMAIL_MCP_COMMAND`, `GMAIL_MCP_ARGS`, and any paths your OAuth tokens use).

If your automation cannot spawn the MCP subprocess, run the agent from Cursor with MCP enabled and use a wrapper that performs the Gmail steps there; the parsing and digest layout still live in `digest.py`.
