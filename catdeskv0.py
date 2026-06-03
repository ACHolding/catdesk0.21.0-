#!/usr/bin/env python3.14
# -*- coding: utf-8 -*-
r"""
   ____      _   ____            _
  / ___|__ _| |_|  _ \  ___  ___| | __
 | |   / _` | __| | | |/ _ \/ __| |/ /
 | |__| (_| | |_| |_| |  __/\__ \   <
  \____\__,_|\__|____/ \___||___/_|\_\   0.1

CatDesk 0.1 - a tiny single-file RustDesk-flavoured remote desktop.
FILES=OFF : no external assets, everything procedural.
Theme     : RustDesk / AnyDesk-style dark blue HUD. nya~

Cross platform: Windows / macOS / Linux / *BSD  (same one .py file)

Features (RustDesk-ish, trimmed for a 0.1):
  - ID + one-time password (HMAC challenge auth, password never sent raw)
  - Direct IP[:port] connect  (LAN / port-forwarded WAN)
  - Optional rendezvous/ID server for ID-based discovery
  - Live screen streaming (mss -> JPEG)  with FPS + quality control
  - Full remote control: mouse move/click/scroll + keyboard (pynput)
  - Resolution-independent input (normalised coords)
  - Clipboard text push
  - File send to the remote machine
  - Optional transport encryption (Fernet) when `cryptography` is present
  - Cozy Tk UI: soft spacing, rounded-ish flat panels, breathable layout

Run modes:
  python catdesk.py                 # the cozy GUI
  python catdesk.py rendezvous 21116 # run a tiny ID/signal server

Deps (install once):
  pip install mss pynput pillow
  pip install cryptography            # optional, enables encryption

> Build software people want to stay inside.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import platform
import queue
import socket
import struct
import sys
import threading
import time
import uuid

# ----------------------------------------------------------------------------
#  auto-installer  (CatSDK: low-friction — just run, it sorts itself out)
# ----------------------------------------------------------------------------
#  (import_name, pip_name, required?)
REQUIREMENTS = [
    ("mss", "mss", True),
    ("PIL", "pillow", True),
    ("pynput", "pynput", True),
    ("cryptography", "cryptography", False),  # optional, enables encryption
]


def _have(import_name):
    import importlib.util
    try:
        return importlib.util.find_spec(import_name) is not None
    except Exception:
        return False


def _pip_install(pkgs):
    """Try a few strategies so it works in plain envs, --user setups and
    PEP-668 'externally managed' systems. Returns True on success."""
    import subprocess
    base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    strategies = [
        base + pkgs,
        base + ["--user"] + pkgs,
        base + ["--break-system-packages"] + pkgs,
        base + ["--user", "--break-system-packages"] + pkgs,
    ]
    # make sure pip exists at all
    try:
        subprocess.run([sys.executable, "-m", "pip", "--version"],
                       capture_output=True)
    except Exception:
        try:
            subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"],
                           capture_output=True)
        except Exception:
            pass
    for cmd in strategies:
        try:
            print("[catdesk] $ " + " ".join(cmd[2:]))
            r = subprocess.run(cmd)
            if r.returncode == 0:
                return True
        except Exception as e:
            print("[catdesk]   (strategy failed: %s)" % e)
    return False


def _bootstrap_deps():
    """Install any missing requirements automatically, then refresh imports."""
    if os.environ.get("CATDESK_NO_INSTALL"):
        return
    missing = [(imp, pip) for imp, pip, _req in REQUIREMENTS if not _have(imp)]
    if not missing:
        return
    print("CatDesk: installing requirements -> %s"
          % ", ".join(p for _i, p in missing))
    ok = _pip_install([p for _i, p in missing])
    import importlib
    importlib.invalidate_caches()
    # make freshly --user-installed packages importable in this same run
    try:
        import site
        for d in (site.getusersitepackages(),) + tuple(site.getsitepackages()
                  if hasattr(site, "getsitepackages") else ()):
            if d and d not in sys.path:
                sys.path.insert(0, d)
    except Exception:
        pass
    importlib.invalidate_caches()
    if ok:
        still = [imp for imp, _pip, _req in REQUIREMENTS if not _have(imp)]
        if still:
            print("[catdesk] note: still missing %s — will degrade gracefully"
                  % ", ".join(still))
        else:
            print("[catdesk] all requirements ready. nya~")
    else:
        print("[catdesk] auto-install couldn't complete (offline / no pip?).")
        print("[catdesk] install manually:  pip install "
              + " ".join(p for _i, p, _r in REQUIREMENTS))


# rendezvous mode is pure-stdlib, so don't bother installing GUI libs for it
if not (len(sys.argv) >= 2 and sys.argv[1] == "rendezvous"):
    _bootstrap_deps()

# ----------------------------------------------------------------------------
#  soft dependency loading  (CatSDK: graceful degradation, friendly messages)
# ----------------------------------------------------------------------------
def _try(mod):
    try:
        return __import__(mod)
    except Exception:
        return None

_mss = _try("mss")
_pynput = _try("pynput")

_PIL_Image = None
try:
    from PIL import Image as _PIL_Image  # noqa: E402
except Exception:
    pass

_PIL_ImageTk = None


def _get_imagetk():
    """PIL.ImageTk must be imported explicitly; load once tkinter is available."""
    global _PIL_ImageTk
    if _PIL_ImageTk is None and _PIL_Image is not None:
        try:
            from PIL import ImageTk
            _PIL_ImageTk = ImageTk
        except Exception:
            pass
    return _PIL_ImageTk

try:
    from cryptography.fernet import Fernet  # type: ignore
    HAVE_CRYPTO = True
except Exception:
    Fernet = None  # type: ignore
    HAVE_CRYPTO = False

# ----------------------------------------------------------------------------
#  theme  (RustDesk dark + AnyDesk-style clean panels, blue accent)
# ----------------------------------------------------------------------------
BG       = "#18191E"   # scaffold / window bg
PANEL    = "#24252B"   # card panel
PANEL2   = "#1C1D23"   # inset fields / log area
BORDER   = "#3F4048"   # panel edge / divider
HUE      = "#0071FF"   # primary accent (RustDesk blue)
HUE_DIM  = "#AAAAAA"   # labels / hints
HUE_HOT  = "#2C8CFF"   # hover / secondary blue
ID_C     = "#00B6F0"   # your ID / password highlight
INK      = "#E5E5E5"   # body text
GHOST    = "#888888"   # timestamps / faint
TEXT_ON  = "#FFFFFF"   # text on primary buttons
HOVER    = "#3F3F3F"   # neutral button hover
CANVAS   = "#212121"   # remote view canvas
OK_C     = "#32BEA6"
WARN_C   = "#FFCC44"
ERR_C    = "#E04F5F"

FONT     = ("Segoe UI" if platform.system() == "Windows"
            else "Helvetica Neue" if platform.system() == "Darwin"
            else "Cantarell")
FN       = (FONT, 11)
FN_SM    = (FONT, 9)
FN_BIG   = (FONT, 26, "bold")
FN_H     = (FONT, 13, "bold")

# ----------------------------------------------------------------------------
#  protocol  (length-prefixed JSON header + optional binary payload)
# ----------------------------------------------------------------------------
PROTO_VER   = 1
DEFAULT_PORT = 21118
RDV_PORT     = 21116

# message types
HELLO, CHALLENGE, AUTH, AUTHOK, AUTHFAIL = "hello", "chal", "auth", "ok", "no"
FRAME, INPUT, CLIP, PING, PONG, BYE      = "frame", "in", "clip", "ping", "pong", "bye"
FILE_BEG, FILE_CHUNK, FILE_END           = "fbeg", "fchunk", "fend"


class Peer:
    """A socket wrapped with a send-lock + optional cipher. Soft + recoverable."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.lock = threading.Lock()
        self.cipher = None  # set after auth if both ends support encryption
        self.alive = True

    # ---- send -----------------------------------------------------------
    def send(self, mtype, header=None, payload=b""):
        header = dict(header or {})
        header["t"] = mtype
        if self.cipher is not None and payload:
            payload = self.cipher.encrypt(payload)
            header["e"] = 1
        header["n"] = len(payload)
        hj = json.dumps(header, separators=(",", ":")).encode("utf-8")
        frame = struct.pack(">I", len(hj)) + hj + payload
        with self.lock:
            self.sock.sendall(frame)

    # ---- receive --------------------------------------------------------
    def _exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(min(65536, n - len(buf)))
            if not chunk:
                raise ConnectionError("peer closed")
            buf += chunk
        return bytes(buf)

    def recv(self):
        (hl,) = struct.unpack(">I", self._exact(4))
        header = json.loads(self._exact(hl).decode("utf-8"))
        n = header.get("n", 0)
        payload = self._exact(n) if n else b""
        if header.get("e") and self.cipher is not None:
            payload = self.cipher.decrypt(payload)
        return header, payload

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except Exception:
            pass


