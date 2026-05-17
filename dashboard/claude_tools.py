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
RAILWAY_API = "https://backboard.railway.app/graphql/v2"
RAILWAY_TOKEN = os.environ.get("RAILWAY_TOKEN", "")
RAILWAY_PROJECT_ID = os.environ.get("RAILWAY_PROJECT_ID", "")
# Optional: explicit service IDs (auto-discovered from project if not set)
RAILWAY_BOT_SERVICE_ID = os.environ.get("RAILWAY_BOT_SERVICE_ID", "")
RAILWAY_DASHBOARD_SERVICE_ID = os.environ.get("RAILWAY_DASHBOARD_SERVICE_ID", "")

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
    {
        "name": "get_railway_logs",
        "description": "Fetch recent deployment logs from Railway for the bot or dashboard service. Use this proactively when the user reports errors, unexpected behavior, or anything not working — don't wait for them to ask. Returns the last N log lines.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "enum": ["bot", "dashboard"],
                    "description": "Which Railway service to fetch logs from"
                },
                "lines": {
                    "type": "integer",
                    "description": "Number of recent log lines (default 50, max 200)",
                    "default": 50
                }
            },
            "required": ["service"]
        }
    },
    {
        "name": "get_deployment_status",
        "description": "Check the latest deployment status on Railway for the bot or dashboard. Use this after committing a file to confirm the deploy succeeded.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "enum": ["bot", "dashboard"],
                    "description": "Which service to check"
                }
            },
            "required": ["service"]
        }
    }
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

def railway_query(query, variables=None):
    """Call Railway GraphQL API. Returns parsed JSON or error dict."""
    if not RAILWAY_TOKEN:
        return {"error": "RAILWAY_TOKEN env var not set — add it to the dashboard Railway service"}
    try:
        r = requests.post(
            RAILWAY_API,
            headers={"Authorization": f"Bearer {RAILWAY_TOKEN}", "Content-Type": "application/json"},
            json={"query": query, "variables": variables or {}},
            timeout=15,
        )
        return r.json()
    except Exception as e:
        return {"error": f"Railway API error: {e}"}

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
        if name == "get_railway_logs":
            service = args.get("service", "bot")
            lines = min(int(args.get("lines", 50)), 200)
            if not RAILWAY_TOKEN:
                return "RAILWAY_TOKEN not set. Ask Corey to add it at railway.app → Account Settings → Tokens."
            if not RAILWAY_PROJECT_ID:
                return "RAILWAY_PROJECT_ID not set. Ask Corey to add it (visible in the Railway project URL)."
            # Step 1: Get service ID
            service_id = RAILWAY_BOT_SERVICE_ID if service == "bot" else RAILWAY_DASHBOARD_SERVICE_ID
            if not service_id:
                # Auto-discover from project
                result = railway_query("""
                    query($projectId: String!) {
                      project(id: $projectId) {
                        services { edges { node { id name } } }
                      }
                    }
                """, {"projectId": RAILWAY_PROJECT_ID})
                if "error" in result:
                    return f"Railway API error: {result['error']}"
                services = result.get("data", {}).get("project", {}).get("services", {}).get("edges", [])
                target_name = "bot" if service == "bot" else "dashboard"
                matched = [s["node"] for s in services if target_name.lower() in s["node"]["name"].lower()]
                if not matched:
                    names = [s["node"]["name"] for s in services]
                    return f"Couldn't find {service} service. Available: {names}. Set RAILWAY_BOT_SERVICE_ID or RAILWAY_DASHBOARD_SERVICE_ID env vars."
                service_id = matched[0]["id"]
            # Step 2: Get latest deployment ID
            result = railway_query("""
                query($serviceId: String!) {
                  deployments(input: { serviceId: $serviceId }) {
                    edges { node { id status createdAt } }
                  }
                }
            """, {"serviceId": service_id})
            if "error" in result:
                return f"Railway API error: {result['error']}"
            deployments = result.get("data", {}).get("deployments", {}).get("edges", [])
            if not deployments:
                return f"No deployments found for {service} service."
            latest = deployments[0]["node"]
            deployment_id = latest["id"]
            status = latest["status"]
            # Step 3: Get logs
            result = railway_query("""
                query($deploymentId: String!) {
                  deploymentLogs(deploymentId: $deploymentId) {
                    message timestamp severity
                  }
                }
            """, {"deploymentId": deployment_id})
            if "error" in result:
                return f"Railway API error: {result['error']}"
            logs = result.get("data", {}).get("deploymentLogs", [])
            if not logs:
                return f"No logs available for latest {service} deployment (status: {status})."
            recent = logs[-lines:]
            log_text = "\n".join(f"[{l.get('severity','INFO')}] {l.get('message','')}" for l in recent)
            return f"=== {service.upper()} logs (deployment {deployment_id[:8]}, status: {status}) ===\n{log_text}"
        if name == "get_deployment_status":
            service = args.get("service", "bot")
            if not RAILWAY_TOKEN:
                return "RAILWAY_TOKEN not set."
            if not RAILWAY_PROJECT_ID:
                return "RAILWAY_PROJECT_ID not set."
            service_id = RAILWAY_BOT_SERVICE_ID if service == "bot" else RAILWAY_DASHBOARD_SERVICE_ID
            if not service_id:
                result = railway_query("""
                    query($projectId: String!) {
                      project(id: $projectId) {
                        services { edges { node { id name } } }
                      }
                    }
                """, {"projectId": RAILWAY_PROJECT_ID})
                if "error" in result:
                    return f"Railway API error: {result['error']}"
                services = result.get("data", {}).get("project", {}).get("services", {}).get("edges", [])
                target_name = "bot" if service == "bot" else "dashboard"
                matched = [s["node"] for s in services if target_name.lower() in s["node"]["name"].lower()]
                if not matched:
                    return f"Couldn't find {service} service."
                service_id = matched[0]["id"]
            result = railway_query("""
                query($serviceId: String!) {
                  deployments(input: { serviceId: $serviceId }) {
                    edges { node { id status createdAt } }
                  }
                }
            """, {"serviceId": service_id})
            if "error" in result:
                return f"Railway API error: {result['error']}"
            deployments = result.get("data", {}).get("deployments", {}).get("edges", [])
            if not deployments:
                return f"No deployments found for {service}."
            d = deployments[0]["node"]
            return f"{service.upper()} latest deployment: {d['status']} (id: {d['id'][:8]}, created: {d['createdAt']})"
        return f"Unknown tool: {name}"
    except Exception as e:
        logging.exception(f"Tool {name} failed")
        return f"Error: {e}"

