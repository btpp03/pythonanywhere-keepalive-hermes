#!/usr/bin/env python3
"""Keep the Nezha agent alive on a free PythonAnywhere account.

Free PA reaps background processes inside consoles, so `setsid nohup` is not
enough: something has to log in from outside and restart them. This script runs
on GitHub Actions (triggered by cron-job.org) and does exactly that:

    login (requests) -> open the bash console over SockJS -> probe -> heal

Health = bridge process + agent process + an end-to-end HTTPS probe through the
local CONNECT bridge to the panel. All three must hold; otherwise it restarts
`~/nz/start.sh` once and re-probes.

Exit 0 = healthy (or healed).  Exit 1 = still broken (run goes red -> GitHub
failure mail + one Telegram line).

Env:
  PA_USER, PA_PASS   PythonAnywhere credentials (secrets)
  NZ_KEY             Nezha agent client secret (secret)
  RELAY_HOST         optional: current ngrok host, used to re-pin bridge.py
  QUIET=true         cron-job.org leg: stay silent when nothing had to be done
  MODE=status        probe only, never restart (for manual smoke tests)
  TG_BOT_TOKEN / TG_CHAT_ID  optional notification channel
"""
import json
import os
import random
import re
import ssl
import string
import sys
import time
import urllib.parse
import urllib.request

import requests
import websocket  # websocket-client

BASE = "https://www.pythonanywhere.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][B0]|\x1b[=>]|\r?\n\r?.*?\x1b\[K|\x08+")

USER = os.environ["PA_USER"]
PASS = os.environ["PA_PASS"]
NZKEY = os.environ.get("NZ_KEY", "")
RELAY_HOST = os.environ.get("RELAY_HOST", "").strip()
QUIET = os.environ.get("QUIET", "").lower() in ("1", "true", "yes")
MODE = os.environ.get("MODE", "run").lower()
EVENT = os.environ.get("GITHUB_EVENT_NAME", "")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT_ID", "")
REPO_URL = "https://github.com/" + os.environ.get("GITHUB_REPOSITORY", "btpp03/pythonanywhere-keepalive-hermes")


def log(*a):
    print(*a, flush=True)


def notify(text):
    if not (TG_TOKEN and TG_CHAT):
        log("[tg] not configured, skipping:", text.replace("\n", " | ")[:120])
        return
    try:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text,
                                       "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN, data=data,
            headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            log("[tg] ok=%s" % (json.loads(r.read().decode()).get("ok")))
    except Exception as e:                  # noqa: BLE001
        log("[tg] failed: %r" % (e,))


