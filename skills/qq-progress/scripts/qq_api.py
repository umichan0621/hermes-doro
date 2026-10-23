#!/usr/bin/env python3
"""QQ Official Bot v2 API client: token / passive send / recall.

Reads QQ_APP_ID, QQ_CLIENT_SECRET, QQBOT_HOME_CHANNEL from /opt/data/.env.
Stdlib only.

CLI:
    qq_api.py send "text" [--seq N] [--openid OID]   # passive reply to latest inbound msg
    qq_api.py recall "<message_id>" [--openid OID]
Prints the message id on send (save it for recall). Exit 1 on failure.
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ENV_PATH = "/opt/data/.env"
STATE_DB = "/opt/data/state.db"
TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"


def _env():
    env = {}
    for line in open(ENV_PATH):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v.strip()
    return env


def latest_inbound_msg_id():
    row = sqlite3.connect(STATE_DB).execute(
        "select platform_message_id from messages where role='user' "
        "and platform_message_id is not null order by id desc limit 1").fetchone()
    if not row:
        sys.exit("no inbound msg_id found in state.db")
    return row[0]


def get_token(env):
    body = json.dumps({"appId": env["QQ_APP_ID"], "clientSecret": env["QQ_CLIENT_SECRET"]}).encode()
    for attempt in range(4):
        req = urllib.request.Request(TOKEN_URL, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                tok = json.loads(r.read()).get("access_token", "")
        except urllib.error.HTTPError as e:
            tok = ""
            print(f"token fetch http {e.code}", file=sys.stderr)
        if len(tok) > 50:  # dud tokens are ~20 chars
            return tok
        time.sleep(5 * (attempt + 1))
    sys.exit("token service keeps returning dud tokens; try again later")


def _api(method, path, token, body=None):
    req = urllib.request.Request(API_BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "QQBot " + token})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["send", "recall"])
    p.add_argument("text", nargs="?")
    p.add_argument("--seq", type=int, default=80)
    p.add_argument("--openid", default="")
    a = p.parse_args()

    env = _env()
    openid = a.openid or env.get("QQBOT_HOME_CHANNEL", "")
    if not openid:
        sys.exit("no openid: pass --openid or set QQBOT_HOME_CHANNEL")
    tok = get_token(env)

    if a.cmd == "send":
        body = {"content": a.text, "msg_type": 0,
                "msg_id": latest_inbound_msg_id(), "msg_seq": a.seq}
        s, b = _api("POST", f"/v2/users/{openid}/messages", tok, body)
        if s != 200:
            sys.exit(f"send {s}: {b}")
        print(json.loads(b)["id"])  # SAVE for recall
    else:
        s, b = _api("DELETE", f"/v2/users/{openid}/messages/{urllib.parse.quote(a.text)}", tok)
        if s != 200:
            sys.exit(f"recall {s}: {b}")
        print("recalled")


if __name__ == "__main__":
    main()