def compact_editor_history(history):
    """Compact a long conversation into summary + recent messages. Returns new history list or None on failure."""
    if not claude:
        return None
    try:
        history_text = "\n".join(
            f"{m['role'].upper()}: {m['content'][:400]}" for m in history
        )
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=500,
            system=(
                "You summarize coding assistant conversation history. "
                "Write 3-6 sentences covering: what files were changed, what bugs were fixed, "
                "what features were built, and key context for future messages. "
                "Be specific — mention file names, function names, what actually changed."
            ),
            messages=[{"role": "user", "content": f"Summarize this conversation:\n\n{history_text}"}],
        )
        summary = response.content[0].text.strip()
        return [
            {"role": "user", "content": f"[Conversation summary: {summary}]"},
            {"role": "assistant", "content": "Got it — I have the context from our previous session. What do you want to work on?"},
        ] + history[-6:]
    except Exception as e:
        logging.error(f"compact_editor_history error: {e}")
        return None


def handle_chat_message(user_message, history):
    """Run an agent loop: send msg → execute tools → repeat until Claude returns final text.

    NOTE: `history` must NOT include the current user_message — this function appends it
    internally. The Flask caller is responsible for storing the returned reply back into
    the session after this returns.
    """
    if not claude:
        return {"reply": "Anthropic API key not configured.", "tool_events": []}
    if not GITHUB_PAT:
        return {"reply": "GITHUB_PAT not configured — can't read/write the repo.", "tool_events": []}

    import time
    # Outer retry loop: handle transient 429s the SDK couldn't shake off.
    # Backoff sequence (seconds) after attempts 1, 2, 3.
    backoffs = [6, 14, 28]
    last_rate_err = None
    for attempt in range(len(backoffs) + 1):
        try:
            return _run_agent_loop(user_message, history)
        except anthropic.APIConnectionError as e:
            logging.exception("Claude API connection error")
            return {"reply": f"Connection error reaching Claude: {e}", "tool_events": []}
        except anthropic.AuthenticationError:
            logging.exception("Claude API auth error")
            return {"reply": "Anthropic API key is invalid or expired.", "tool_events": []}
        except anthropic.RateLimitError as e:
            last_rate_err = e
            if attempt < len(backoffs):
                logging.warning(f"Claude rate limit (attempt {attempt + 1}); sleeping {backoffs[attempt]}s")
                time.sleep(backoffs[attempt])
                continue
            logging.exception("Claude API rate limit — exhausted retries")
            return {"reply": "Rate limited by Anthropic after several retries. Try again in a minute.", "tool_events": []}
        except anthropic.APIStatusError as e:
            logging.exception("Claude API status error")
            return {"reply": f"Claude API error ({e.status_code}): {e.message}", "tool_events": []}
        except Exception as e:
            logging.exception("Unexpected error in handle_chat_message")
            return {"reply": f"Unexpected error: {e}", "tool_events": []}
    # Should not reach here, but in case:
    return {"reply": "Rate limited by Anthropic after several retries. Try again in a minute.", "tool_events": []}


def _run_agent_loop(user_message, history):
    """Inner agent loop — called by handle_chat_message inside a try/except."""
    # Cap incoming history at the last 20 messages to avoid ballooning API costs.
    capped_history = list(history)[-20:]

    messages = capped_history + [{"role": "user", "content": user_message}]

    tool_events = []

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
            return {"reply": text or "(no response)", "tool_events": tool_events}

        # Append assistant turn (with tool_use blocks) and run the tools
        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = execute_tool(block.name, block.input)
                tool_events.append({
                    "tool": block.name,
                    "input": block.input,
                    "result_preview": result[:300] if isinstance(result, str) else str(result)[:300]
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
        messages.append({"role": "user", "content": tool_results})

    return {"reply": "Hit the tool-use loop cap (10 iterations). Try a more specific request.", "tool_events": tool_events}
