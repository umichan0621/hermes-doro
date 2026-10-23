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
  OPENCODE_SESSION_STORE   (default /opt/data/workspace/opencode_sessions.json)
  OPENCODE_V2_CACHE        (default /opt/data/workspace/.oc_api_v2_cache.json)
  OPENCODE_DIFF_DIR        (default /opt/data/workspace/diffs)
  OPENCODE_SCRIPTS_DIR     (default /opt/data/scripts)

Exit codes (branch on these, never on stderr text):
  0  success
  1  error (API failure, failed/interrupted turn, bad state)
  2  usage error or policy refusal (e.g. --agent build without --allow-build)
  3  awaiting a human decision (pending form, or pending permission in
     --no-approve mode)
  4  still running (poll deadline reached; the anchor is persisted —
     re-check later with `wait` or `status`)

High-level commands (per-project session store, plan agent by default):
  task <project> [text]     one full turn: resume/create the stored session,
                            switch agent, send the prompt, poll to completion
                            (default 90 s), auto-approve permissions, print
                            the conclusion + token line. Flags:
                            --new (rotate first) --dir D (work-item session)
                            --stdin --file F --agent A --allow-build
                            --timeout S --no-approve --delivery steer|queue
  wait <project>            block until the stored turn finishes (experimental
                            long-poll, polling fallback). --timeout S (300)
                            --no-approve
  stop <project>            interrupt the stored session's running turn
  status [project]          all-project overview table, or one project's
                            turn status as JSON
  diff <project>            last turn's file changes (+/- per file); patches
                            over 100 changed lines are saved under
                            OPENCODE_DIFF_DIR. Flags: --all --from ID --to ID
  rotate <project>          label + title the old session, persist history,
                            create the replacement
  overview <project>        read-only "what is this project" shell recipes
  todos <project>           read-only "next tasks" shell recipes
  selftest <project>        endpoint shape smoke test (scratch session,
                            deleted afterwards)
  handoff <project>         print the deterministic one-line cron check
                            script for the stored turn; --write saves it
                            under OPENCODE_SCRIPTS_DIR and prints the bare
                            filename (for Hermes cronjob_manage)
  session-get <project> [key]
  session-set <project> <key> <value>   keys: session_id last_anchor agent
  approve <project> <request_id> [once|always|reject]
  form <project>            list pending forms
  form <project> <form_id> key=value ...   answer one

Low-level commands (explicit session ids):
  server <project>                      # version gate + info
  sessions <project>                    # root sessions, newest first
  info <project> <session_id>           # title/outcome/tokens
  label <project> <session_id>          # '<created time> <topic>'
  new <project>                         # fresh session (asserts dir)
  prompt <project> <session_id> <text>  # send prompt, print turn anchor
  answer <project> <session_id>         # final assistant prose + tools
  pending <project> <session_id>        # permissions + forms
  interrupt <project> <session_id>      # stop one session's running turn
  shell <project> '<command>'           # location shell (no session)
  file <project> <path>                 # read host file (raw bytes)
  ls <project> [path]                   # list host directory

Or import: from oc_api import (server, root_sessions, session_report, session_label,
    new_session, prompt, messages, final_answer, is_running, turn_status, wait, shell,
    interrupt, permissions_pending, permission_reply, forms_pending, form_reply,
    host_skills, read_host_file, list_host_dir, models, agents, switch_agent,
    session_diff, patch_session, delete_session, rotate_session, store_entry,
    store_update)
