#!/usr/bin/env python3
"""tvctl - control a Samsung Tizen TV over its official local-network remote API.

Standard library only. Talks to the TV's built-in endpoints:
  http://TV:8001/api/v2/                         device info (no pairing needed)
  wss://TV:8002/api/v2/channels/samsung.remote.control   key presses, app list, app launch
The first websocket connection makes the TV show an "Allow" prompt; the token it
returns is saved in ~/.samsung-tv/ so later runs don't prompt again.

Every command is logged with a timestamp to ~/.samsung-tv/session.log, and the TV's
event stream is printed only while that command runs. `tvctl.py monitor` tails it.
"""
import argparse
import base64
import datetime
import json
import os
import re
import select
import socket
import ssl
import struct
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~/.samsung-tv")
CONFIG = os.path.join(HOME, "config.json")
LOG = os.path.join(HOME, "session.log")
CLIENT_NAME = "tvctl"
YOUTUBE_APP_ID = "111299001912"
# Checked one by one when the TV won't return its full app list (common on 2021+ models).
KNOWN_APPS = {
    "111299001912": "YouTube", "3201907018807": "Netflix", "11101200001": "Netflix (legacy)",
    "3201910019365": "Prime Video", "3201512006785": "Prime Video (legacy)",
    "3201901017640": "Disney+", "3201807016597": "Apple TV", "3201606009684": "Spotify",
    "3201512006963": "Plex", "org.tizen.browser": "Internet",
}


# ---------- logging / monitor ----------

def stamp():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(kind, text):
    line = f"[{stamp()}] {kind:<5} {text}"
    print(line, flush=True)
    os.makedirs(HOME, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load_config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(HOME, exist_ok=True)
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG, 0o600)


# ---------- minimal websocket client (RFC 6455) ----------

class WebSocket:
    def __init__(self, host, port, path, use_tls):
        raw = socket.create_connection((host, port), timeout=10)
        if use_tls:
            # The TV serves a self-signed certificate on 8002; verification is
            # skipped for this one LAN connection only.
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            raw = ctx.wrap_socket(raw, server_hostname=host)
        self.sock = raw
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("TV closed the connection during handshake")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status + " ":
            raise ConnectionError(f"websocket upgrade refused: {status}")

    def _fill(self, n, deadline):
        """Make sure n bytes are buffered, giving up at a hard deadline."""
        while len(self.buf) < n:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError
            # select() enforces the deadline even where an SSL read ignores the
            # socket timeout (seen with macOS's bundled Python 3.9).
            pending = getattr(self.sock, "pending", lambda: 0)()
            if not pending and not select.select([self.sock], [], [], remaining)[0]:
                raise TimeoutError
            self.sock.settimeout(max(remaining, 0.01))
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, ssl.SSLWantReadError):
                continue
            if not chunk:
                raise ConnectionError("TV closed the connection")
            self.buf += chunk

    def _send_frame(self, opcode, payload):
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, obj):
        self._send_frame(0x1, json.dumps(obj).encode())

    def recv(self, timeout):
        """Return the next text message as a dict, or None on timeout.

        A frame is only taken off the buffer once it has fully arrived, so a
        timeout never leaves the stream half-read."""
        deadline = time.time() + timeout
        message = b""
        try:
            while True:
                self._fill(2, deadline)
                b0, b1 = self.buf[0], self.buf[1]
                opcode, n, pos = b0 & 0x0F, b1 & 0x7F, 2
                if n == 126:
                    self._fill(4, deadline)
                    n, pos = struct.unpack("!H", self.buf[2:4])[0], 4
                elif n == 127:
                    self._fill(10, deadline)
                    n, pos = struct.unpack("!Q", self.buf[2:10])[0], 10
                masked = b1 & 0x80
                total = pos + (4 if masked else 0) + n
                self._fill(total, deadline)
                frame, self.buf = self.buf[:total], self.buf[total:]
                data = frame[total - n:]
                if masked:
                    mask = frame[pos:pos + 4]
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                if opcode == 0x9:
                    self._send_frame(0xA, data)
                    continue
                if opcode == 0x8:
                    raise ConnectionError("TV closed the websocket")
                if opcode in (0x0, 0x1):
                    message += data
                    if b0 & 0x80:
                        return json.loads(message.decode(errors="replace"))
        except TimeoutError:
            return None

    def close(self):
        try:
            self._send_frame(0x8, b"")
            self.sock.close()
        except OSError:
            pass