def make_cipher(password: str):
    if not HAVE_CRYPTO:
        return None
    key = base64.urlsafe_b64encode(
        hashlib.sha256(("catdesk:" + password).encode("utf-8")).digest()
    )
    return Fernet(key)


def hmac_resp(password: str, nonce: bytes) -> str:
    return hmac.new(password.encode("utf-8"), nonce, hashlib.sha256).hexdigest()


def stable_id() -> str:
    """9-digit ID derived from the machine, stable across launches."""
    node = uuid.getnode()
    h = hashlib.sha256(str(node).encode()).hexdigest()
    return str(int(h[:12], 16) % 1_000_000_000).zfill(9)


def random_password(n=6) -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    raw = os.urandom(n)
    return "".join(alphabet[b % len(alphabet)] for b in raw)


# ----------------------------------------------------------------------------
#  keyboard mapping  (Tk keysym -> pynput)
# ----------------------------------------------------------------------------
_SPECIAL = {
    "Return": "enter", "BackSpace": "backspace", "Tab": "tab", "Escape": "esc",
    "space": "space", "Left": "left", "Right": "right", "Up": "up", "Down": "down",
    "Delete": "delete", "Home": "home", "End": "end", "Prior": "page_up",
    "Next": "page_down", "Insert": "insert", "Caps_Lock": "caps_lock",
    "Print": "print_screen", "Pause": "pause", "Scroll_Lock": "scroll_lock",
    "Num_Lock": "num_lock", "Menu": "menu",
    "Shift_L": "shift", "Shift_R": "shift_r",
    "Control_L": "ctrl", "Control_R": "ctrl_r",
    "Alt_L": "alt", "Alt_R": "alt_gr",
    "Super_L": "cmd", "Super_R": "cmd_r",
    "Meta_L": "cmd", "Meta_R": "cmd_r",
}
for _i in range(1, 13):
    _SPECIAL["F%d" % _i] = "f%d" % _i


# ============================================================================
#  HOST  (this machine shares its screen + accepts control)
# ============================================================================
class HostServer:
    def __init__(self, app):
        self.app = app
        self.thread = None
        self.listen_sock = None
        self.running = False
        self.sessions = []  # active HostSession
        self.password = random_password()
        self.allow_control = True
        self.fps = 12
        self.quality = 55

    def log(self, msg, color=INK):
        self.app.log("[host] " + msg, color)

    def start(self, port):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._serve, args=(port,), daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        try:
            if self.listen_sock:
                self.listen_sock.close()
        except Exception:
            pass
        for s in list(self.sessions):
            s.stop()

    def _serve(self, port):
        try:
            self.listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listen_sock.bind(("0.0.0.0", port))
            self.listen_sock.listen(4)
            self.log("listening on 0.0.0.0:%d  (ID %s)" % (port, self.app.my_id), HUE)
        except Exception as e:
            self.log("cannot listen: %s" % e, ERR_C)
            self.running = False
            return

        while self.running:
            try:
                client, addr = self.listen_sock.accept()
            except OSError:
                break
            self.log("incoming connection from %s:%d" % addr, HUE_HOT)
            sess = HostSession(self, client, addr)
            self.sessions.append(sess)
            sess.start()


