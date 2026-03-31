# AI & Tech News Digest

This project runs a small Python agent that, on weekdays, reads every newsletter in `learnmindsethub@gmail.com` that arrived between **4:30am and 10:00am** (America/New_York), extracts the highest-signal items with a **local Ollama** model (e.g. llama3.2) and **no paid APIs**, and emails you **one** plain-text digest grouped by newsletter—typically **after** that window, at **10:05am** local time (see `RUN_DIGEST_*` in `digest.py` and `crontab-schedule.txt`). A processed-ID log stops the same email from being summarized twice.

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

## Change the email window (which messages are included)

In `digest.py`, edit these four variables only:

```python
START_HOUR = 4
START_MINUTE = 30
END_HOUR = 10
END_MINUTE = 0
```

## When the script is allowed to run (digest schedule)

The script only runs the full pipeline on **Monday–Friday**, starting at **`RUN_DIGEST_HOUR` / `RUN_DIGEST_MINUTE`** (default **10:05** local time) through **`RUN_DIGEST_GRACE_MINUTES`** later (`is_within_allowed_window()` in `digest.py`). That is **separate** from the email window above: messages are still filtered to 4:30am–10:00am, but the job is meant to run once after that window ends so the digest includes all of them. Outside the digest run window it exits immediately (unless `DIGEST_FORCE_RUN=1` for testing). Schedule the run with **cron** using `crontab-schedule.txt` in this repo. A second job at **10:10** runs `python3 digest.py --post-check`: it emails you if **no line** was written to `digest_log.txt` for today (digest may not have run) or if the latest line shows **FAILED**, **PARTIAL**, or an unknown status. A **SUCCESS** log (including “no newsletters” runs) does not trigger that email.

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

## Cron schedule (recommended)

Paste the two lines from **`crontab-schedule.txt`** into your crontab (`crontab -e`). That runs the digest at **4:30am** on weekdays and **`pkill -f ollama`** at **10:00am** as a safety net. Ensure `PATH` in your environment includes `npx` / Homebrew if needed.