# ---------- TV session ----------

class TV:
    def __init__(self, host, port=8002):
        self.host, self.port = host, port
        self.ws = None

    def info(self):
        url = f"http://{self.host}:8001/api/v2/"
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.load(r)

    def connect(self):
        cfg = load_config()
        token = cfg.get("tokens", {}).get(self.host, "")
        name = base64.b64encode(CLIENT_NAME.encode()).decode()
        path = f"/api/v2/channels/samsung.remote.control?name={name}"
        if token:
            path += f"&token={token}"
        log("CONN", f"wss://{self.host}:{self.port} (token {'saved' if token else 'none - look at the TV and press Allow'})")
        self.ws = WebSocket(self.host, self.port, path, use_tls=self.port != 8001)
        deadline = time.time() + 30
        while time.time() < deadline:
            msg = self.ws.recv(timeout=deadline - time.time())
            if msg is None:
                break
            self._show(msg)
            event = msg.get("event")
            if event == "ms.channel.connect":
                new = (msg.get("data") or {}).get("token")
                if new and new != token:
                    cfg.setdefault("tokens", {})[self.host] = new
                    save_config(cfg)
                    log("CONN", "TV approved this computer; token saved")
                return
            if event == "ms.channel.unauthorized":
                raise PermissionError("TV denied the connection. On the TV: Settings > General > "
                                      "External Device Manager > Device Connection Manager > Device List, "
                                      "allow 'tvctl', then retry.")
        raise TimeoutError("No answer from the TV within 30 s (was the Allow prompt accepted?)")

    def _show(self, msg):
        text = json.dumps(msg)
        log("TV", text if len(text) < 400 else text[:400] + " ...")

    def drain(self, seconds):
        """Print TV events for a short window after a command (the 'logcat' of this TV)."""
        end = time.time() + seconds
        while time.time() < end:
            try:
                msg = self.ws.recv(timeout=max(0.05, end - time.time()))
            except (ConnectionError, OSError):
                log("WARN", "TV closed the connection; reconnecting on the next command")
                self.ws = None
                return
            if msg is None:
                return
            self._show(msg)

    def _send(self, obj):
        """Send, reconnecting once if the TV dropped the connection (it does after some key presses)."""
        if self.ws is None:
            self.connect()
        try:
            self.ws.send(obj)
        except OSError:
            log("WARN", "connection lost; reconnecting")
            self.connect()
            self.ws.send(obj)

    def key(self, key, hold=None):
        key = key.upper() if key.upper().startswith("KEY_") else "KEY_" + key.upper()
        log("CMD", f"key {key}" + (f" (hold {hold}s)" if hold else ""))
        params = {"Option": "false", "TypeOfRemote": "SendRemoteKey", "DataOfCmd": key}
        if hold:
            self._send({"method": "ms.remote.control", "params": {**params, "Cmd": "Press"}})
            time.sleep(hold)
            self._send({"method": "ms.remote.control", "params": {**params, "Cmd": "Release"}})
        else:
            self._send({"method": "ms.remote.control", "params": {**params, "Cmd": "Click"}})
        self.drain(0.6)

    def emit(self, event, data=None, wait_for=None, timeout=8):
        params = {"event": event, "to": "host"}
        if data is not None:
            params["data"] = data
        self._send({"method": "ms.channel.emit", "params": params})
        end = time.time() + timeout
        while time.time() < end:
            msg = self.ws.recv(timeout=end - time.time())
            if msg is None:
                break
            if msg.get("event") == wait_for:
                return msg
            self._show(msg)
        return None

    def apps(self):
        log("CMD", "list installed apps")
        msg = self.emit("ed.installedApp.get", wait_for="ed.installedApp.get")
        if not msg:
            raise TimeoutError("TV did not return its app list")
        return (msg.get("data") or {}).get("data", [])

    def launch(self, app_id, meta=""):
        log("CMD", f"launch {app_id}" + (f" with {meta!r}" if meta else ""))
        msg = self.emit("ed.apps.launch",
                        {"appId": app_id, "action_type": "DEEP_LINK", "metaTag": meta},
                        wait_for="ed.apps.launch", timeout=3)
        if msg:
            self._show(msg)
        self.drain(1.5)
        return msg

    def dial_base(self):
        """Find the TV's DIAL server (what phones use to cast YouTube) via SSDP."""
        cfg = load_config()
        cached = cfg.get("dial", {}).get(self.host)
        if cached:
            return cached
        base = None
        msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
               "MX: 2\r\nST: urn:dial-multiscreen-org:service:dial:1\r\n\r\n").encode()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(3)
        try:
            sock.sendto(msg, ("239.255.255.250", 1900))
            sock.sendto(msg, (self.host, 1900))
            end = time.time() + 3
            while time.time() < end and not base:
                data, addr = sock.recvfrom(4096)
                if addr[0] != self.host:
                    continue
                m = re.search(rb"(?im)^location:\s*(\S+)", data)
                if m:
                    with urllib.request.urlopen(m.group(1).decode(), timeout=3) as r:
                        base = r.headers.get("Application-URL")
        except OSError as e:
            log("WARN", f"DIAL discovery: {e}")
        finally:
            sock.close()
        base = base or f"http://{self.host}:8080/ws/apps/"
        log("INFO", f"DIAL server: {base}")
        cfg.setdefault("dial", {})[self.host] = base
        save_config(cfg)
        return base

    def youtube_dial(self, video_id):
        """Open YouTube directly on a video, the way 'cast to TV' does.

        YouTube's DIAL server refuses (403) callers it doesn't recognise, so the
        request names YouTube's own clients as its origin, as phones do."""
        url = self.dial_base().rstrip("/") + "/YouTube"
        for origin in ("https://www.youtube.com", "package:com.google.android.youtube", None):
            headers = {"Content-Type": "text/plain; charset=utf-8"}
            if origin:
                headers["Origin"] = origin
            log("CMD", f"DIAL POST {url} v={video_id} (origin {origin or 'none'})")
            req = urllib.request.Request(url, data=f"v={video_id}".encode(), method="POST", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    log("INFO", f"DIAL answered {r.status}")
                    return r.status in (200, 201)
            except urllib.error.HTTPError as e:
                log("WARN", f"DIAL refused: {e.code} {e.reason}")
            except Exception as e:
                log("WARN", f"DIAL launch failed: {e}")
                cfg = load_config()
                cfg.get("dial", {}).pop(self.host, None)  # rediscover next time
                save_config(cfg)
                return False
        return False

    def youtube_screen_id(self):
        """The TV's YouTube screen ID, from its DIAL entry (starting YouTube if it isn't running)."""
        url = self.dial_base().rstrip("/") + "/YouTube"
        launched = False
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=5) as r:
                    xml = r.read().decode(errors="replace")
            except Exception as e:
                log("WARN", f"DIAL status failed: {e}")
                return None
            m = re.search(r"<screenId>([^<]+)</screenId>", xml)
            if m:
                return m.group(1)
            if not launched:
                log("INFO", "YouTube has no screen ID yet; starting it")
                self.youtube_dial("")
                launched = True
            time.sleep(1.5)
        log("WARN", "YouTube never reported a screen ID")
        return None

    def youtube_lounge_play(self, video_id):
        """Queue a video the way the YouTube phone app does when casting: pair with the
        TV's YouTube app through youtube.com's lounge service, then set its playlist."""
        screen = self.youtube_screen_id()
        if not screen:
            return False
        log("CMD", f"YouTube cast: pairing with screen {screen[:8]}...")
        try:
            data = urllib.parse.urlencode({"screen_ids": screen}).encode()
            with urllib.request.urlopen("https://www.youtube.com/api/lounge/pairing/get_lounge_token_batch",
                                        data=data, timeout=10) as r:
                token = json.load(r)["screens"][0]["loungeToken"]
            bind = "https://www.youtube.com/api/lounge/bc/bind?"
            headers = {"X-YouTube-LoungeId-Token": token}
            rid = int(time.time()) % 90000 + 10000
            params = {"RID": rid, "VER": 8, "CVER": 1}
            device = {"device": "REMOTE_CONTROL", "id": uuid.uuid4().hex, "name": "tvctl",
                      "mdx-version": 3, "pairing_type": "cast", "app": "android-phone-13.14.55"}
            req = urllib.request.Request(bind + urllib.parse.urlencode(params),
                                         data=urllib.parse.urlencode(device).encode(), headers=headers)
            with urllib.request.urlopen(req, timeout=10) as r:
                text = r.read().decode(errors="replace")
            sid = re.search(r'"c","(.*?)",', text)
            gsession = re.search(r'"S","(.*?)"\]', text)
            if not (sid and gsession):
                log("WARN", "YouTube cast: no session in the pairing reply")
                return False
            params = {"SID": sid.group(1), "gsessionid": gsession.group(1), "RID": rid + 1, "VER": 8, "CVER": 1}
            action = {"count": 1, "req0__sc": "setPlaylist", "req0_videoId": video_id,
                      "req0_currentTime": 0, "req0_currentIndex": -1, "req0_audioOnly": "false",
                      "req0_listId": ""}
            req = urllib.request.Request(bind + urllib.parse.urlencode(params),
                                         data=urllib.parse.urlencode(action).encode(), headers=headers)
            with urllib.request.urlopen(req, timeout=10) as r:
                log("INFO", f"YouTube cast: video queued ({r.status})")
                return r.status == 200
        except Exception as e:
            log("WARN", f"YouTube cast failed: {e}")
            return False

    def close_app(self, app_id):
        """Close an app so the next launch is a cold start (deep links only apply then)."""
        log("CMD", f"close {app_id}")
        req = urllib.request.Request(f"http://{self.host}:8001/api/v2/applications/{app_id}", method="DELETE")
        try:
            urllib.request.urlopen(req, timeout=5).close()
        except Exception as e:
            log("WARN", f"close failed: {e}")
            return False
        for _ in range(10):
            time.sleep(0.5)
            if not self.app_status(app_id).get("running"):
                log("INFO", "closed")
                return True
        log("WARN", "app still reports running")
        return False

    def apps_or_probe(self):
        try:
            return self.apps()
        except TimeoutError:
            log("WARN", "TV did not send its app list (normal on many 2021+ models); checking common apps one by one")
        found = []
        for app_id, name in KNOWN_APPS.items():
            log("INFO", f"checking {name}")
            st = self.app_status(app_id)
            if "error" not in st:
                found.append({"appId": app_id, "name": st.get("name") or name, "probed": True})
        log("INFO", f"found {len(found)} of {len(KNOWN_APPS)} common apps")
        return found

    def app_status(self, app_id):
        url = f"http://{self.host}:8001/api/v2/applications/{app_id}"
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                return json.load(r)
        except Exception as e:  # newer firmware may refuse this endpoint
            return {"error": str(e)}

    def close(self):
        if self.ws:
            self.ws.close()