class HostSession:
    def __init__(self, server: HostServer, sock, addr):
        self.server = server
        self.peer = Peer(sock)
        self.addr = addr
        self.alive = True
        self.cap_thread = None
        self.recv_thread = None
        self._mouse = None
        self._kbd = None
        self.scr_w = self.scr_h = 1

    def log(self, m, c=INK):
        self.server.log(m, c)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.alive = False
        self.peer.close()
        if self in self.server.sessions:
            try:
                self.server.sessions.remove(self)
            except ValueError:
                pass

    # -- auth + setup ------------------------------------------------------
    def _run(self):
        try:
            header, _ = self.peer.recv()
            if header.get("t") != HELLO:
                self.stop(); return
            peer_enc = bool(header.get("enc"))
            nonce = os.urandom(24)
            self.peer.send(CHALLENGE, {"nonce": nonce.hex(), "enc": HAVE_CRYPTO})

            header, _ = self.peer.recv()
            if header.get("t") != AUTH:
                self.stop(); return
            expect = hmac_resp(self.server.password, nonce)
            if not hmac.compare_digest(expect, header.get("resp", "")):
                self.peer.send(AUTHFAIL, {"why": "bad password"})
                self.log("auth FAILED from %s:%d" % self.addr, ERR_C)
                self.stop(); return

            # discover screen size
            with _mss.mss() as sct:
                mon = sct.monitors[1]
                self.scr_w, self.scr_h = mon["width"], mon["height"]

            enc_on = HAVE_CRYPTO and peer_enc
            self.peer.send(AUTHOK, {
                "w": self.scr_w, "h": self.scr_h,
                "enc": enc_on,
                "control": self.server.allow_control,
                "host": socket.gethostname(),
            })
            if enc_on:
                self.peer.cipher = make_cipher(self.server.password)
            self.log("authorised %s:%d  enc=%s" % (self.addr[0], self.addr[1], enc_on),
                     OK_C)

            if self.server.allow_control and _pynput:
                self._mouse = _pynput.mouse.Controller()
                self._kbd = _pynput.keyboard.Controller()

            self.cap_thread = threading.Thread(target=self._capture_loop, daemon=True)
            self.cap_thread.start()
            self._recv_loop()
        except Exception as e:
            self.log("session ended: %s" % e, GHOST)
        finally:
            self.stop()

    # -- screen capture ----------------------------------------------------
    def _capture_loop(self):
        Image = _PIL_Image
        try:
            with _mss.mss() as sct:
                mon = sct.monitors[1]
                while self.alive and self.peer.alive:
                    t0 = time.time()
                    shot = sct.grab(mon)
                    img = Image.frombytes("RGB", shot.size, shot.rgb)
                    # gentle downscale for big screens (breathable bandwidth)
                    maxw = 1440
                    if img.width > maxw:
                        ratio = maxw / img.width
                        img = img.resize((maxw, int(img.height * ratio)),
                                         Image.BILINEAR)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG",
                             quality=int(self.server.quality), optimize=False)
                    self.peer.send(FRAME, {"w": img.width, "h": img.height,
                                           "sw": self.scr_w, "sh": self.scr_h},
                                   buf.getvalue())
                    # frame pacing
                    dt = time.time() - t0
                    target = 1.0 / max(1, self.server.fps)
                    if dt < target:
                        time.sleep(target - dt)
        except Exception as e:
            self.log("capture stopped: %s" % e, GHOST)

    # -- inbound: input / clipboard / files --------------------------------
    def _recv_loop(self):
        recv_file = None
        while self.alive and self.peer.alive:
            header, payload = self.peer.recv()
            t = header.get("t")
            if t == INPUT:
                self._apply_input(header)
            elif t == PING:
                self.peer.send(PONG)
            elif t == CLIP:
                self.server.app.set_clipboard(payload.decode("utf-8", "ignore"))
            elif t == FILE_BEG:
                recv_file = self._begin_file(header)
            elif t == FILE_CHUNK and recv_file:
                recv_file.write(payload)
            elif t == FILE_END and recv_file:
                recv_file.close()
                self.log("received file -> %s" % getattr(recv_file, "name", "?"), OK_C)
                recv_file = None
            elif t == BYE:
                break

    def _begin_file(self, header):
        folder = os.path.join(os.path.expanduser("~"), "CatDesk_Received")
        os.makedirs(folder, exist_ok=True)
        name = os.path.basename(header.get("name", "file.bin")) or "file.bin"
        return open(os.path.join(folder, name), "wb")

    def _apply_input(self, h):
        if not (self.server.allow_control and self._mouse and self._kbd):
            return
        kind = h.get("k")
        try:
            if kind == "move":
                self._mouse.position = (h["x"] * self.scr_w, h["y"] * self.scr_h)
            elif kind in ("down", "up"):
                self._mouse.position = (h["x"] * self.scr_w, h["y"] * self.scr_h)
                btn = {"l": _pynput.mouse.Button.left,
                       "r": _pynput.mouse.Button.right,
                       "m": _pynput.mouse.Button.middle}.get(h.get("b"),
                                                             _pynput.mouse.Button.left)
                if kind == "down":
                    self._mouse.press(btn)
                else:
                    self._mouse.release(btn)
            elif kind == "scroll":
                self._mouse.scroll(0, h.get("d", 0))
            elif kind in ("kdown", "kup"):
                self._press_key(h, down=(kind == "kdown"))
        except Exception:
            pass

    def _press_key(self, h, down):
        Key = _pynput.keyboard.Key
        KeyCode = _pynput.keyboard.KeyCode
        keysym = h.get("sym", "")
        char = h.get("ch", "")
        key = None
        if keysym in _SPECIAL:
            key = getattr(Key, _SPECIAL[keysym], None)
        if key is None and char and char.isprintable() and len(char) == 1:
            key = KeyCode.from_char(char)
        if key is None and len(keysym) == 1:
            key = KeyCode.from_char(keysym)
        if key is None:
            return
        if down:
            self._kbd.press(key)
        else:
            self._kbd.release(key)


