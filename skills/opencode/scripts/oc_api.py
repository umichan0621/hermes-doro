#!/usr/bin/env python3
"""OpenCode 2.0 host API client (proven workflow, see skill opencode).

V2-ONLY. The V1 server API is dead and fails SILENTLY against a V2 host
(unknown/V1 paths fall through to the web SPA and return 200 HTML; V1
request bodies are ignored — a V1-style session-create body lands the
session in the server process cwd). Every entry point here gates on
GET /api/info reporting a 2.x version and refuses anything older.

Loads the project map from the projects YAML and Basic Auth credentials
(username `opencode`) from the env file. Never print or log the password.

Container defaults; override for host-side debugging:
  OPENCODE_API_HOST        (default host.docker.internal)
  OPENCODE_PROJECTS_YAML   (default /opt/data/opencode_projects.yaml)
  OPENCODE_ENV_FILE        (default /opt/data/.env)

CLI:
  python3 oc_api.py server <project>                      # version gate + info
  python3 oc_api.py sessions <project>                    # root sessions, newest first
  python3 oc_api.py info <project> <session_id>           # title/outcome/tokens
  python3 oc_api.py label <project> <session_id>          # '<created time> <topic>'
  python3 oc_api.py new <project>                         # fresh session (asserts dir)
  python3 oc_api.py prompt <project> <session_id> <text>  # send prompt, print turn anchor
  python3 oc_api.py answer <project> <session_id>         # final assistant prose + tools
  python3 oc_api.py pending <project> <session_id>        # permissions + forms
  python3 oc_api.py shell <project> '<command>'           # location shell (no session)
  python3 oc_api.py file <project> <path>                 # read host file (raw bytes)
  python3 oc_api.py ls <project> [path]                   # list host directory
Or import: from oc_api import (server, root_sessions, session_report, session_label,
    new_session, prompt, messages, final_answer, is_running, turn_status, wait, shell,
    interrupt, permissions_pending, permission_reply, forms_pending, form_reply,
    host_skills, read_host_file, list_host_dir, models)
"""
import base64
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "/api"
PROJECTS_YAML = os.environ.get("OPENCODE_PROJECTS_YAML", "/opt/data/opencode_projects.yaml")
ENV_FILE = os.environ.get("OPENCODE_ENV_FILE", "/opt/data/.env")
HOST = os.environ.get("OPENCODE_API_HOST", "host.docker.internal")

_TERMINAL_FINISH = ("stop", "length", "content-filter", "error", "unknown")


class ApiError(Exception):
    def __init__(self, status, message, tag=None):
        self.status = status
        self.tag = tag
        self.message = message
        super().__init__("HTTP %s%s: %s" % (status, " %s" % tag if tag else "", message))


def _password():
    for line in open(ENV_FILE):
        if line.startswith("OPENCODE_SERVER_PASSWORD="):
            return line.strip().split("=", 1)[1]
    raise RuntimeError("OPENCODE_SERVER_PASSWORD not set in %s" % ENV_FILE)


def project_info(name):
    text = open(PROJECTS_YAML).read()
    m = re.search(
        r"^\s+%s:\s*\n\s+dir:\s*(\S+)\s*\n\s+port:\s*(\d+)" % re.escape(name),
        text,
        re.M,
    )
    if not m:
        raise RuntimeError("project %r not in %s" % (name, PROJECTS_YAML))
    return {"name": name, "dir": m.group(1), "port": int(m.group(2))}