# ---------- helpers ----------

def confirm(question, assume_yes):
    """The TV API has no screenshot, so the person watching confirms the screen."""
    if assume_yes:
        return True
    answer = input(f"\n>>> {question} [y/N] ").strip().lower()
    return answer in ("y", "yes")


def youtube_video_id(query):
    """Accept a video ID, a YouTube URL, or a title to look up on youtube.com."""
    if re.fullmatch(r"[\w-]{11}", query):
        return query, query
    parsed = urllib.parse.urlparse(query)
    if parsed.netloc.endswith("youtu.be"):
        return parsed.path.strip("/")[:11], query
    if "youtube.com" in parsed.netloc:
        v = urllib.parse.parse_qs(parsed.query).get("v")
        if v:
            return v[0], query
        m = re.search(r"/(?:shorts|live|embed)/([\w-]{11})", parsed.path)
        if m:
            return m.group(1), query
    url = "https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": query})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=10) as r:
        html = r.read().decode(errors="replace")
    m = re.search(r'"videoRenderer":\{"videoId":"([\w-]{11})"', html)
    if not m:
        raise LookupError(f"No YouTube result for {query!r}; pass the video's URL instead")
    t = re.search(r'"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"', html[m.end():])
    return m.group(1), json.loads(f'"{t.group(1)}"') if t else query