# ============================================================================
#  CLIENT  (this machine views + controls a remote)
# ============================================================================
class ClientConnection:
    def __init__(self, app, host, port, password, on_open, on_frame, on_close):
        self.app = app
        self.host = host
        self.port = port
        self.password = password
        self.on_open = on_open
        self.on_frame = on_frame
        self.on_close = on_close
        self.peer = None
        self.alive = False

    def log(self, m, c=INK):
        self.app.log("[view] " + m, c)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            sock = socket.create_connection((self.host, self.port), timeout=8)
            self.peer = Peer(sock)
            self.alive = True
            self.peer.send(HELLO, {"ver": PROTO_VER, "enc": HAVE_CRYPTO})

            header, _ = self.peer.recv()
            if header.get("t") != CHALLENGE:
                self.log("handshake refused", ERR_C); self._end(); return
            nonce = bytes.fromhex(header["nonce"])
            self.peer.send(AUTH, {"resp": hmac_resp(self.password, nonce)})

            header, _ = self.peer.recv()
            if header.get("t") == AUTHFAIL:
                self.log("auth failed: %s" % header.get("why", ""), ERR_C)
                self._end(); return
            if header.get("t") != AUTHOK:
                self.log("unexpected reply", ERR_C); self._end(); return

            if header.get("enc"):
                self.peer.cipher = make_cipher(self.password)
            self.log("connected to %s  %dx%d  enc=%s  control=%s" % (
                header.get("host", self.host), header.get("w", 0),
                header.get("h", 0), header.get("enc"), header.get("control")), OK_C)
            self.on_open(header)
            self._recv_loop()
        except Exception as e:
            self.log("connection error: %s" % e, ERR_C)
        finally:
            self._end()

    def _recv_loop(self):
        Image = _PIL_Image
        while self.alive and self.peer.alive:
            header, payload = self.peer.recv()
            t = header.get("t")
            if t == FRAME:
                try:
                    img = Image.open(io.BytesIO(payload)).convert("RGB")
                    self.on_frame(img)
                except Exception:
                    pass
            elif t == PONG:
                pass
            elif t == BYE:
                break

    # -- outbound helpers --------------------------------------------------
    def send_input(self, header):
        if self.alive and self.peer:
            try:
                self.peer.send(INPUT, header)
            except Exception:
                self._end()

    def send_clip(self, text):
        if self.alive and self.peer:
            try:
                self.peer.send(CLIP, {}, text.encode("utf-8"))
            except Exception:
                pass

    def send_file(self, path):
        def worker():
            try:
                size = os.path.getsize(path)
                self.peer.send(FILE_BEG, {"name": os.path.basename(path), "size": size})
                with open(path, "rb") as f:
                    while True:
                        chunk = f.read(64 * 1024)
                        if not chunk:
                            break
                        self.peer.send(FILE_CHUNK, {}, chunk)
                self.peer.send(FILE_END, {})
                self.log("sent %s (%d bytes)" % (os.path.basename(path), size), OK_C)
            except Exception as e:
                self.log("file send failed: %s" % e, ERR_C)
        threading.Thread(target=worker, daemon=True).start()

    def _end(self):
        if self.alive:
            self.alive = False
            self.on_close()
            if self.peer:
                self.peer.close()


# ============================================================================
#  RENDEZVOUS  (optional tiny ID -> address server)
# ============================================================================
class RendezvousServer:
    """Minimal signalling: REGISTER {id,port} / LOOKUP {id} -> {ip,port}."""

    def __init__(self):
        self.table = {}   # id -> (ip, port, ts)
        self.lock = threading.Lock()

    def serve(self, port=RDV_PORT):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(16)
        print("[rendezvous] listening on 0.0.0.0:%d" % port)
        while True:
            c, addr = srv.accept()
            threading.Thread(target=self._handle, args=(c, addr), daemon=True).start()

    def _handle(self, c, addr):
        peer = Peer(c)
        try:
            header, _ = peer.recv()
            t = header.get("t")
            if t == "register":
                with self.lock:
                    self.table[header["id"]] = (addr[0], header.get("port",
                                                 DEFAULT_PORT), time.time())
                peer.send("reg_ok", {})
                print("[rendezvous] register %s -> %s:%s" %
                      (header["id"], addr[0], header.get("port")))
            elif t == "lookup":
                with self.lock:
                    rec = self.table.get(header["id"])
                if rec:
                    peer.send("found", {"ip": rec[0], "port": rec[1]})
                else:
                    peer.send("missing", {})
        except Exception:
            pass
        finally:
            peer.close()


def rdv_register(rdv_host, rdv_port, my_id, listen_port):
    try:
        c = socket.create_connection((rdv_host, rdv_port), timeout=5)
        p = Peer(c)
        p.send("register", {"id": my_id, "port": listen_port})
        p.recv()
        p.close()
        return True
    except Exception:
        return False


def rdv_lookup(rdv_host, rdv_port, target_id):
    c = socket.create_connection((rdv_host, rdv_port), timeout=5)
    p = Peer(c)
    p.send("lookup", {"id": target_id})
    header, _ = p.recv()
    p.close()
    if header.get("t") == "found":
        return header["ip"], header["port"]
    return None


