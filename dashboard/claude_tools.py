import os
import base64
import logging
import requests
import anthropic

CLAUDE_MODEL = "claude-sonnet-4-6"
GITHUB_REPO = os.environ.get("GITHUB_REPO", "CaidenChancelor/Corey-s-Bjj-Assistant")
GITHUB_PAT = os.environ.get("GITHUB_PAT", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

GITHUB_API = "https://api.github.com"
GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_PAT}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

EDITOR_SYSTEM_PROMPT = f"""You are helping Corey edit his BJJ WhatsApp bot at {GITHUB_REPO}. The main file is bot.py.

Use the tools to read, modify, and commit files. Every commit triggers Railway auto-deploy in ~1 minute.

Rules:
- Always read_file before write_file so you have the current contents and don't accidentally drop existing code.
- When writing a file, you must include the FULL file content — there's no patch/diff tool.
- Be careful with destructive changes. If Corey asks for a big rewrite, summarize what you'll change and confirm before committing.
- Use clear commit messages that describe the change ("Fix water tracking bug", not "Update bot.py").
- Stack: Python 3, Flask, APScheduler, Twilio, Anthropic SDK, SQLite. Don't introduce new frameworks unless asked.
- When you're done, give Corey a short summary of what you changed and what to test.

You are running on {CLAUDE_MODEL}."""

TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file from the bjj-bot GitHub repo. Returns the file contents as a string.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to repo root (e.g. 'bot.py')"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write contents to a file in the bjj-bot repo and commit the change. This triggers a Railway redeploy. ALWAYS read_file first to get current contents — you must include the full file content, not a diff.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to repo root"},
                "content": {"type": "string", "description": "Full file contents"},
                "commit_message": {"type": "string", "description": "Concise commit message describing the change"},
            },
            "required": ["path", "content", "commit_message"],
        },
    },
    {
        "name": "list_files",
        "description": "List files in a directory of the bjj-bot repo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (empty string for repo root)", "default": ""},
            },
        },
    },
]

def gh_get_file(path):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
    r = requests.get(url, headers=GH_HEADERS, timeout=15)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    data = r.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content, data["sha"]

def gh_put_file(path, content, message, sha=None):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(url, headers=GH_HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()

def gh_list(path):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}".rstrip("/")
    r = requests.get(url, headers=GH_HEADERS, timeout=15)
    r.raise_for_status()
    items = r.json()
    return [{"name": i["name"], "type": i["type"], "path": i["path"]} for i in items]

def execute_tool(name, args):
    try:
        if name == "read_file":
            content, _ = gh_get_file(args["path"])
            if content is None:
                return f"File not found: {args['path']}"
            return content
        if name == "write_file":
            _, sha = gh_get_file(args["path"])
            gh_put_file(args["path"], args["content"], args["commit_message"], sha=sha)
            return f"Committed to {args['path']}: {args['commit_message']}"
        if name == "list_files":
            items = gh_list(args.get("path", ""))
            return "\n".join(f"{i['type']}: {i['path']}" for i in items)
        return f"Unknown tool: {name}"
    except Exception as e:
        logging.exception(f"Tool {name} failed")
        return f"Error: {e}"

def handle_chat_message(user_message, history):
    """Run an agent loop: send msg → execute tools → repeat until Claude returns final text.

    NOTE: `history` must NOT include the current user_message — this function appends it
    internally. The Flask caller is responsible for storing the returned reply back into
    the session after this returns.
    """
    if not claude:
        return "Anthropic API key not configured."
    if not GITHUB_PAT:
        return "GITHUB_PAT not configured — can't read/write the repo."

    try:
        return _run_agent_loop(user_message, history)
    except anthropic.APIConnectionError as e:
        logging.exception("Claude API connection error")
        return f"Connection error reaching Claude: {e}"
    except anthropic.AuthenticationError:
        logging.exception("Claude API auth error")
        return "Anthropic API key is invalid or expired."
    except anthropic.RateLimitError:
        logging.exception("Claude API rate limit")
        return "Rate limited by Anthropic. Try again in a moment."
    except anthropic.APIStatusError as e:
        logging.exception("Claude API status error")
        return f"Claude API error ({e.status_code}): {e.message}"
    except Exception as e:
        logging.exception("Unexpected error in handle_chat_message")
        return f"Unexpected error: {e}"


def _run_agent_loop(user_message, history):
    """Inner agent loop — called by handle_chat_message inside a try/except."""
    # Cap incoming history at the last 20 messages to avoid ballooning API costs.
    capped_history = list(history)[-20:]

    messages = capped_history + [{"role": "user", "content": user_message}]

    for _ in range(10):  # safety cap on tool-use loops
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=EDITOR_SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            # Final text response — concat any text blocks
            text = "".join(b.text for b in response.content if b.type == "text")
            return text or "(no response)"

        # Append assistant turn (with tool_use blocks) and run the tools
        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
        messages.append({"role": "user", "content": tool_results})

    return "Hit the tool-use loop cap (10 iterations). Try a more specific request."