def host_arg(args):
    cfg = load_config()
    host = args.tv or cfg.get("default_tv")
    if args.tv and cfg.get("default_tv") != args.tv:
        cfg["default_tv"] = args.tv
        save_config(cfg)
    if not host:
        sys.exit("Give the TV's IP with --tv 192.168.x.y (it is remembered after the first time).")
    return host


# ---------- commands ----------

def cmd_info(args):
    tv = TV(host_arg(args))
    log("CMD", "device info")
    d = tv.info().get("device", {})
    for k in ("name", "modelName", "model", "OS", "firmwareVersion", "PowerState",
              "TokenAuthSupport", "networkType", "ip", "wifiMac", "resolution"):
        if k in d:
            print(f"  {k:<17} {d[k]}")


def cmd_pair(args):
    host = args.tv
    cfg = load_config()
    cfg["default_tv"] = host
    save_config(cfg)
    tv = TV(host)
    log("CMD", "pair")
    print(f"Model: {tv.info().get('device', {}).get('modelName', '?')}")
    tv.connect()
    tv.close()
    print("Paired. Later commands can omit --tv.")


def cmd_backup(args):
    host = host_arg(args)
    tv = TV(host)
    folder = os.path.join(os.getcwd(), "backup-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(folder)
    log("CMD", f"backup to {folder}")
    with open(os.path.join(folder, "device.json"), "w") as f:
        json.dump(tv.info(), f, indent=2)
    with open(os.path.join(folder, "settings-before.txt"), "w") as f:
        f.write(SETTINGS_TEMPLATE)
    tv.connect()
    apps = tv.apps_or_probe()
    tv.close()
    with open(os.path.join(folder, "apps.json"), "w") as f:
        json.dump(apps, f, indent=2)
    with open(os.path.join(folder, "apps.txt"), "w") as f:
        for a in sorted(apps, key=lambda a: a.get("name", "")):
            f.write(f"{a.get('appId', ''):<30} {a.get('name', '')}\n")
    print(f"Saved device.json, apps.json, apps.txt and settings-before.txt in {folder}")
    print("Fill in settings-before.txt from the TV before changing anything (see README).")


def cmd_apps(args):
    tv = TV(host_arg(args))
    tv.connect()
    for a in sorted(tv.apps_or_probe(), key=lambda a: a.get("name", "")):
        print(f"  {a.get('appId', ''):<30} {a.get('name', '')}")
    tv.close()


def cmd_key(args):
    tv = TV(host_arg(args))
    tv.connect()
    for k in args.keys:
        if k.upper() in ("ENTER", "KEY_ENTER") and not confirm(
                "About to press OK. Is the right item highlighted on the TV?", args.yes):
            log("SKIP", "OK not pressed")
            break
        tv.key(k)
        time.sleep(args.delay)
    tv.close()


def cmd_launch(args):
    tv = TV(host_arg(args))
    tv.connect()
    tv.launch(args.app_id, args.meta)
    tv.close()


def cmd_youtube(args):
    query = " ".join(args.video)
    log("CMD", f"youtube {query!r}")
    vid, title = youtube_video_id(query)
    log("INFO", f"video {vid}: {title}")
    tv = TV(host_arg(args))
    tv.connect()

    log("STEP", "1/3 Home")
    tv.key("HOME")
    time.sleep(1.5)

    def open_video(cold):
        if tv.youtube_lounge_play(vid):
            time.sleep(args.wait)
            return
        log("INFO", "trying a plain DIAL launch")
        if tv.youtube_dial(vid):
            time.sleep(args.wait)
            return
        log("INFO", "falling back to Samsung's app launch")
        # The video in a Samsung launch is only honoured when YouTube starts from closed.
        if cold and tv.app_status(YOUTUBE_APP_ID).get("running"):
            tv.close_app(YOUTUBE_APP_ID)
            time.sleep(1)
        tv.launch(YOUTUBE_APP_ID, f"v={vid}")
        time.sleep(args.wait)

    log("STEP", "2/3 open YouTube on the video")
    open_video(cold=True)
    log("INFO", f"YouTube status: {tv.app_status(YOUTUBE_APP_ID)}")

    log("STEP", "3/3 check the screen")
    if args.yes:
        log("DONE", f"sent {title}")
        tv.close()
        return
    # No screenshots on Tizen: the person watching says what is on screen, and
    # OK is only pressed when they ask for it.
    menu = ("\n>>> What is on the TV?\n"
            "    p = the video is playing        s = paused on the video (press Play)\n"
            "    w = \"Who's watching?\" screen   h = YouTube home / something else (send the video again)\n"
            "    q = quit\n>>> ")
    while True:
        ans = input(menu).strip().lower()
        if ans == "p":
            log("DONE", f"playing {title}")
            break
        if ans == "s":
            tv.key("PLAY")
        elif ans == "w":
            log("STEP", "profile picker")
            while True:
                k = input(">>> Move with l / r, press OK with o when YOUR profile is highlighted, b = back: ").strip().lower()
                if k in ("l", "r"):
                    tv.key("LEFT" if k == "l" else "RIGHT")
                elif k == "o":
                    tv.key("ENTER")
                    log("INFO", "profile chosen; YouTube should continue to the video")
                    time.sleep(4)
                    break
                elif k == "b":
                    break
        elif ans == "h":
            log("STEP", "sending the video again")
            open_video(cold=False)
        elif ans == "q":
            log("STOP", "left as is")
            break
    tv.close()


def cmd_monitor(args):
    """Tail the session log: each command with its timestamp, TV events while it runs."""
    os.makedirs(HOME, exist_ok=True)
    open(LOG, "a").close()
    print(f"Watching {LOG} (Ctrl-C to stop)")
    colors = {"CMD": "\033[1;36m", "STEP": "\033[1;33m", "DONE": "\033[1;32m",
              "WARN": "\033[1;31m", "STOP": "\033[1;31m", "TV": "\033[2m"}
    with open(LOG) as f:
        f.seek(0, os.SEEK_END)
        try:
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.2)
                    continue
                kind = line[15:20].strip()
                print(colors.get(kind, "") + line.rstrip() + "\033[0m", flush=True)
        except KeyboardInterrupt:
            pass