class PA:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.cid = None
        self.ws = None

    # ---------- login / console ----------
    def login(self):
        last = None
        for _ in range(3):
            try:
                r = self.s.get(BASE + "/", timeout=30)
                if "/logout/" not in r.text:
                    lr = self.s.get(BASE + "/login/?next=/", timeout=30)
                    m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', lr.text)
                    if not m:
                        raise RuntimeError("no csrf token on login page")
                    self.s.post(BASE + "/login/", data={
                        "csrfmiddlewaretoken": m.group(1),
                        "login_view-current_step": "auth",
                        "auth-username": USER,
                        "auth-password": PASS,
                        "next": "/"}, headers={"Referer": lr.url}, timeout=30)
                    r = self.s.get(BASE + "/", timeout=30)
                if "/logout/" not in r.text:
                    raise RuntimeError("login failed (bad credentials?)")
                return
            except Exception as e:          # noqa: BLE001
                last = e
                time.sleep(5)
        raise RuntimeError("PA login failed after 3 tries: %r" % (last,))

    def console_id(self):
        ids = []
        try:
            r = self.s.get("%s/api/v0/user/%s/consoles/" % (BASE, USER), timeout=30)
            ids = sorted({str(c["id"]) for c in r.json()}, key=int)
        except Exception:                   # noqa: BLE001
            pass
        for cid in reversed(ids):
            fr = self.s.get("%s/user/%s/consoles/%s/frame/" % (BASE, USER, cid), timeout=30)
            if "LoadConsole" in fr.text:
                return cid, fr.text
        r = self.s.get("%s/user/%s/consoles/bash/new" % (BASE, USER),
                       timeout=30, allow_redirects=False)
        loc = r.headers.get("Location", "") or r.url
        m = re.search(r"consoles/(\d+)/", loc)
        if not m:
            raise RuntimeError("could not open a console: %s %s" % (r.status_code, loc))
        cid = m.group(1)
        time.sleep(3)
        fr = self.s.get("%s/user/%s/consoles/%s/frame/" % (BASE, USER, cid), timeout=30)
        return cid, fr.text

    def connect(self):
        self.cid, frame = self.console_id()
        m = re.search(r'LoadConsole\("([^"]+)", "([^"]+)", "(\d+)"', frame)
        server, key = m.group(1), m.group(2)
        sess = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(8))
        url = "wss://%s/sj/%d/%s/websocket" % (server, random.randint(0, 999), sess)
        self.ws = websocket.create_connection(
            url, timeout=25, sslopt={"cert_reqs": ssl.CERT_NONE},
            header=["Cookie: sessionid=%s" % key, "User-Agent: %s" % UA,
                    "Origin: https://%s" % server])
        assert self.ws.recv() == "o", "sockjs handshake failed"
        self._send("\x1b[%s;%s;;a" % (key, self.cid))
        try:
            self.ws.settimeout(5)
            self.ws.recv()
        except Exception:                   # noqa: BLE001
            pass
        self.ws.settimeout(None)
        self._send("\x1b[8;50;200t")
        time.sleep(2)
        self.ws.settimeout(0.5)
        try:
            while True:
                self.ws.recv()              # drain banner
        except Exception:                   # noqa: BLE001
            pass
        self.ws.settimeout(20)

    def _send(self, text):
        self.ws.send(json.dumps(text))

    def run(self, cmd, timeout=150):
        marker = "ZK" + "".join(random.choice(string.digits) for _ in range(6))
        self._send(cmd.replace("\n", "\r") + "\r")
        self._send("echo %s$?\r" % marker)
        raw = []
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                chunk = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as e:          # noqa: BLE001
                return "T", "<ws died: %r>" % (e,)
            raw.append(chunk)
            text, close = _decode("".join(raw))
            text = ANSI.sub("", text)
            if close:
                return "C", text
            if marker in text:
                tail = text.split(marker)[-1]
                m = re.match(r"(\d+)", tail.strip())
                return (m.group(1) if m else "?"), text.split(marker)[0]
        return "T", ANSI.sub("", _decode("".join(raw))[0])

    def close(self):
        try:
            self.ws.close()
        except Exception:                   # noqa: BLE001
            pass


def _decode(buf):
    msgs, close = [], None
    i = 0
    dec = json.JSONDecoder()
    while i < len(buf):
        c = buf[i]
        if c in "ho":
            i += 1
        elif c in "ma":
            try:
                val, j = dec.raw_decode(buf[i + 1:])
            except Exception:               # noqa: BLE001
                break
            if isinstance(val, list):
                msgs.extend(v if isinstance(v, str) else json.dumps(v) for v in val)
            else:
                msgs.append(val if isinstance(val, str) else json.dumps(val))
            i += 1 + j
        elif c == "c":
            close = buf[i:i + 30]
            break
        else:
            i += 1
    return "".join(msgs), close


PROBE = [
    "cd $HOME/nz 2>/dev/null || exit 9",
    "BR=$(pgrep -f 'python3 bridge.py' | wc -l)",
    "AG=$(pgrep -x nezha-agent | wc -l)",
    "PR=$(curl -s -o /dev/null -w '%{http_code}' -x http://127.0.0.1:18081 "
    "--max-time 15 https://nezhak2.btpp.ggff.net/ 2>/dev/null)",
    'echo "HK ST BR=$BR AG=$AG PR=$PR"',
]