"""
import base64
import datetime
import fcntl
import json
import os
import posixpath
import re
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "/api"
PROJECTS_YAML = os.environ.get("OPENCODE_PROJECTS_YAML", "/opt/data/opencode_projects.yaml")
ENV_FILE = os.environ.get("OPENCODE_ENV_FILE", "/opt/data/.env")
HOST = os.environ.get("OPENCODE_API_HOST", "host.docker.internal")
STORE = os.environ.get("OPENCODE_SESSION_STORE", "/opt/data/workspace/opencode_sessions.json")
V2_CACHE_FILE = os.environ.get("OPENCODE_V2_CACHE", "/opt/data/workspace/.oc_api_v2_cache.json")
DIFF_DIR = os.environ.get("OPENCODE_DIFF_DIR", "/opt/data/workspace/diffs")
SCRIPTS_DIR = os.environ.get("OPENCODE_SCRIPTS_DIR", "/opt/data/scripts")
V2_CACHE_TTL = 300

_TERMINAL_FINISH = ("stop", "length", "content-filter", "error", "unknown")
_TASK_POLL_SECONDS = 3
_WAIT_POLL_SECONDS = 3


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


# ---------------------------------------------------------------------------
# Project map — line-based parse (dir/port in any order inside a block),
# matching oc-servectl's awk parser. The old regex required `dir` to be
# immediately followed by `port`, so a reordered file parsed as "unknown
# project" here while oc-servectl happily accepted it.


def projects_map():
    """Parse the fixed two-level projects YAML. Returns
    (default_name, {name: {"dir": str, "port": int}})."""
    try:
        text = open(PROJECTS_YAML).read()
    except FileNotFoundError:
        raise RuntimeError("projects file %s not found" % PROJECTS_YAML)
    default, projects, cur = None, {}, None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[0].isspace():
            cur = None
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(\S+)", stripped)
            if m and m.group(1) == "default":
                default = m.group(2)
            continue
        m = re.match(r"^  ([A-Za-z_][A-Za-z0-9_-]*):\s*$", line)
        if m:
            cur = m.group(1)
            projects.setdefault(cur, {})
            continue
        m = re.match(r"^    ([A-Za-z_][A-Za-z0-9_-]*):\s*(\S+)", line)
        if m and cur:
            projects[cur][m.group(1)] = m.group(2)
            continue
        # other lines inside the block are ignored (forward-compatible)
    out = {}
    for n, cfg in projects.items():
        if cfg.get("dir") and cfg.get("port"):
            try:
                out[n] = {"dir": cfg["dir"], "port": int(cfg["port"])}
            except ValueError:
                raise RuntimeError("project %r has a non-numeric port %r" % (n, cfg["port"]))
    return default, out


_PROJECTS_CACHE = None


def _projects():
    global _PROJECTS_CACHE
    if _PROJECTS_CACHE is None:
        _PROJECTS_CACHE = projects_map()
    return _PROJECTS_CACHE


def project_info(name):
    """Resolve a project name (the literal name `default` resolves to the
    YAML's default project) to {name, dir, port}."""
    default, projects = _projects()
    if name == "default" and default:
        name = default
    if name not in projects:
        raise RuntimeError("project %r not in %s (known: %s)"
                           % (name, PROJECTS_YAML, ", ".join(sorted(projects)) or "-"))
    p = projects[name]
    return {"name": name, "dir": p["dir"], "port": p["port"]}


# ---------------------------------------------------------------------------
# HTTP layer


def _request(name, path, method="GET", body=None, timeout=60):
    """One authenticated request against project <name>'s OpenCode endpoint.

    `path` is relative to the /api root (query string allowed). Returns
    (status, content_type, bytes). Raises ApiError on HTTP >= 400 and on
    connection failures (status 0).
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
        reason = getattr(e, "reason", e)
        raise ApiError(0, "cannot reach %s:%d: %s" % (HOST, p["port"], reason))
    except OSError as e:  # includes socket timeouts raised outside URLError
        raise ApiError(0, "cannot reach %s:%d: %s" % (HOST, p["port"], e))


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


# ---------------------------------------------------------------------------
# V2 gate — in-process cache + best-effort file cache (TTL) so repeated CLI
# invocations stop paying one /api/info round-trip each. A 401 always
# propagates uncached; only a verified-2.x response is ever cached.


_v2_cache = {}


def _v2_cache_read(key):
    try:
        blob = json.load(open(V2_CACHE_FILE))
        ent = (blob or {}).get(key)
        if ent and time.time() - ent.get("ts", 0) < V2_CACHE_TTL:
            return ent.get("info")
    except Exception:
        pass
    return None


def _v2_cache_write(key, info):
    try:
        blob = {}
        try:
            blob = json.load(open(V2_CACHE_FILE)) or {}
        except Exception:
            blob = {}
        blob[key] = {"ts": time.time(), "info": info}
        os.makedirs(os.path.dirname(V2_CACHE_FILE), exist_ok=True)
        tmp = "%s.tmp.%d" % (V2_CACHE_FILE, os.getpid())
        with open(tmp, "w") as f:
            json.dump(blob, f)
        os.replace(tmp, V2_CACHE_FILE)
    except Exception:
        pass  # the cache is best-effort; never fail a call over it


def require_v2(name):
    """Refuse to talk to anything but OpenCode >= 2.0 (cached per project)."""
    key = project_info(name)["name"]  # resolved name: `default` shares the entry
    if key in _v2_cache:
        return _v2_cache[key]
    info = _v2_cache_read(key)
    if info is not None:
        _v2_cache[key] = info
        return info
    port = project_info(key)["port"]
    try:
        info = _json(key, "/info")
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
    _v2_cache[key] = info
    _v2_cache_write(key, info)
    return info


def server(name):
    """GET /api/info (gated): {version, pid, urls, paths, capabilities}."""
    return require_v2(name)


# ---------------------------------------------------------------------------
# Session store — one entry per project: {session_id, last_anchor, agent,
# history:[{id,label,tokens,at}] (last 10 rotations)}. Atomic write
# (tmp + rename) under an exclusive flock so the agent and a cron check
# never interleave a read-modify-write. Tolerates the legacy agent-written
# shape {"proj": "ses_xxx"} so an upgrade never loses stored sessions.


def _norm_entry(v):
    if isinstance(v, str):
        return {"session_id": v, "last_anchor": None, "agent": None,
                "history": [], "dirs": {}}
    if isinstance(v, dict):
        hist = v.get("history")
        dirs = v.get("dirs")
        return {
            "session_id": v.get("session_id") or v.get("id"),
            "last_anchor": v.get("last_anchor"),
            "agent": v.get("agent"),
            "history": hist if isinstance(hist, list) else [],
            "dirs": dirs if isinstance(dirs, dict) else {},
        }
    return {"session_id": None, "last_anchor": None, "agent": None,
            "history": [], "dirs": {}}


def _store_load():
    try:
        with open(STORE) as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {}
    except ValueError:
        backup = "%s.corrupt-%d" % (STORE, int(time.time()))
        try:
            os.replace(STORE, backup)
            sys.stderr.write("warning: %s was not valid JSON; moved to %s\n" % (STORE, backup))
        except OSError:
            pass
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: _norm_entry(v) for k, v in raw.items()}


def _store_save(mapping):
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = "%s.tmp.%d" % (STORE, os.getpid())
    with open(tmp, "w") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STORE)


def store_entry(project):
    """The project's normalized store entry (never None; keys may be None).
    `dirs` is the work-item registry: {project-relative dir: session slot}."""
    name = project_info(project)["name"]
    entry = dict(_store_load().get(name) or {})
    entry.setdefault("session_id", None)
    entry.setdefault("last_anchor", None)
    entry.setdefault("agent", None)
    entry.setdefault("history", [])
    entry.setdefault("dirs", {})
    return entry


def store_update(project, **fields):
    """Merge fields into the project's entry under flock; returns the entry."""
    allowed = ("session_id", "last_anchor", "agent", "history", "dirs")
    name = project_info(project)["name"]
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    with open(STORE + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        mapping = _store_load()
        entry = mapping.get(name) or {"session_id": None, "last_anchor": None,
                                      "agent": None, "history": [], "dirs": {}}
        entry.setdefault("dirs", {})
        for k, v in fields.items():
            if k == "history" and isinstance(v, list):
                v = v[-10:]  # rotation history cap is a store invariant
            if k in allowed:
                entry[k] = v
        mapping[name] = entry
        _store_save(mapping)
        return entry


# ---------------------------------------------------------------------------
# Core API


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
    """Title/outcome/tokens/current agent for one session."""
    s = unwrap(req(name, "/session/%s" % session_id)) or {}
    return {
        "id": s.get("id", session_id),
        "title": s.get("title") or "",
        "outcome": s.get("outcome"),
        "agent": s.get("agent"),
        "cost": s.get("cost"),
        "time": s.get("time") or {},
        "tokens": s.get("tokens") or {},
        "location": s.get("location") or {},
    }


def _resolve_workdir(name, d):
    """Normalize a --dir work directory to a project-relative key.

    Relative paths resolve against the project dir; absolute paths must
    already be inside it. "." (the project root itself) maps to "." — the
    caller treats that as "no work dir" (the default stored session). The
    project dir is the hard boundary: anything resolving above or outside
    it is refused. This is a typo guard, not a sandbox — host symlinks are
    not resolved client-side.
    """
    root = project_info(name)["dir"].rstrip("/") or "/"
    d = (d or "").strip()
    if not d or d == ".":
        return "."
    absd = posixpath.normpath(d if d.startswith("/") else posixpath.join(root, d))
    if absd == root:
        return "."
    prefix = root if root.endswith("/") else root + "/"
    if not absd.startswith(prefix):
        raise RuntimeError(
            "--dir %r escapes the project dir %r — the project is the hard boundary"
            % (d, root))
    return posixpath.relpath(absd, root)


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


def delete_session(name, session_id):
    """DELETE /api/session/<id> — remove a session and all its data."""
    return unwrap(req(name, "/session/%s" % session_id, method="DELETE"))


def patch_session(name, session_id, title=None):
    """PATCH /api/session/<id> — update session properties (title)."""
    body = {}
    if title is not None:
        body["title"] = title
    return unwrap(req(name, "/session/%s" % session_id, method="PATCH", body=body))


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


def agents(name):
    """GET /api/agent: [{id, name, mode, hidden, permissions, ...}]."""
    return unwrap(req(name, "/agent?%s" % _loc(name))) or []


def switch_agent(name, session_id, agent):
    """POST /api/session/<id>/agent {"agent": id} -> 204.

    Switches the agent used by SUBSEQUENT provider turns (an agent-switched
    marker message is appended to the session). Callers should compare
    Session.Info.agent first to avoid marker spam.
    """
    req(name, "/session/%s/agent" % session_id, method="POST", body={"agent": agent})


def prompt(name, session_id, text, delivery=None):
    """Send a prompt via POST /api/session/<id>/prompt with body {"text": ...}.

    Returns the inbox item; its "id" is the user message id of this turn —
    the ANCHOR to pass to turn_status. Accepted does NOT mean finished.
    Optional delivery: "steer" (default) or "queue" — keep the default
    unless you specifically need queued delivery.

    On a transport failure (status 0) the request may still have been
    delivered: the newest user messages are checked for this exact text
    before a single retry, so a flaky link cannot double-deliver a prompt.
    """
    body = {"text": text}
    if delivery:
        body["delivery"] = delivery
    try:
        return unwrap(req(name, "/session/%s/prompt" % session_id, method="POST", body=body))
    except ApiError as e:
        if e.status != 0:
            raise
        for m in messages(name, session_id, 20):
            if m["type"] == "user" and m["text"] == text:
                return {"id": m["id"], "recovered": True}
        time.sleep(1.0)
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
            "created": (m.get("time") or {}).get("created"),
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
        else:  # user / system / synthetic / idle / agent-switched / ...
            row["text"] = m.get("text") or ""
        out.append(row)
    return out


def _messages_window(name, session_id, anchor):
    """messages() page that contains `anchor`, escalating the page size
    (50 -> 250 -> 1000): a long turn pushes the anchor out of a small
    newest-page. Falls back to the largest page the server accepts; when
    the anchor never appears the caller degrades to whole-page inspection.
    """
    best = None
    for limit in (50, 250, 1000):
        try:
            page = messages(name, session_id, limit)
        except ApiError as e:
            if e.status == 400 and best is not None:
                break  # server caps the page below this limit
            raise
        best = page
        if not anchor or any(m.get("id") == anchor for m in page):
            break
    return best or []


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

    `anchor` is the user message id returned by prompt(). The message page
    grows (50 -> 250 -> 1000) until the anchor is in it; if it never is,
    this degrades to whole-history inspection of the newest page.
    """
    active = is_running(name, session_id)
    msgs = _messages_window(name, session_id, anchor)
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
    elapsed = None
    if anchor and msgs and msgs[0].get("id") == anchor and msgs[0].get("created"):
        end_ms = (conclusion or {}).get("completed")
        if end_ms:
            elapsed = max(0, int((end_ms - msgs[0]["created"]) / 1000))
    return {"running": active, "outcome": outcome, "done": done,
            "conclusion": conclusion, "elapsed": elapsed}


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
    MUST be a single line (';' joined). Keep these read-only — the location
    shell is NOT gated by the plan agent's permissions.
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


def session_diff(name, session_id, query=None):
    """GET /api/session/<id>/diff[?<query>] -> [{file, patch, additions,
    deletions, status: added|deleted|modified}].

    Turn scoping: `from=<user message id>` (defaults to the newest user
    turn), `to=<later user message id>` extends through that turn,
    `context=<n>` trims hunk context. Build the query string here or pass
    None for the newest-turn default.
    """
    path = "/session/%s/diff" % session_id
    if query:
        path += "?" + query
    return unwrap(req(name, path)) or []


def permissions_pending(name, session_id):
    """Pending permission requests for the session:
    [{id, sessionID, action, resources[], message?, ...}]."""
    return unwrap(req(name, "/session/%s/permission" % session_id)) or []


def permission_reply(name, session_id, request_id, decision="once"):
    """Reply to a permission request. V2 key is "decision" (V1 used
    "response"): "once" | "always" | "reject"."""
    req(name, "/session/%s/permission/%s/reply" % (session_id, request_id),
        method="POST", body={"decision": decision})


def forms_pending(name, session_id):
    """Pending forms (structured questions from the remote agent):
    [{id, sessionID, title, fields}]."""
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
    require_v2(name)  # fs routes are V2 too; never bypass the gate
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


# ---------------------------------------------------------------------------
# High-level orchestration


def rotate_session(name, workdir=None):
    """Explicit rotation: label + title the old session, append it to the
    history (kept to 10), create the replacement, persist it. With
    `workdir`, rotates that work item's registry slot instead of the
    default stored session. Returns a dict {old: {...}|None, new: session_id}
    for the announcement."""
    entry = store_entry(name)
    if workdir is None:
        old = entry.get("session_id")
    else:
        old = (entry.get("dirs") or {}).get(workdir, {}).get("session_id")
    out = {"old": None, "new": None}
    hist = entry.get("history") or []
    if old:
        try:
            label = session_label(name, old)
            rep = session_report(name, old)
            # the label is '<19-char timestamp> <topic>' — the timestamp
            # itself contains a space, so slice by length, not by split
            topic = " ".join(label[20:].split())[:60] if len(label) > 20 else ""
            if topic and topic != "empty session (never used)":
                try:
                    patch_session(name, old, title=topic)  # best-effort title
                except ApiError:
                    pass
            hist.append({"id": old, "label": label, "tokens": rep.get("tokens"),
                         "at": int(time.time())})
            out["old"] = {"id": old, "label": label, "tokens": rep.get("tokens"),
                          "outcome": rep.get("outcome"),
                          "running": is_running(name, old)}
        except ApiError as e:
            if e.status != 404:
                raise
            out["old"] = {"id": old, "gone": True}
    fresh = new_session(name)
    fields = {"session_id": fresh["id"], "last_anchor": None, "agent": None,
              "history": hist[-10:]}
    if workdir is not None:
        dirs = dict(entry.get("dirs") or {})
        dirs[workdir] = {"session_id": fresh["id"], "last_anchor": None,
                         "agent": None}
        fields["dirs"] = dirs
    store_update(name, **fields)
    out["new"] = fresh["id"]
    return out


def _fmt_k(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    if n >= 1000:
        return "%.0fk" % (n / 1000)
    return "%g" % n


def _token_line(tokens, cost=None):
    t = tokens or {}
    cache = t.get("cache") or {}
    line = "Session tokens: input %s · output %s · cache read %s" % (
        _fmt_k(t.get("input")), _fmt_k(t.get("output")), _fmt_k(cache.get("read")))
    if cost is not None:
        try:
            line += " · cost $%.2f" % float(cost)
        except (TypeError, ValueError):
            pass
    return line


def _report_done(name, session_id, st):
    """Print conclusion + outcome + token line; exit code by outcome.
    stdout carries ONLY the report (directly relay-able); interim progress
    lives on stderr."""
    c = st.get("conclusion") or {}
    text = c.get("text") or ""
    if not text and c.get("tool_outputs"):
        text = "(no prose; last tool output)\n" + c["tool_outputs"][-1]
    if text:
        if len(text) > 4000:
            text = text[:4000] + \
                "\n(truncated — full text: oc_api.py answer %s %s)" % (name, session_id)
        print(text)
    for out in (c.get("tool_outputs") or [])[:3]:
        print("OUT:", out[:1200])
    outcome = st.get("outcome") or "unknown"
    suffix = " · %ds" % st["elapsed"] if st.get("elapsed") is not None else ""
    print("outcome: %s%s" % (outcome, suffix))
    rep = session_report(name, session_id)
    print(_token_line(rep["tokens"], rep.get("cost")))
    return 0 if outcome in ("succeeded", "unknown") else 1


def _drain_pending(name, session_id, no_approve):
    """Approve pending permissions (or demand a human); detect forms.
    Returns exit code 3 when a human decision is required, else None."""
    perms = permissions_pending(name, session_id)
    if perms:
        if no_approve:
            print("pending permission(s) need a human decision:")
            for p in perms:
                print("  %s: %s %s" % (p.get("id"), p.get("action"),
                                       " ".join(p.get("resources") or [])[:160]))
            print("approve with: oc_api.py approve <project> <request_id> [once|always|reject]")
            return 3
        for p in perms:
            permission_reply(name, session_id, p["id"], "once")
            sys.stderr.write("approved permission %s (%s %s)\n"
                             % (p.get("id"), p.get("action"),
                                " ".join(p.get("resources") or [])[:120]))
    forms = forms_pending(name, session_id)
    if forms:
        print("pending form(s) need a human decision:")
        print(json.dumps(forms, ensure_ascii=False, indent=1))
        print("answer with: oc_api.py form <project> <form_id> key=value ...")
        return 3
    return None


def _resolve_task_session(name, workdir=None):
    """Stored session for the project, recovering from a 404 (dead stored
    id) by creating a replacement — recovery, not rotation. With `workdir`,
    resolves the work item's registry slot instead of the default stored
    session; work-item sessions are ordinary ROOT sessions (full-project
    access) — the workdir is only the client-side routing key (see
    SKILL.md "Work-item sessions")."""
    entry = store_entry(name)
    if workdir is None:
        sid = entry.get("session_id")
    else:
        sid = (entry.get("dirs") or {}).get(workdir, {}).get("session_id")
    if sid:
        try:
            session_report(name, sid)
            return sid, False
        except ApiError as e:
            if e.status != 404:
                raise
            sys.stderr.write("stored session %s is gone (404); creating a replacement\n" % sid)
    if workdir is not None:
        try:
            list_host_dir(name, workdir)  # typo guard on the create path only
        except ApiError as e:
            if e.status != 404:
                raise
            raise RuntimeError("--dir %r does not exist in the project (typo?); "
                               "refusing to create a work-item session for it"
                               % workdir)
    fresh = new_session(name)
    fields = {"session_id": fresh["id"], "last_anchor": None, "agent": None}
    if workdir is not None:
        dirs = dict(entry.get("dirs") or {})
        dirs[workdir] = {"session_id": fresh["id"], "last_anchor": None,
                         "agent": None}
        fields["dirs"] = dirs
    store_update(name, **fields)
    return fresh["id"], True


def _ensure_agent(name, session_id, agent):
    """Validate `agent` against GET /api/agent, then switch the session to
    it when it differs from the session's current agent (Session.Info.agent)
    — so a previous build run never leaves residual write access. Fails
    hard when the switch is refused: proceeding on the wrong agent would
    silently run with stale permissions."""
    known = {a.get("id") for a in agents(name)}
    known.discard(None)
    if known and agent not in known:
        raise RuntimeError("agent %r is not registered on the host (available: %s)"
                           % (agent, ", ".join(sorted(known))))
    current = session_report(name, session_id)["agent"]
    if current == agent:
        return False
    switch_agent(name, session_id, agent)
    return True


def _busy_for(name, sid):
    """', running for Xs' suffix for busy errors — how long the stored
    anchor (the running turn) has been going, so the caller can decide
    between waiting and stopping. Empty string when unknown."""
    anchor = store_entry(name).get("last_anchor")
    if not anchor:
        return ""
    for m in messages(name, sid, 50):
        if m.get("id") == anchor and m.get("created"):
            return ", running for %ds" % max(0, int(time.time() - m["created"] / 1000))
    return ""


# ---------------------------------------------------------------------------
# CLI


def _usage(msg):
    sys.stderr.write("usage: %s\n(run with no arguments for the full CLI reference)\n" % msg)
    return 2


def _parse_opts(rest, value_opts):
    """Split args into (positionals, opts). Flags: --flag, --flag=value,
    --flag value (when listed in value_opts). A bare `--` ends flag parsing
    (for prompt text that starts with '-')."""
    pos, opts, no_more = [], {}, False
    i = 0
    while i < len(rest):
        tok = rest[i]
        if not no_more and tok == "--":
            no_more = True
        elif not no_more and tok.startswith("--"):
            if "=" in tok:
                k, v = tok[2:].split("=", 1)
                opts[k] = v
            elif tok[2:] in value_opts and i + 1 < len(rest):
                opts[tok[2:]] = rest[i + 1]
                i += 1
            else:
                opts[tok[2:]] = True
        else:
            pos.append(tok)
        i += 1
    return pos, opts


def _cmd_task(rest):
    pos, o = _parse_opts(rest, {"file", "agent", "timeout", "delivery", "dir"})
    if not pos:
        return _usage("task <project> [text] [--stdin | --file F] [--new] "
                      "[--dir D] [--agent A --allow-build] [--timeout S] "
                      "[--no-approve] [--delivery steer|queue]")
    project = pos[0]
    if "stdin" in o:
        text = sys.stdin.read()
    elif "file" in o:
        text = open(o["file"]).read()
    elif len(pos) > 1:
        text = " ".join(pos[1:])
    else:
        return _usage("task needs prompt text (positional, --stdin, or --file)")
    if not text.strip():
        return _usage("task needs non-empty prompt text")
    agent = o.get("agent", "plan")
    if agent != "plan" and "allow-build" not in o:
        sys.stderr.write("refusing agent %r without --allow-build: non-plan agents "
                         "have write access; get the user's consent first\n" % agent)
        return 2
    delivery = o.get("delivery")
    if delivery not in (None, "steer", "queue"):
        return _usage("--delivery must be steer or queue")
    try:
        timeout = float(o.get("timeout", 90))
    except ValueError:
        return _usage("--timeout must be a number of seconds")

    name = project_info(project)["name"]
    workdir = None
    if "dir" in o:
        try:
            workdir = _resolve_workdir(name, o["dir"])
        except RuntimeError as e:
            return _usage(str(e))
        if workdir == ".":
            workdir = None  # the project root IS the default stored session
    if "new" in o:
        r = rotate_session(name, workdir)
        sys.stderr.write("rotated: %s -> %s\n"
                         % ((r["old"] or {}).get("id", "-"), r["new"]))
        if (r["old"] or {}).get("running"):
            sys.stderr.write("warning: the old session %s is STILL RUNNING in the "
                             "background — it keeps spending tokens until it finishes "
                             "or is interrupted (`oc_api.py interrupt %s %s`)\n"
                             % (r["old"]["id"], name, r["old"]["id"]))
    sid, _created = _resolve_task_session(name, workdir)
    _ensure_agent(name, sid, agent)
    store_update(name, agent=agent)

    if is_running(name, sid):
        sys.stderr.write("error: session %s is busy (a previous turn is still "
                         "running%s); use `wait`, `stop`, or `task --new`\n"
                         % (sid, _busy_for(name, sid)))
        return 1
    try:
        item = prompt(name, sid, text, delivery)
    except ApiError as e:
        if e.status == 409:
            sys.stderr.write("error: session %s is busy (409%s); use `wait`, `stop`, "
                             "or `task --new`\n" % (sid, _busy_for(name, sid)))
            return 1
        raise
    anchor = (item or {}).get("id")
    if not anchor:
        raise ApiError(0, "prompt returned no anchor id: %r" % (item,))
    if workdir is not None:
        entry = store_entry(name)
        dirs = dict(entry.get("dirs") or {})
        dirs[workdir] = {"session_id": sid, "last_anchor": anchor, "agent": agent}
        store_update(name, session_id=sid, last_anchor=anchor, agent=agent,
                     dirs=dirs)
    else:
        store_update(name, session_id=sid, last_anchor=anchor, agent=agent)

    deadline = time.time() + timeout
    while True:
        rc = _drain_pending(name, sid, "no-approve" in o)
        if rc is not None:
            return rc
        st = turn_status(name, sid, anchor)
        if st["done"]:
            return _report_done(name, sid, st)
        if time.time() >= deadline:
            print("still running: session %s anchor %s — re-check with `wait` or "
                  "`status <project>`" % (sid, anchor))
            return 4
        time.sleep(_TASK_POLL_SECONDS)


def _cmd_wait(rest):
    pos, o = _parse_opts(rest, {"timeout"})
    if not pos:
        return _usage("wait <project> [--timeout S] [--no-approve]")
    try:
        timeout = float(o.get("timeout", 300))
    except ValueError:
        return _usage("--timeout must be a number of seconds")
    name = project_info(pos[0])["name"]
    entry = store_entry(name)
    sid = entry.get("session_id")
    if not sid:
        sys.stderr.write("error: no stored session for %s; send a task first\n" % name)
        return 1
    anchor = entry.get("last_anchor")
    deadline = time.time() + timeout
    long_poll = True
    while True:
        rc = _drain_pending(name, sid, "no-approve" in o)
        if rc is not None:
            return rc
        st = turn_status(name, sid, anchor)
        if st["done"]:
            return _report_done(name, sid, st)
        remaining = deadline - time.time()
        if remaining <= 0:
            print("still running: session %s anchor %s — deadline reached" % (sid, anchor))
            return 4
        if long_poll:
            try:
                finished = wait(name, sid, timeout=min(30.0, max(5.0, remaining)))
                if not finished:
                    time.sleep(1)
                continue
            except ApiError as e:
                if e.status == 401 or e.tag in ("SessionNotFoundError",):
                    raise
                long_poll = False  # endpoint missing/unreachable -> poll
                continue
        time.sleep(_WAIT_POLL_SECONDS)


def _cmd_stop(rest):
    pos, _o = _parse_opts(rest, set())
    if not pos:
        return _usage("stop <project>")
    name = project_info(pos[0])["name"]
    entry = store_entry(name)
    sid = entry.get("session_id")
    if not sid:
        sys.stderr.write("error: no stored session for %s; send a task first\n" % name)
        return 1
    r = interrupt(name, sid) or {}
    print(json.dumps(r, ensure_ascii=False))
    return 0


def _cmd_status(rest):
    pos, _o = _parse_opts(rest, set())
    if pos:
        name = project_info(pos[0])["name"]
        entry = store_entry(name)
        sid = entry.get("session_id")
        if not sid:
            sys.stderr.write("error: no stored session for %s; send a task first\n" % name)
            return 1
        st = turn_status(name, sid, entry.get("last_anchor"))
        conclusion = st.get("conclusion") or {}
        try:
            pending = {"permissions": len(permissions_pending(name, sid)),
                       "forms": len(forms_pending(name, sid))}
        except ApiError:
            pending = {"permissions": -1, "forms": -1}
        idle_ms = (session_report(name, sid).get("time") or {}).get("idle")
        print(json.dumps({
            "project": name,
            "session_id": sid,
            "agent": entry.get("agent"),
            "last_anchor": entry.get("last_anchor"),
            "turn": {"running": st["running"], "outcome": st["outcome"],
                     "done": st["done"], "elapsed": st.get("elapsed")},
            "idle_for": (max(0, int((time.time() * 1000 - idle_ms) / 1000))
                         if idle_ms else None),
            "conclusion": (conclusion.get("text") or "")[:400],
            "pending": pending,
        }, ensure_ascii=False, indent=1))
        return 0
    _default, projects = _projects()
    header = ["project", "session", "agent", "run", "outcome", "updated", "topic"]
    rows = []
    loaded = _store_load()
    for n in sorted(projects):
        entry = loaded.get(n) or {}
        sid = entry.get("session_id")
        if not sid:
            rows.append([n, "-", "-", "-", "-", "-", "-"])
            continue
        try:
            running = is_running(n, sid)
            rep = session_report(n, sid)
            upd = (rep.get("time") or {}).get("updated")
            ts = (datetime.datetime.fromtimestamp(upd / 1000).strftime("%m-%d %H:%M")
                  if upd else "-")
            topic = " ".join((rep.get("title") or "").split())[:24] or "-"
            rows.append([n, sid[:15], entry.get("agent") or "-",
                         "yes" if running else "no", rep.get("outcome") or "-",
                         ts, topic])
        except ApiError as e:
            rows.append([n, sid[:15], "-", "-", "ERR:%s" % e.status, "-", "-"])
    widths = [max([len(header[i])] + [len(r[i]) for r in rows])
              for i in range(len(header))]
    fmt = "  ".join("%%-%ds" % w for w in widths[:-1]) + "%s"
    print(fmt % tuple(header))
    for r in rows:
        print(fmt % tuple(r))
    return 0


def _cmd_diff(rest):
    pos, o = _parse_opts(rest, {"from", "to"})
    if not pos:
        return _usage("diff <project> [--all | --from ID [--to ID]]")
    name = project_info(pos[0])["name"]
    entry = store_entry(name)
    sid = entry.get("session_id")
    if not sid:
        sys.stderr.write("error: no stored session for %s; send a task first\n" % name)
        return 1
    q = []
    if "from" in o:
        q.append("from=%s" % o["from"])
    elif "all" in o:
        users = [m for m in messages(name, sid, 1000) if m["type"] == "user"]
        if not users:
            print("(no user turns to diff)")
            return 0
        q.append("from=%s" % users[0]["id"])
        q.append("to=%s" % users[-1]["id"])
    elif entry.get("last_anchor"):
        q.append("from=%s" % entry["last_anchor"])
    if "to" in o:
        q.append("to=%s" % o["to"])
    items = session_diff(name, sid, "&".join(q) or None)
    if not items:
        print("(no changes)")
        return 0
    total_add = total_del = 0
    patches, changed_lines = [], 0
    for d in items:
        f, st = d.get("file") or "?", d.get("status") or "?"
        a, dl = d.get("additions") or 0, d.get("deletions") or 0
        total_add += a
        total_del += dl
        print("%-9s %s  +%-4d -%-4d" % (st, f, a, dl))
        patch = d.get("patch") or ""
        patches.append(patch)
        changed_lines += sum(1 for l in patch.splitlines()
                             if (l.startswith("+") or l.startswith("-"))
                             and not l.startswith(("+++", "---")))
    print("total: %d files, +%d -%d" % (len(items), total_add, total_del))
    body = "\n".join(p for p in patches if p)
    if changed_lines > 100:
        path = os.path.join(DIFF_DIR, "%s-%s.patch" % (name, sid[:8]))
        os.makedirs(DIFF_DIR, exist_ok=True)
        with open(path, "w") as f:
            f.write(body)
        print("patch (%d changed lines) saved to %s" % (changed_lines, path))
    elif body:
        print(body)


def _cmd_rotate(rest):
    pos, _o = _parse_opts(rest, set())
    if not pos:
        return _usage("rotate <project>")
    name = project_info(pos[0])["name"]
    r = rotate_session(name)
    if (r["old"] or {}).get("running"):
        sys.stderr.write("warning: the old session %s is STILL RUNNING in the "
                         "background — it keeps spending tokens until it finishes "
                         "or is interrupted (`oc_api.py interrupt %s %s`)\n"
                         % (r["old"]["id"], name, r["old"]["id"]))
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0


def _cmd_overview(rest):
    pos, _o = _parse_opts(rest, set())
    if not pos:
        return _usage("overview <project>")
    name = project_info(pos[0])["name"]

    def run(title, command):
        print("== %s ==" % title)
        try:
            out = shell(name, command, timeout=60)
        except ApiError as e:
            print("(failed: %s)" % e.message)
            return
        print((out.rstrip() or "(empty)") + "\n")

    def is_git():
        try:
            return shell(name, "git rev-parse --is-inside-work-tree 2>/dev/null",
                         timeout=30).strip() == "true"
        except ApiError:
            return False

    run("README (first 6000 chars)",
        "head -c 6000 README.md 2>/dev/null || head -c 6000 readme.md 2>/dev/null")
    if is_git():
        run("last 14 days of commits",
            "git log --since='14 days ago' --all --oneline | head -30; "
            "git log -1 --format='%ci %s'")
        run("module map",
            "find . -maxdepth 2 -type d -not -path './.git*' -not -path './node_modules*' "
            "| sort | head -40")
        run("commit topics (14 days)",
            "git log --since='14 days ago' --all --oneline "
            "| sed -E 's/^[a-f0-9]+ //; s/\\(.*\\)//' | awk '{print $1}' "
            "| sort | uniq -c | sort -rn | head -15")
    else:
        print("== commit history ==\n(not a git repository — skipped)\n")
        run("module map",
            "find . -maxdepth 2 -type d -not -path './.git*' -not -path './node_modules*' "
            "| sort | head -40")
    return 0


def _cmd_todos(rest):
    pos, _o = _parse_opts(rest, set())
    if not pos:
        return _usage("todos <project>")
    name = project_info(pos[0])["name"]
    listing = shell(
        name,
        "find . \\( -iname 'TODO*' -o -iname 'ROADMAP*' -o -iname '*design*.md' \\) "
        "-not -path './.git/*' -type f 2>/dev/null | head -10",
        timeout=60)
    files = [l.strip() for l in listing.splitlines() if l.strip()]
    if not files:
        print("no TODO/ROADMAP/design docs found (try `overview <project>`)")
        return 0
    for f in files[:5]:
        print("== %s ==" % f)
        out = shell(name, "grep -n -E '^\\s*- \\[ \\]|TODO|next steps' %s 2>/dev/null "
                    "| head -30" % shlex.quote(f), timeout=60)
        print((out.rstrip() or "(no open items)") + "\n")
    return 0


_HANDOFF_TMPL = '''#!/usr/bin/env python3
"""One-shot cron check for Hermes cronjob_manage — generated by
`oc_api.py handoff <project> --write`. Prints EXACTLY ONE deterministic
line (RUNNING / DONE:<outcome> / TIMEOUT / ERR:<reason>): the monitor
gate hashes exact stdout bytes, so never add timestamps or extra output.
ERR:form-pending means a form awaits a human answer — relay it to the
user. Runs with the container's default OPENCODE_* environment."""
import sys
import time as _time

sys.path.insert(0, @@OC_DIR@@)
import oc_api as oc

PROJECT = @@PROJECT@@
SESSION = @@SESSION@@
ANCHOR = @@ANCHOR@@
DEADLINE = @@DEADLINE@@  # epoch seconds; baked at generation (~30 min)


def main():
    try:
        for p in oc.permissions_pending(PROJECT, SESSION):
            oc.permission_reply(PROJECT, SESSION, p["id"], "once")
        if oc.forms_pending(PROJECT, SESSION):
            print("ERR:form-pending")
            return
        st = oc.turn_status(PROJECT, SESSION, ANCHOR)
        if st["done"]:
            print("DONE:%s" % (st["outcome"] or "unknown"))
        elif _time.time() > DEADLINE:
            print("TIMEOUT")
        elif st["running"]:
            print("RUNNING")
        else:
            print("ERR:stuck-not-running-not-done")
    except Exception as e:
        print("ERR:%s" % str(e).splitlines()[0][:120])


main()
'''


def _cmd_handoff(rest):
    pos, o = _parse_opts(rest, set())
    if not pos:
        return _usage("handoff <project> [--write] — generate the deterministic "
                      "cron check script for the stored turn")
    name = project_info(pos[0])["name"]
    entry = store_entry(name)
    sid = entry.get("session_id")
    if not sid:
        sys.stderr.write("error: no stored session for %s; send a task first\n" % name)
        return 1
    script = (_HANDOFF_TMPL
              .replace("@@OC_DIR@@", repr(os.path.dirname(os.path.abspath(__file__))))
              .replace("@@PROJECT@@", repr(name))
              .replace("@@SESSION@@", repr(sid))
              .replace("@@ANCHOR@@", repr(entry.get("last_anchor")))
              .replace("@@DEADLINE@@", str(int(time.time()) + 1800)))
    if "write" in o:
        os.makedirs(SCRIPTS_DIR, exist_ok=True)
        base = "oc_check_%s.py" % re.sub(r"[^A-Za-z0-9_-]", "_", name)
        path = os.path.join(SCRIPTS_DIR, base)
        with open(path, "w") as f:
            f.write(script)
        os.chmod(path, 0o755)
        sys.stderr.write("written to %s\n" % path)
        print(base)  # bare filename — cronjob_manage refuses absolute paths
        return 0
    sys.stdout.write(script)
    return 0


def _cmd_selftest(rest):
    pos, _o = _parse_opts(rest, set())
    if not pos:
        return _usage("selftest <project>")
    name = project_info(pos[0])["name"]
    results = []
    scratch = {}

    def check(label, fn):
        try:
            results.append((label, True, fn() or "ok"))
        except Exception as e:  # noqa: BLE001 — diagnostics tool
            results.append((label, False, str(e)))

    check("info version gate",
          lambda: "v%s" % (server(name).get("version") or "?"))
    check("agent list",
          lambda: "agents: %s" % ",".join(sorted(a.get("id") for a in agents(name))))
    check("session list",
          lambda: "%d root sessions" % len(root_sessions(name, limit=5)))

    def _create():
        scratch["id"] = new_session(name)["id"]
        return scratch["id"]
    check("session create (dir asserted)", _create)

    def _switch():
        if not scratch.get("id"):
            raise RuntimeError("no scratch session")
        switch_agent(name, scratch["id"], "plan")
        return "switched scratch session to plan (204)"
    check("switch agent", _switch)

    def _messages():
        note = "scratch page ok (%d msgs)" % len(messages(name, scratch["id"], 50))
        sid = store_entry(name).get("session_id")
        if sid:
            note += "; stored-session probe: %d msgs returned for limit=1000" \
                    % len(messages(name, sid, 1000))
        return note
    check("message page shape + limit probe", _messages)

    def _pending():
        return ("permissions %d, forms %d"
                % (len(permissions_pending(name, scratch["id"])),
                   len(forms_pending(name, scratch["id"]))))
    check("permission/form lists", _pending)

    def _wait():
        try:
            return "long-poll ok (%s)" % ("idle" if wait(name, scratch["id"], 10) else "busy")
        except ApiError as e:
            if e.status in (404, 405, 501):
                raise RuntimeError("experimental wait endpoint missing (%d)" % e.status)
            return "long-poll responded (HTTP %d)" % e.status
    check("experimental wait", _wait)

    def _diff():
        try:
            d = session_diff(name, scratch["id"])
        except ApiError as e:
            if e.status == 400:
                return "ok (empty session has no turn to diff)"
            raise
        return "diff shape: %s" % ",".join(sorted(d[0].keys()) if d else ["(empty)"])
    check("diff shape", _diff)

    if scratch.get("id"):
        try:
            delete_session(name, scratch["id"])
        except ApiError as e:
            sys.stderr.write("warning: scratch session cleanup failed: %s\n" % e.message)

    failed = 0
    for i, (label, ok, detail) in enumerate(results, 1):
        if not ok:
            failed += 1
        print("[%d/%d] %-34s %s  %s" % (i, len(results), label,
                                        "PASS" if ok else "FAIL", detail))
    return 1 if failed else 0


def _cmd_session_get(rest):
    pos, _o = _parse_opts(rest, set())
    if not pos:
        return _usage("session-get <project> [session_id|last_anchor|agent|history]")
    entry = store_entry(pos[0])
    if len(pos) > 1:
        if pos[1] not in entry:
            return _usage("session-get key must be session_id|last_anchor|agent|history")
        print(json.dumps(entry[pos[1]], ensure_ascii=False))
    else:
        print(json.dumps(entry, ensure_ascii=False, indent=1))
    return 0


def _cmd_session_set(rest):
    pos, _o = _parse_opts(rest, set())
    if len(pos) != 3 or pos[1] not in ("session_id", "last_anchor", "agent"):
        return _usage("session-set <project> <session_id|last_anchor|agent> <value>")
    entry = store_update(pos[0], **{pos[1]: pos[2]})
    print(json.dumps(entry, ensure_ascii=False))
    return 0


def _cmd_approve(rest):
    if not rest:
        return _usage("approve <project> <request_id> [once|always|reject]")
    project = rest[0]
    if len(rest) < 2:
        return _usage("approve <project> <request_id> [once|always|reject]")
    decision = rest[2] if len(rest) > 2 else "once"
    if decision not in ("once", "always", "reject"):
        return _usage("decision must be once, always, or reject")
    name = project_info(project)["name"]
    sid = store_entry(name).get("session_id")
    if not sid:
        sys.stderr.write("error: no stored session for %s\n" % name)
        return 1
    permission_reply(name, sid, rest[1], decision)
    print("replied %s to permission %s" % (decision, rest[1]))
    return 0


def _cmd_form(rest):
    if not rest:
        return _usage("form <project> [<form_id> key=value ...]")
    name = project_info(rest[0])["name"]
    sid = store_entry(name).get("session_id")
    if not sid:
        sys.stderr.write("error: no stored session for %s\n" % name)
        return 1
    if len(rest) == 1:
        print(json.dumps(forms_pending(name, sid), ensure_ascii=False, indent=1))
        return 0
    if len(rest) < 3:
        return _usage("form <project> <form_id> key=value [key=value ...]")
    answer = {}
    for kv in rest[2:]:
        if "=" not in kv:
            return _usage("form answers must look like key=value")
        k, v = kv.split("=", 1)
        answer[k] = v
    form_reply(name, sid, rest[1], answer)
    print("replied to form %s" % rest[1])
    return 0


def _cmd_interrupt(rest):
    if len(rest) < 2:
        return _usage("interrupt <project> <session_id>")
    name = project_info(rest[0])["name"]
    print(json.dumps(interrupt(name, rest[1]) or {}, ensure_ascii=False))
    return 0


def _main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd, rest = argv[1], argv[2:]
    high = {
        "task": _cmd_task, "wait": _cmd_wait, "stop": _cmd_stop,
        "status": _cmd_status, "diff": _cmd_diff, "rotate": _cmd_rotate,
        "overview": _cmd_overview, "todos": _cmd_todos, "selftest": _cmd_selftest,
        "handoff": _cmd_handoff,
        "session-get": _cmd_session_get, "session-set": _cmd_session_set,
        "approve": _cmd_approve, "form": _cmd_form, "interrupt": _cmd_interrupt,
    }
    if cmd in high:
        return high[cmd](rest)
    if not rest:
        print(__doc__)
        return 2
    proj = rest[0]
    if cmd == "server":
        print(json.dumps(server(proj), ensure_ascii=False))
    elif cmd == "sessions":
        for s in root_sessions(proj):
            upd = (s.get("time") or {}).get("updated", 0)
            ts = datetime.datetime.fromtimestamp(upd / 1000).strftime("%m-%d %H:%M")
            print(s["id"], "|", ts, "|", (s.get("title") or "")[:90])
    elif cmd == "new":
        print(new_session(proj).get("id"))
    elif cmd == "info" and len(rest) > 1:
        r = session_report(proj, rest[1])
        print(r["id"], "|", r["title"] or "(untitled)", "| outcome:", r["outcome"])
        print(json.dumps(r["tokens"], ensure_ascii=False))
    elif cmd == "label" and len(rest) > 1:
        print(session_label(proj, rest[1]))
    elif cmd == "prompt" and len(rest) > 2:
        item = prompt(proj, rest[1], " ".join(rest[2:]))
        print(item.get("id"))
    elif cmd == "answer" and len(rest) > 1:
        m = final_answer(proj, rest[1])
        if not m:
            print("(no terminal assistant message yet)")
            return 1
        print(m["text"][:3000])
        for out in m["tool_outputs"]:
            print("OUT:", out[:1500])
    elif cmd == "pending" and len(rest) > 1:
        print(json.dumps({"permissions": permissions_pending(proj, rest[1]),
                          "forms": forms_pending(proj, rest[1])}, ensure_ascii=False))
    elif cmd == "shell" and len(rest) > 1:
        print(shell(proj, rest[1]))
    elif cmd == "file" and len(rest) > 1:
        sys.stdout.buffer.write(read_host_file(proj, rest[1]))
    elif cmd == "ls":
        target = rest[1] if len(rest) > 1 else "."
        for e in list_host_dir(proj, target):
            print("%-10s %s" % (e.get("type", "?"), e.get("path", "")))
    else:
        print(__doc__)
        return 2
    return 0


def main(argv):
    try:
        return _main(argv)
    except ApiError as e:
        msg = "error: %s" % e.message
        if e.status == 401:
            msg += ("\nhint: check that OPENCODE_SERVER_PASSWORD in config/.env "
                    "matches the host's ~/.config/opencode-serve/auth.env")
        elif e.status == 0:
            msg += ("\nhint: is opencode-serve@<project> up? run "
                    "`oc-servectl status` on the host and inspect the unit journal")
        sys.stderr.write(msg + "\n")
        return 1
    except RuntimeError as e:
        sys.stderr.write("error: %s\n" % e)
        return 1
    except ValueError as e:
        sys.stderr.write("error: %s\n" % e)
        return 1
    except OSError as e:
        sys.stderr.write("error: %s\n" % e)
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv))