SETTINGS_TEMPLATE = """\
Write the CURRENT value of each setting here before changing it (photo works too).

DNS (Settings > General > Network > Network Status > IP Settings > DNS setting):
  mode: ______  (Obtain automatically / Enter manually)   server: ______
Interest-Based Advertisement: ______
Viewing Information Services: ______
Voice / other privacy choices touched: ______
Autorun Smart Hub: ______      Autorun Last App: ______
Home-bar apps removed (name, position): ______
"""


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tv", help="TV IP address (remembered after `pair`)")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before pressing OK")
    # Accept --tv/-y after the command too (`tvctl.py info --tv IP`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--tv", default=argparse.SUPPRESS, help="TV IP address")
    common.add_argument("-y", "--yes", action="store_true", default=argparse.SUPPRESS,
                        help="don't ask before pressing OK")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", parents=[common], help="model, firmware, power state (no pairing needed)").set_defaults(fn=cmd_info)
    s = sub.add_parser("pair", parents=[common], help="connect and approve this computer on the TV")
    s.add_argument("tv_ip")
    s.set_defaults(fn=lambda a: cmd_pair(argparse.Namespace(**{**vars(a), "tv": a.tv_ip})))
    sub.add_parser("backup", parents=[common], help="save device info and app list before changes").set_defaults(fn=cmd_backup)
    sub.add_parser("apps", parents=[common], help="list installed apps").set_defaults(fn=cmd_apps)
    s = sub.add_parser("key", parents=[common], help="press remote keys, e.g. key HOME RIGHT ENTER")
    s.add_argument("keys", nargs="+")
    s.add_argument("--delay", type=float, default=0.8)
    s.set_defaults(fn=cmd_key)
    s = sub.add_parser("launch", parents=[common], help="open an app by appId")
    s.add_argument("app_id")
    s.add_argument("--meta", default="")
    s.set_defaults(fn=cmd_launch)
    s = sub.add_parser("youtube", parents=[common], help="open YouTube and play a video (ID, URL or title)")
    s.add_argument("video", nargs="+")
    s.add_argument("--wait", type=float, default=8, help="seconds to let YouTube load")
    s.set_defaults(fn=cmd_youtube)
    sub.add_parser("monitor", parents=[common], help="live log of commands and TV events").set_defaults(fn=cmd_monitor)
    args = p.parse_args()
    try:
        args.fn(args)
    except KeyboardInterrupt:
        log("STOP", "cancelled with Ctrl-C")
        sys.exit(130)
    except (OSError, PermissionError, TimeoutError, LookupError, ConnectionError) as e:
        log("ERR", str(e))
        if "No route to host" in str(e) and sys.platform == "darwin":
            print("macOS: System Settings > Privacy & Security > Local Network > allow your terminal, then retry.")
        sys.exit(1)


if __name__ == "__main__":
    main()