def probe(pa, tries=3, gap=8):
    """Run the probe, retrying when the console output can't be parsed.

    A single unparsed read used to be reported as BR=0/AG=0/PR=000, which made a
    healthy restart look like a failure (false alarm 2026-10-23). Unknown != down.
    """
    last = None
    for i in range(tries):
        rc, out = pa.run("; ".join(PROBE))
        m = re.search(r"HK ST BR=(\d+) AG=(\d+) PR=(\w*)", out)
        if m:
            return {"rc": rc, "br": int(m.group(1)), "ag": int(m.group(2)),
                    "pr": m.group(3) or "000"}
        last = (rc, " ".join(out.split())[-120:])
        log("[probe] unparsed (try %d/%d) rc=%s tail=%s" % (i + 1, tries, last[0], last[1]))
        time.sleep(gap)
    return None


def main():
    pa = PA()
    pa.login()
    log("[pa] logged in as %s, console=%s" % (USER, "reused" if pa.cid else "new"))
    pa.connect()
    log("[pa] console %s attached" % pa.cid)

    st = probe(pa)
    if st is None:
        log("[pa] probe produced no readable output — treating as down")
        st = {"rc": "?", "br": 0, "ag": 0, "pr": "000"}
    log("[probe] BR=%(br)s AG=%(ag)s PR=%(pr)s" % st)

    healthy = st["br"] >= 1 and st["ag"] >= 1 and st["pr"] == "200"
    healed = False

    if healthy:
        log("[verdict] healthy — nothing to do")
        pa.close()
        if EVENT == "workflow_dispatch" and not QUIET:
            notify("✅ PythonAnywhere 哪吒探针在线\n进程 BR=1 AG=1，穿透探测 200\n%s" % REPO_URL)
        return 0

    if MODE == "status":
        log("[verdict] unhealthy (status-only mode, not touching it)")
        pa.close()
        return 1

    # stale ngrok host -> re-pin bridge.py before restarting anything
    if st["pr"] != "200" and RELAY_HOST:
        rp = ("cd $HOME/nz && sed -i 's|^RELAY = .*|RELAY = \"%s\"|' bridge.py && "
              "grep -m1 '^RELAY' bridge.py" % RELAY_HOST)
        rc, out = pa.run(rp)
        log("[repin] %s" % " ".join(out.split())[-80:])

    if not NZKEY:
        log("[fail] NZ_KEY secret not set — cannot restart the agent")
        pa.close()
        return 1

    rc, out = pa.run("NZKEY=%s bash $HOME/nz/start.sh 2>&1 | tail -4" % NZKEY)
    log("[restart] rc=%s %s" % (rc, " | ".join(out.split())[-160:]))

    # give the agent time to dial the panel through the bridge; poll instead of a
    # single 20s sample (the panel handshake is not instant on a cold start)
    st2 = None
    for i in range(6):
        time.sleep(15)
        st2 = probe(pa, tries=1)
        if st2 and st2["br"] >= 1 and st2["ag"] >= 1 and st2["pr"] == "200":
            break
        log("[reprobe] attempt %d: %s" % (i + 1, st2))
    st2 = st2 or {"br": 0, "ag": 0, "pr": "000"}
    log("[reprobe] BR=%(br)s AG=%(ag)s PR=%(pr)s" % st2)

    healthy2 = st2["br"] >= 1 and st2["ag"] >= 1 and st2["pr"] == "200"
    healed = healthy2
    pa.close()

    if healed:
        notify("♻️ PythonAnywhere 哪吒探针掉过线，已自动拉起\n之前 BR=%s AG=%s PR=%s → 现在 BR=%s AG=%s PR=%s\n%s"
               % (st["br"], st["ag"], st["pr"], st2["br"], st2["ag"], st2["pr"], REPO_URL))
        return 0

    notify("❌ PythonAnywhere 哪吒探针重启失败\nBR=%s AG=%s PR=%s（探针/链路仍不通）\n检查 ngrok 地址是否变了、VPS relay 是否在跑\n%s"
           % (st2["br"], st2["ag"], st2["pr"], REPO_URL))
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:                  # noqa: BLE001
        log("[fatal] %r" % (e,))
        notify("❌ PythonAnywhere 保活脚本异常：%s\n%s" % (repr(e)[:200], REPO_URL))
        sys.exit(1)