# ============================================================================
#  procedural calico cat mascot  (FILES=OFF — eyes on sprite, no eye icon)
# ============================================================================
def _calico_cat_image(size=36):
    """Tiny pixel calico: white + orange + black patches, with cat eyes."""
    if _PIL_Image is None:
        return None
    Image = _PIL_Image
    rows = [
        "..OO....OO....",
        ".OBBKOBBKO....",
        ".OWWWWWWWWWWO..",
        ".OWWWYEWWYEWWWO",
        "..OWWWPPWWWO...",
        "..OWWWWWW.....",
        "OBBWWWWWWBBO..",
        "OBBWWWWWWWWBBO",
        "OBWWWWWWWWBO..",
        ".OWWW..WWW....",
    ]
    cols = {
        "W": (245, 240, 232, 255), "O": (232, 131, 58, 255),
        "B": (26, 26, 26, 255), "K": (42, 24, 16, 255),
        "P": (255, 184, 192, 255), "Y": (210, 175, 55, 255),
        "E": (20, 20, 20, 255), ".": None,
    }
    h = len(rows)
    w = max(len(r) for r in rows)
    rows = [(r + "." * w)[:w] for r in rows]
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = img.load()
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            c = cols.get(ch)
            if c:
                px[x, y] = c
    return img.resize((size, int(size * h / w)), Image.NEAREST)