def _request(name, path, method="GET", body=None, timeout=60):
    """One authenticated request against project <name>'s OpenCode endpoint.

    `path` is relative to the /api root (query string allowed). Returns
    (status, content_type, bytes). Raises ApiError on HTTP >= 400 and on
    connection failures.
    """
    p = project_info(name)
    auth = "Basic " + base64.b64encode(("opencode:%s" % _password()).encode()).decode()
    r = urllib.request.Request(
        "http://%s:%d%s%s" % (HOST, p["port"], API, path),
        method=method,
        headers={"Authorization": auth, "Content-Type": "application/json"},
    )
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(r, data=data, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as e:
        payload = e.read()
        tag = None
        try:
            j = json.loads(payload)
            tag, message = j.get("_tag"), j.get("message")
        except Exception:
            message = payload[:200].decode("utf-8", "replace") if payload else str(e)
        raise ApiError(e.code, message or str(e), tag)
    except urllib.error.URLError as e:
        raise ApiError(0, "cannot reach %s:%d: %s" % (HOST, p["port"], e.reason))


def _json(name, path, method="GET", body=None, timeout=60):
    status, ctype, payload = _request(name, path, method, body, timeout)
    if not payload:
        return None
    if "json" not in ctype.lower():
        raise ApiError(
            status,
            "endpoint returned %s, not JSON — not a V2 API path (unknown and "
            "legacy V1 paths fall through to the web SPA); path was %s"
            % (ctype or "no content-type", path.split("?")[0]),
        )
    return json.loads(payload)


def unwrap(r):
    """Strip the V2 response envelope {location?, data, cursor?} -> data.

    Responses without the envelope (e.g. GET /api/info) pass through.
    """
    if isinstance(r, dict) and "data" in r and set(r.keys()) <= {"data", "location", "cursor"}:
        return r["data"]
    return r


def req(name, path, method="GET", body=None, timeout=60):
    """Gated JSON request: asserts a V2 host, then issues the call."""
    require_v2(name)
    return _json(name, path, method, body, timeout)


_v2_cache = {}


def require_v2(name):
    """Refuse to talk to anything but OpenCode >= 2.0 (cached per project)."""
    if name in _v2_cache:
        return _v2_cache[name]
    port = project_info(name)["port"]
    try:
        info = _json(name, "/info")
    except ApiError as e:
        if e.status == 401:
            raise
        raise ApiError(
            e.status,
            "cannot read /api/info on port %d — the host is not serving the "
            "OpenCode 2.0 API (V1 has no /api/* routes). Upgrade "
            "~/.opencode/bin/opencode to >= 2.0 and restart the opencode-serve@ "
            "unit. Detail: %s" % (port, e.message),
        )
    version = (info or {}).get("version") or ""
    if version.split(".")[0] != "2":
        raise ApiError(
            0,
            "host OpenCode is %r; this bridge requires >= 2.0 (the V1 server "
            "API is not supported)" % (version or "unknown"),
        )
    _v2_cache[name] = info
    return info


def server(name):
    """GET /api/info (gated): {version, pid, urls, paths, capabilities}."""
    return require_v2(name)


def _loc(name):
    d = project_info(name)["dir"]
    return "location%5Bdirectory%5D=" + urllib.parse.quote(d, safe="")


def root_sessions(name, limit=50, search=None):
    """Root sessions for a project, newest-updated first (time.updated is
    epoch ms). `parentID=null` filters child sessions server-side (children
    duplicate subagent work and inflate the list); the client re-sorts as a
    safety net. `search` filters by title.
    """
    directory = project_info(name)["dir"]
    q = "directory=%s&parentID=null&order=desc&limit=%d" % (
        urllib.parse.quote(directory), limit)
    if search:
        q += "&search=%s" % urllib.parse.quote(search)
    items = unwrap(req(name, "/session?%s" % q)) or []
    roots = [s for s in items if not s.get("parentID")]
    roots.sort(key=lambda s: (s.get("time") or {}).get("updated", 0), reverse=True)
    return roots


def session_report(name, session_id):
    """Title/outcome/tokens for one session (rotation announcements, reports)."""
    s = unwrap(req(name, "/session/%s" % session_id)) or {}
    return {
        "id": s.get("id", session_id),
        "title": s.get("title") or "",
        "outcome": s.get("outcome"),
        "time": s.get("time") or {},
        "tokens": s.get("tokens") or {},
    }


def new_session(name):
    """Create a fresh session via POST /api/session with body
    {"location": {"directory": ...}}.

    A V1-style {"directory": ...} body is silently IGNORED by V2 and the
    session lands in the server process cwd — the returned location is
    ASSERTED to match the project dir before the id is handed out.
    """
    directory = project_info(name)["dir"]
    s = unwrap(req(name, "/session", method="POST",
                   body={"location": {"directory": directory}})) or {}
    got = (s.get("location") or {}).get("directory")
    if got != directory:
        raise ApiError(0, "session created in %r, expected %r — refusing to use it"
                       % (got, directory))
    return s


_DEFAULT_TITLE_RE = re.compile(r"^New session\b")


def session_label(name, session_id, limit=10):
    """Rotation label '<created local time> <topic>', e.g.
    '2026-10-17 00:59:28 read project info'.

    Topic: the session title when it is a real one (V2 fresh sessions have
    NO title until the first turn; V1 used to write 'New session - <ts>');
    otherwise a short excerpt of the earliest user message; otherwise
    'empty session (never used)'. The agent may further condense the excerpt
    (and translate) when announcing — replies follow the user's language.
    """
    s = unwrap(req(name, "/session/%s" % session_id)) or {}
    created = (s.get("time") or {}).get("created")
    ts = (
        datetime.datetime.fromtimestamp(created / 1000).strftime("%Y-%m-%d %H:%M:%S")
        if created
        else "?"
    )
    title = (s.get("title") or "").strip()
    if title and not _DEFAULT_TITLE_RE.match(title):
        topic = title
    else:
        topic = ""
        for m in messages(name, session_id, limit):
            if m["type"] == "user" and m["text"]:
                topic = " ".join(m["text"].split())[:24]
                break
        topic = topic or "empty session (never used)"
    return "%s %s" % (ts, topic)


def prompt(name, session_id, text, delivery=None):
    """Send a prompt via POST /api/session/<id>/prompt with body {"text": ...}.

    Returns the inbox item; its "id" is the user message id of this turn —
    the ANCHOR to pass to turn_status. Accepted does NOT mean finished.
    Optional delivery: "steer" (default) or "queue" — keep the default
    unless you specifically need queued delivery.
    """
    body = {"text": text}
    if delivery:
        body["delivery"] = delivery
    return unwrap(req(name, "/session/%s/prompt" % session_id, method="POST", body=body))


def messages(name, session_id, limit=20):
    """Flatten GET /api/session/<id>/message to a list of dicts, oldest first:
    {id, type, role, finish, completed, text, tool_outputs, outcome}.

    V2 shape: {data: [...], cursor}; each item is FLAT with a `type`
    discriminator (user/assistant/shell/system/synthetic/idle/...).
    Fetched as order=desc (newest page) then REVERSED — order=asc with a
    limit returns the OLDEST N messages and silently misses the conclusion.
    Assistant prose lives in content[] items of type "text"; completed tool
    output in content[] items of type "tool" under state.content[].text
    (V1's state.output key no longer exists).
    """
    items = unwrap(req(name, "/session/%s/message?order=desc&limit=%d"
                       % (session_id, limit))) or []
    out = []
    for m in reversed(items):
        mtype = m.get("type")
        row = {
            "id": m.get("id"),
            "type": mtype,
            "role": "user" if mtype == "user" else ("assistant" if mtype == "assistant" else None),
            "finish": m.get("finish"),
            "completed": (m.get("time") or {}).get("completed"),
            "text": "",
            "tool_outputs": [],
            "outcome": m.get("outcome"),
        }
        if mtype == "assistant":
            texts, tools = [], []
            for c in m.get("content") or []:
                if c.get("type") == "text" and c.get("text"):
                    texts.append(c["text"])
                elif c.get("type") == "tool":
                    st = c.get("state") or {}
                    if st.get("status") in ("completed", "error"):
                        parts = [tc.get("text") or "" for tc in (st.get("content") or [])
                                 if tc.get("type") == "text"]
                        if not parts and st.get("status") == "error":
                            err = st.get("error") or {}
                            parts = ["tool error: %s" % (err.get("message") or "")]
                        tools.append("\n".join(x for x in parts if x))
            row["text"] = "\n".join(texts)
            row["tool_outputs"] = tools
        elif mtype == "shell":
            row["text"] = "$ %s" % (m.get("command") or "")
            output = (m.get("output") or {}).get("output")
            if output:
                row["tool_outputs"] = [output]
        else:  # user / system / synthetic / idle / ...
            row["text"] = m.get("text") or ""
        out.append(row)
    return out


def final_answer(name, session_id, limit=30):
    """The conclusion of the newest turn: newest assistant message with a
    terminal finish (prefer "stop"; fall back to length/error/...). Mid-run
    assistant messages have finish "tool-calls" and empty text.
    """
    msgs = messages(name, session_id, limit)
    stops = [m for m in msgs if m["role"] == "assistant" and m["finish"] == "stop"]
    if stops:
        return stops[-1]
    others = [m for m in msgs
              if m["role"] == "assistant" and m["finish"] in _TERMINAL_FINISH]
    return others[-1] if others else None


def is_running(name, session_id):
    """True while the session has an active turn (GET /api/session/active)."""
    active = unwrap(req(name, "/session/active"))
    if isinstance(active, dict):
        return session_id in active
    if isinstance(active, list):
        return any(x == session_id or (x or {}).get("id") == session_id for x in active)
    return False


def turn_status(name, session_id, anchor=None):
    """Layered completion signal for the newest (or anchored) turn.

    Returns {running, outcome, done, conclusion}:
      running     session id appears in /api/session/active
      outcome     the idle message's outcome (succeeded/failed/interrupted)
                  for THIS turn — the session-level outcome field goes stale
                  between turns; never trust it alone
      conclusion  newest assistant message after the anchor with a terminal
                  finish (or None)
      done        not running AND (an idle message exists after the anchor,
                  or a conclusion with time.completed set)

    `anchor` is the user message id returned by prompt(). When the anchor
    is older than the fetched page it degrades to whole-history inspection.
    """
    active = is_running(name, session_id)
    msgs = messages(name, session_id, limit=50)
    if anchor:
        ids = [m["id"] for m in msgs]
        if anchor in ids:
            msgs = msgs[ids.index(anchor):]
    outcome = None
    done_marker = False
    for m in reversed(msgs):
        if m["type"] == "idle":
            done_marker = True
            if m.get("outcome"):
                outcome = m["outcome"]
            break
    conclusion = None
    for m in reversed(msgs):
        if m["role"] == "assistant" and m["finish"] in _TERMINAL_FINISH:
            conclusion = m
            break
    done = (not active) and (
        done_marker or (conclusion is not None and conclusion["completed"])
    )
    return {"running": active, "outcome": outcome, "done": done, "conclusion": conclusion}


def wait(name, session_id, timeout=60):
    """Long-poll POST /api/experimental/session/<id>/wait (experimental).

    True = turn finished (204); False = server reports busy (503), retry
    later; anything else raises.
    """
    try:
        req(name, "/experimental/session/%s/wait" % session_id, method="POST",
            timeout=timeout)
        return True
    except ApiError as e:
        if e.status == 503:
            return False
        raise


def shell(name, command, timeout=120):
    """One-shot synchronous command via the location shell:
    POST /api/shell?location[directory]=<dir> -> poll GET /api/shell/<id>
    -> GET /api/shell/<id>/output -> DELETE.

    Session-independent: no session id, no busy-409 against a running
    prompt, and the remote conversation context stays clean. The command
    MUST be a single line (';' joined). Keep these read-only.
    """
    info = unwrap(req(name, "/shell?%s" % _loc(name), method="POST",
                      body={"command": command}, timeout=timeout)) or {}
    shell_id = info.get("id")
    if not shell_id:
        raise ApiError(0, "shell create returned no id: %r" % (info,))
    deadline = time.time() + timeout
    try:
        while True:
            info = unwrap(req(name, "/shell/%s" % shell_id)) or {}
            if info.get("status") in ("exited", "timeout", "killed"):
                break
            if time.time() > deadline:
                raise ApiError(0, "shell command timed out after %ss: %s"
                               % (timeout, command[:120]))
            time.sleep(0.5)
        out = unwrap(req(name, "/shell/%s/output" % shell_id)) or {}
    finally:
        try:
            req(name, "/shell/%s" % shell_id, method="DELETE")
        except ApiError:
            pass
    return out.get("output", "")


def interrupt(name, session_id):
    """POST /api/session/<id>/interrupt — stop the running turn (V1 'abort').
    Returns {"interrupted": bool}; the session stays usable afterwards."""
    return unwrap(req(name, "/session/%s/interrupt" % session_id, method="POST"))


def permissions_pending(name, session_id):
    """Pending permission requests for the session."""
    return unwrap(req(name, "/session/%s/permission" % session_id)) or []


def permission_reply(name, session_id, request_id, decision="once"):
    """Reply to a permission request. V2 key is "decision" (V1 used
    "response"): "once" | "always" | "reject"."""
    req(name, "/session/%s/permission/%s/reply" % (session_id, request_id),
        method="POST", body={"decision": decision})


def forms_pending(name, session_id):
    """Pending forms (structured questions from the remote agent)."""
    return unwrap(req(name, "/session/%s/form" % session_id)) or []


def form_reply(name, session_id, form_id, answer):
    """Answer a form: `answer` maps field keys to values, e.g. {"choice": "b"}."""
    req(name, "/session/%s/form/%s/reply" % (session_id, form_id),
        method="POST", body={"answer": answer})


def host_skills(name):
    """Registered host skills (GET /api/skill): {id, name, description, path,
    content} — `content` is the full SKILL.md text; `path` is the host path
    (V1 called this field "location")."""
    return unwrap(req(name, "/skill?%s" % _loc(name))) or []


def read_host_file(name, path):
    """Host file bytes via GET /api/fs/read/<path>?location[directory]=<dir>.

    Location-SCOPED: `path` may be relative to the project directory or
    absolute, but it must resolve INSIDE the project directory — anything
    outside 404s (FileNotFoundError). For files outside the project dir use
    the location shell (`shell("cat <abs path>")`) or a session prompt.
    Returns RAW BYTES — the endpoint serves octet-stream, not JSON.
    """
    status, ctype, payload = _request(
        name, "/fs/read/%s?%s" % (urllib.parse.quote(path, safe="/"), _loc(name)))
    return payload


def list_host_dir(name, path="."):
    """List a host directory (GET /api/fs/list): [{path, type}], `path`
    relative to the project directory (pass "." for its root)."""
    q = "%s&path=%s" % (_loc(name), urllib.parse.quote(path, safe="/"))
    return unwrap(req(name, "/fs/list?%s" % q)) or []


def models(name):
    """Model names only, from GET /api/config.

    WARNING: the response embeds provider API keys under
    providers.*.settings — this function extracts names only; never print
    settings, keys, or any slice of them.
    """
    docs = unwrap(req(name, "/config?%s" % _loc(name))) or []
    out = {"default": None, "providers": {}}
    for doc in docs if isinstance(docs, list) else [docs]:
        info = (doc or {}).get("info") or {}
        if info.get("model") and not out["default"]:
            out["default"] = info.get("model")
        for pid, pv in (info.get("providers") or {}).items():
            names = sorted((pv or {}).get("models") or {})
            out["providers"].setdefault(pid, names)
    return out


def _main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    cmd, proj = argv[1], argv[2]
    if cmd == "server":
        print(json.dumps(server(proj), ensure_ascii=False))
    elif cmd == "sessions":
        for s in root_sessions(proj):
            upd = (s.get("time") or {}).get("updated", 0)
            ts = datetime.datetime.fromtimestamp(
                upd / 1000, datetime.timezone.utc).strftime("%m-%d %H:%M")
            print(s["id"], "|", ts, "|", (s.get("title") or "")[:90])
    elif cmd == "new":
        print(new_session(proj).get("id"))
    elif cmd == "info" and len(argv) > 3:
        r = session_report(proj, argv[3])
        print(r["id"], "|", r["title"] or "(untitled)", "| outcome:", r["outcome"])
        print(json.dumps(r["tokens"], ensure_ascii=False))
    elif cmd == "label" and len(argv) > 3:
        print(session_label(proj, argv[3]))
    elif cmd == "prompt" and len(argv) > 4:
        item = prompt(proj, argv[3], " ".join(argv[4:]))
        print(item.get("id"))
    elif cmd == "answer" and len(argv) > 3:
        m = final_answer(proj, argv[3])
        if not m:
            print("(no terminal assistant message yet)")
            return 1
        print(m["text"][:3000])
        for out in m["tool_outputs"]:
            print("OUT:", out[:1500])
    elif cmd == "pending" and len(argv) > 3:
        print(json.dumps({"permissions": permissions_pending(proj, argv[3]),
                          "forms": forms_pending(proj, argv[3])}, ensure_ascii=False))
    elif cmd == "shell" and len(argv) > 3:
        print(shell(proj, argv[3]))
    elif cmd == "file" and len(argv) > 3:
        sys.stdout.buffer.write(read_host_file(proj, argv[3]))
    elif cmd == "ls":
        target = argv[3] if len(argv) > 3 else "."
        for e in list_host_dir(proj, target):
            print("%-10s %s" % (e.get("type", "?"), e.get("path", "")))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