# ============================================================================
#  GUI
# ============================================================================
def run_gui():
    import tkinter as tk
    from tkinter import filedialog

    # ---- styled widget helpers (cozy / soft / readable) -----------------
    def panel(parent, **kw):
        f = tk.Frame(parent, bg=PANEL, highlightbackground=BORDER,
                     highlightthickness=1, **kw)
        return f

    def label(parent, text, font=FN, fg=INK, **kw):
        return tk.Label(parent, text=text, bg=kw.pop("bg", PANEL), fg=fg,
                        font=font, **kw)

    def soft_button(parent, text, cmd, accent=False):
        fg = TEXT_ON if accent else INK
        bg = HUE if accent else PANEL
        hover_bg = HUE_HOT if accent else HOVER
        hover_fg = TEXT_ON
        b = tk.Button(parent, text=text, command=cmd, font=FN, fg=fg, bg=bg,
                      activebackground=hover_bg, activeforeground=hover_fg,
                      relief="flat", bd=0, padx=14, pady=7, cursor="hand2",
                      highlightbackground=BORDER, highlightthickness=1)
        def _enter(_):
            b.config(bg=hover_bg, fg=hover_fg)
        def _leave(_):
            b.config(bg=bg, fg=fg)
        b.bind("<Enter>", _enter)
        b.bind("<Leave>", _leave)
        return b

    def soft_entry(parent, var=None, width=20, show=None):
        return tk.Entry(parent, textvariable=var, width=width, show=show,
                        font=FN, fg=INK, bg=PANEL2, insertbackground=ID_C,
                        relief="flat", bd=0, highlightbackground=BORDER,
                        highlightcolor=HUE, highlightthickness=1)

    # ---- app state -------------------------------------------------------
    class App:
        def __init__(self, root):
            self.root = root
            self.my_id = stable_id()
            self.host = HostServer(self)
            self.ui_q = queue.Queue()
            self.viewer = None
            self._build()
            self.root.after(50, self._pump)
            # start sharing immediately, RustDesk style
            self.host.start(self.listen_port.get())

        # -------- logging / clipboard (thread-safe via queue) ------------
        def log(self, msg, color=INK):
            self.ui_q.put(("log", msg, color))

        def set_clipboard(self, text):
            self.ui_q.put(("clip", text, None))

        def _pump(self):
            try:
                while True:
                    kind, a, b = self.ui_q.get_nowait()
                    if kind == "log":
                        self._write_log(a, b)
                    elif kind == "clip":
                        try:
                            self.root.clipboard_clear()
                            self.root.clipboard_append(a)
                        except Exception:
                            pass
            except queue.Empty:
                pass
            self.root.after(50, self._pump)

        def _write_log(self, msg, color):
            ts = time.strftime("%H:%M:%S")
            self.logbox.config(state="normal")
            self.logbox.insert("end", "%s  " % ts, ("dim",))
            self.logbox.insert("end", msg + "\n", (color,))
            self.logbox.tag_config("dim", foreground=GHOST)
            for c in (INK, HUE, HUE_HOT, ID_C, OK_C, WARN_C, ERR_C, GHOST):
                self.logbox.tag_config(c, foreground=c)
            self.logbox.see("end")
            self.logbox.config(state="disabled")

        # -------- layout -------------------------------------------------
        def _build(self):
            r = self.root
            r.title("CatDesk 0.1")
            r.configure(bg=BG)
            r.geometry("780x560")
            r.minsize(700, 520)

            # header
            head = tk.Frame(r, bg=BG)
            head.pack(fill="x", padx=18, pady=(16, 8))
            ImageTk = _get_imagetk()
            cat_img = _calico_cat_image(40)
            if ImageTk is not None and cat_img is not None:
                self._cat_icon = ImageTk.PhotoImage(cat_img)
                title = tk.Frame(head, bg=BG)
                title.pack(side="left")
                tk.Label(title, image=self._cat_icon, bg=BG).pack(side="left", padx=(0, 8))
                label(title, "CatDesk", font=FN_BIG, fg=HUE, bg=BG).pack(side="left")
            else:
                label(head, "CatDesk", font=FN_BIG, fg=HUE, bg=BG).pack(side="left")
            label(head, "  0.1 · remote desktop", font=FN_SM,
                  fg=HUE_DIM, bg=BG).pack(side="left", anchor="s", pady=(0, 6))
            enc_txt = "🔒 encryption ready" if HAVE_CRYPTO else "🔓 plaintext (pip install cryptography)"
            label(head, enc_txt, font=FN_SM, fg=(OK_C if HAVE_CRYPTO else WARN_C),
                  bg=BG).pack(side="right", pady=(0, 6))

            body = tk.Frame(r, bg=BG)
            body.pack(fill="both", expand=True, padx=18, pady=4)
            body.columnconfigure(0, weight=1, uniform="col")
            body.columnconfigure(1, weight=1, uniform="col")

            self._build_host_panel(body)
            self._build_remote_panel(body)
            self._build_log(r)

        def _build_host_panel(self, parent):
            p = panel(parent)
            p.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=4)
            pad = {"padx": 16}
            label(p, "THIS DESKTOP", font=FN_H, fg=HUE).pack(anchor="w", pady=(14, 2), **pad)
            label(p, "share this machine — give the ID + password", font=FN_SM,
                  fg=GHOST).pack(anchor="w", **pad)

            label(p, "Your ID", font=FN_SM, fg=HUE_DIM).pack(anchor="w", pady=(14, 0), **pad)
            label(p, self.my_id, font=(FONT, 22, "bold"), fg=ID_C).pack(anchor="w", **pad)

            label(p, "One-time password", font=FN_SM, fg=HUE_DIM).pack(anchor="w",
                                                                       pady=(10, 0), **pad)
            prow = tk.Frame(p, bg=PANEL)
            prow.pack(anchor="w", fill="x", **pad)
            self.pw_var = tk.StringVar(value=self.host.password)
            self.pw_lbl = label(prow, self.host.password, font=(FONT, 18, "bold"),
                                fg=ID_C)
            self.pw_lbl.pack(side="left")
            soft_button(prow, "🔄", self._refresh_pw).pack(side="left", padx=(10, 4))

            # controls
            self.allow_var = tk.BooleanVar(value=True)
            tk.Checkbutton(p, text="Allow remote control", variable=self.allow_var,
                           command=self._toggle_control, font=FN, fg=INK, bg=PANEL,
                           activebackground=PANEL, activeforeground=HUE,
                           selectcolor=PANEL2, bd=0, highlightthickness=0,
                           cursor="hand2").pack(anchor="w", pady=(12, 0), **pad)

            qrow = tk.Frame(p, bg=PANEL)
            qrow.pack(anchor="w", fill="x", pady=(8, 0), **pad)
            label(qrow, "FPS", font=FN_SM, fg=HUE_DIM).pack(side="left")
            self.fps_var = tk.IntVar(value=self.host.fps)
            tk.Spinbox(qrow, from_=1, to=30, width=4, textvariable=self.fps_var,
                       command=self._apply_qual, font=FN_SM, fg=INK, bg=PANEL2,
                       buttonbackground=PANEL2, relief="flat",
                       highlightbackground=BORDER).pack(side="left", padx=(6, 16))
            label(qrow, "Quality", font=FN_SM, fg=HUE_DIM).pack(side="left")
            self.q_var = tk.IntVar(value=self.host.quality)
            tk.Spinbox(qrow, from_=15, to=90, width=4, textvariable=self.q_var,
                       command=self._apply_qual, font=FN_SM, fg=INK, bg=PANEL2,
                       buttonbackground=PANEL2, relief="flat",
                       highlightbackground=BORDER).pack(side="left", padx=(6, 0))

            label(p, "Listen port", font=FN_SM, fg=HUE_DIM).pack(anchor="w",
                                                                 pady=(10, 0), **pad)
            self.listen_port = tk.IntVar(value=DEFAULT_PORT)
            soft_entry(p, self.listen_port, width=8).pack(anchor="w", **pad)

            srow = tk.Frame(p, bg=PANEL)
            srow.pack(anchor="w", fill="x", pady=(12, 16), **pad)
            self.share_btn = soft_button(srow, "■ Stop sharing", self._toggle_share)
            self.share_btn.pack(side="left")
            self.share_state = label(srow, "● sharing", fg=OK_C)
            self.share_state.pack(side="left", padx=12)

        def _build_remote_panel(self, parent):
            p = panel(parent)
            p.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=4)
            pad = {"padx": 16}
            label(p, "REMOTE DESKTOP", font=FN_H, fg=HUE).pack(anchor="w",
                                                               pady=(14, 2), **pad)
            label(p, "connect to another machine by ID or IP", font=FN_SM,
                  fg=GHOST).pack(anchor="w", **pad)

            label(p, "Remote ID  or  IP[:port]", font=FN_SM, fg=HUE_DIM).pack(
                anchor="w", pady=(14, 0), **pad)
            self.target_var = tk.StringVar()
            soft_entry(p, self.target_var, width=24).pack(anchor="w", fill="x",
                                                          pady=(2, 0), **pad)

            label(p, "Password", font=FN_SM, fg=HUE_DIM).pack(anchor="w",
                                                              pady=(10, 0), **pad)
            self.rpw_var = tk.StringVar()
            soft_entry(p, self.rpw_var, width=24, show="•").pack(anchor="w",
                                                                 fill="x", pady=(2, 0), **pad)

            label(p, "Rendezvous server (optional)", font=FN_SM,
                  fg=HUE_DIM).pack(anchor="w", pady=(10, 0), **pad)
            self.rdv_var = tk.StringVar()
            soft_entry(p, self.rdv_var, width=24).pack(anchor="w", fill="x",
                                                       pady=(2, 0), **pad)
            label(p, "host:21116 — leave blank for direct IP", font=FN_SM,
                  fg=GHOST).pack(anchor="w", **pad)

            soft_button(p, "→  Connect", self._connect, accent=True).pack(
                anchor="w", pady=(16, 6), **pad)

            label(p, "tip: same Wi-Fi → type the host's LAN IP\n"
                     "over internet → forward the listen port",
                  font=FN_SM, fg=GHOST, justify="left").pack(anchor="w",
                                                             pady=(6, 16), **pad)

        def _build_log(self, r):
            import tkinter as tk
            wrap = panel(r)
            wrap.pack(fill="both", expand=False, padx=18, pady=(6, 16))
            label(wrap, "ACTIVITY", font=FN_SM, fg=HUE_DIM).pack(anchor="w",
                                                                 padx=12, pady=(8, 2))
            self.logbox = tk.Text(wrap, height=7, bg=PANEL2, fg=INK, font=FN_SM,
                                  relief="flat", bd=0, highlightthickness=0,
                                  wrap="word", state="disabled", padx=10, pady=6)
            self.logbox.pack(fill="both", expand=True, padx=10, pady=(0, 10))
            self.log("CatDesk 0.1 ready. nya~", HUE)
            self.log("ID %s · password %s" % (self.my_id, self.host.password), HUE_DIM)

        # -------- host controls ------------------------------------------
        def _refresh_pw(self):
            self.host.password = random_password()
            self.pw_lbl.config(text=self.host.password)
            self.log("password rotated -> %s" % self.host.password, HUE)

        def _toggle_control(self):
            self.host.allow_control = self.allow_var.get()
            self.log("remote control %s" %
                     ("enabled" if self.host.allow_control else "view-only"), HUE)

        def _apply_qual(self):
            try:
                self.host.fps = int(self.fps_var.get())
                self.host.quality = int(self.q_var.get())
            except Exception:
                pass

        def _toggle_share(self):
            if self.host.running:
                self.host.stop()
                self.share_btn.config(text="▶ Start sharing")
                self.share_state.config(text="○ stopped", fg=GHOST)
                self.log("sharing stopped", WARN_C)
            else:
                # rejoin rendezvous if set
                self.host.start(self.listen_port.get())
                self._maybe_register()
                self.share_btn.config(text="■ Stop sharing")
                self.share_state.config(text="● sharing", fg=OK_C)

        def _maybe_register(self):
            rdv = self.rdv_var.get().strip()
            if rdv:
                host, _, port = rdv.partition(":")
                port = int(port or RDV_PORT)
                ok = rdv_register(host, port, self.my_id, self.listen_port.get())
                self.log("rendezvous register %s" %
                         ("ok" if ok else "failed"), OK_C if ok else ERR_C)

        # -------- connect as client --------------------------------------
        def _connect(self):
            target = self.target_var.get().strip()
            pw = self.rpw_var.get()
            if not target or not pw:
                self.log("need a target and a password", WARN_C)
                return

            host, port = self._resolve_target(target)
            if host is None:
                return

            if self.viewer and self.viewer.alive:
                self.log("a session is already open", WARN_C)
                return

            self.log("connecting to %s:%d ..." % (host, port), HUE)
            self.viewer = ViewerWindow(self, host, port, pw)

        def _resolve_target(self, target):
            rdv = self.rdv_var.get().strip()
            # looks like an IP/host:port -> direct
            if (":" in target and not rdv) or any(ch in target for ch in "."):
                host, _, p = target.partition(":")
                return host, int(p or DEFAULT_PORT)
            if target.isdigit() and rdv:
                rh, _, rp = rdv.partition(":")
                try:
                    res = rdv_lookup(rh, int(rp or RDV_PORT), target)
                except Exception as e:
                    self.log("rendezvous lookup error: %s" % e, ERR_C)
                    return None, None
                if not res:
                    self.log("ID %s not found on rendezvous" % target, ERR_C)
                    return None, None
                self.log("rendezvous resolved %s -> %s:%s" % (target, *res), OK_C)
                return res[0], int(res[1])
            # fallback: treat as hostname
            host, _, p = target.partition(":")
            return host, int(p or DEFAULT_PORT)

    # ---- the remote-view window -----------------------------------------
    class ViewerWindow:
        def __init__(self, app, host, port, password):
            self.app = app
            self.alive = True
            self.remote_w = 1
            self.remote_h = 1
            self.control = False
            self.frame_q = queue.Queue(maxsize=2)
            self._photo = None
            self._disp = (0, 0, 1, 1)  # ox, oy, w, h of drawn image
            self._pressed_buttons = set()

            self.win = tk.Toplevel(app.root, bg=BG)
            self.win.title("CatDesk · remote view")
            self.win.geometry("960x600")
            self.win.configure(bg=BG)

            bar = tk.Frame(self.win, bg=PANEL, highlightbackground=BORDER,
                           highlightthickness=1)
            bar.pack(fill="x")
            self.status = tk.Label(bar, text="connecting…", bg=PANEL, fg=HUE,
                                   font=FN_SM)
            self.status.pack(side="left", padx=10, pady=6)
            soft_button(bar, "⎘ send clipboard", self._send_clip).pack(side="right",
                                                                       padx=4, pady=4)
            soft_button(bar, "📁 send file", self._send_file).pack(side="right",
                                                                   padx=4, pady=4)

            self.canvas = tk.Canvas(self.win, bg=CANVAS, highlightthickness=0,
                                    cursor="tcross")
            self.canvas.pack(fill="both", expand=True)
            self.canvas.create_text(480, 300, text="waiting for first frame…",
                                    fill=HUE_DIM, font=FN, tags="hint")

            # input bindings
            c = self.canvas
            c.bind("<Motion>", self._on_move)
            c.bind("<ButtonPress-1>", lambda e: self._on_btn(e, "down", "l"))
            c.bind("<ButtonRelease-1>", lambda e: self._on_btn(e, "up", "l"))
            c.bind("<ButtonPress-3>", lambda e: self._on_btn(e, "down", "r"))
            c.bind("<ButtonRelease-3>", lambda e: self._on_btn(e, "up", "r"))
            c.bind("<ButtonPress-2>", lambda e: self._on_btn(e, "down", "m"))
            c.bind("<ButtonRelease-2>", lambda e: self._on_btn(e, "up", "m"))
            c.bind("<MouseWheel>", self._on_wheel)
            c.bind("<Button-4>", lambda e: self._wheel(1))
            c.bind("<Button-5>", lambda e: self._wheel(-1))
            self.win.bind("<KeyPress>", lambda e: self._on_key(e, True))
            self.win.bind("<KeyRelease>", lambda e: self._on_key(e, False))
            c.focus_set()
            c.bind("<Enter>", lambda e: c.focus_set())

            self.win.protocol("WM_DELETE_WINDOW", self.close)

            self.conn = ClientConnection(app, host, port, password,
                                         self._on_open, self._on_frame, self._on_close)
            self.conn.start()
            self.win.after(33, self._render)

        # -- network callbacks (run on net thread) ------------------------
        def _on_open(self, header):
            self.remote_w = header.get("sw") or header.get("w") or 1
            self.remote_h = header.get("sh") or header.get("h") or 1
            self.control = bool(header.get("control"))
            self.win.after(0, lambda: self.status.config(
                text="● %s  %dx%d  %s" % (
                    header.get("host", "remote"), self.remote_w, self.remote_h,
                    "control" if self.control else "view-only"), fg=OK_C))

        def _on_frame(self, img):
            try:
                if self.frame_q.full():
                    self.frame_q.get_nowait()
                self.frame_q.put_nowait(img)
            except Exception:
                pass

        def _on_close(self):
            self.alive = False
            try:
                self.win.after(0, lambda: self.status.config(text="● disconnected",
                                                             fg=ERR_C))
            except Exception:
                pass

        # -- render loop (main thread) ------------------------------------
        def _render(self):
            if not self.alive:
                return
            ImageTk = _get_imagetk()
            Image = _PIL_Image
            if ImageTk is None or Image is None:
                return
            try:
                img = self.frame_q.get_nowait()
            except queue.Empty:
                img = None
            if img is not None:
                cw = max(1, self.canvas.winfo_width())
                ch = max(1, self.canvas.winfo_height())
                iw, ih = img.size
                scale = min(cw / iw, ch / ih)
                dw, dh = max(1, int(iw * scale)), max(1, int(ih * scale))
                ox, oy = (cw - dw) // 2, (ch - dh) // 2
                self._disp = (ox, oy, dw, dh)
                disp = img.resize((dw, dh), Image.BILINEAR)
                self._photo = ImageTk.PhotoImage(disp)
                self.canvas.delete("all")
                self.canvas.create_image(ox, oy, anchor="nw", image=self._photo)
            self.win.after(20, self._render)

        # -- coordinate mapping -------------------------------------------
        def _norm(self, x, y):
            ox, oy, dw, dh = self._disp
            nx = (x - ox) / dw if dw else 0
            ny = (y - oy) / dh if dh else 0
            return max(0.0, min(1.0, nx)), max(0.0, min(1.0, ny))

        # -- input handlers -----------------------------------------------
        def _on_move(self, e):
            if not (self.alive and self.control):
                return
            nx, ny = self._norm(e.x, e.y)
            self.conn.send_input({"k": "move", "x": nx, "y": ny})

        def _on_btn(self, e, kind, btn):
            if not (self.alive and self.control):
                return
            nx, ny = self._norm(e.x, e.y)
            self.conn.send_input({"k": kind, "b": btn, "x": nx, "y": ny})

        def _on_wheel(self, e):
            self._wheel(1 if e.delta > 0 else -1)

        def _wheel(self, d):
            if self.alive and self.control:
                self.conn.send_input({"k": "scroll", "d": d})

        def _on_key(self, e, down):
            if not (self.alive and self.control):
                return
            self.conn.send_input({"k": "kdown" if down else "kup",
                                  "sym": e.keysym, "ch": e.char})

        def _send_clip(self):
            try:
                text = self.app.root.clipboard_get()
                self.conn.send_clip(text)
                self.app.log("clipboard pushed to remote (%d chars)" % len(text), OK_C)
            except Exception:
                self.app.log("clipboard empty", WARN_C)

        def _send_file(self):
            path = filedialog.askopenfilename(parent=self.win)
            if path:
                self.conn.send_file(path)

        def close(self):
            self.alive = False
            try:
                self.conn._end()
            except Exception:
                pass
            try:
                self.win.destroy()
            except Exception:
                pass

    # ---- dependency gate ------------------------------------------------
    missing = []
    if _mss is None:   missing.append("mss")
    if _PIL_Image is None:   missing.append("pillow")
    if _pynput is None: missing.append("pynput")
    root = tk.Tk()
    if missing:
        root.title("CatDesk 0.1 — missing deps")
        root.configure(bg=BG)
        root.geometry("520x260")
        miss_head = tk.Frame(root, bg=BG)
        miss_head.pack(pady=(28, 8))
        ImageTk = _get_imagetk()
        cat_img = _calico_cat_image(32)
        if ImageTk is not None and cat_img is not None:
            miss_cat = ImageTk.PhotoImage(cat_img)
            tk.Label(miss_head, image=miss_cat, bg=BG).pack(side="left", padx=(0, 8))
            root._miss_cat = miss_cat  # keep ref alive
        tk.Label(miss_head, text="CatDesk needs a couple of libs", bg=BG, fg=HUE,
                 font=FN_H).pack(side="left")
        tk.Label(root, text="pip install " + " ".join(missing), bg=PANEL2, fg=ID_C,
                 font=(FONT, 13, "bold"), padx=14, pady=10).pack(pady=8)
        if not HAVE_CRYPTO:
            tk.Label(root, text="optional: pip install cryptography  (encryption)",
                     bg=BG, fg=WARN_C, font=FN_SM).pack(pady=4)
        tk.Label(root, text="install, then relaunch. nya~", bg=BG, fg=GHOST,
                 font=FN_SM).pack(pady=12)
        root.mainloop()
        return

    App(root)
    root.mainloop()


# ============================================================================
#  entry point
# ============================================================================
def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "rendezvous":
        port = int(sys.argv[2]) if len(sys.argv) >= 3 else RDV_PORT
        RendezvousServer().serve(port)
        return
    run_gui()


if __name__ == "__main__":
    main()
