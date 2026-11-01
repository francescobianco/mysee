#!/usr/bin/env python3
"""mysee - griglia di videosorveglianza per tutte le telecamere V4L2 del sistema.

Dipendenze: python3 (>= 3.9), python3-pil, v4l-utils (v4l2-ctl).

La cattura usa `v4l2-ctl -w`, cioe' passa da libv4l2: qualunque formato la
telecamera produca (MJPEG, YUYV, Bayer, formati proprietari gspca...) viene
convertito in RGB24 e poi ricompresso in JPEG per lo stream MJPEG.
Ogni telecamera viene aperta solo quando qualcuno la sta guardando e chiusa
dopo IDLE_STOP secondi senza spettatori.
"""
import argparse
import glob
import io
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

log = logging.getLogger("mysee")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_PIXELS = 640 * 480      # risoluzione massima richiesta (CPU limitata)
IDLE_STOP = 10.0            # secondi senza spettatori prima di chiudere la telecamera
RESCAN_EVERY = 15.0         # secondi tra due scansioni di /dev/video*
BOUNDARY = "myseeframe"


def v4l2(*args, timeout=10):
    r = subprocess.run(["v4l2-ctl", *args], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, universal_newlines=True,
                       timeout=timeout)
    return r.stdout


def probe(dev):
    """Restituisce (nome, bus, larghezza, altezza) se `dev` cattura video, altrimenti None."""
    try:
        info = v4l2("-d", dev, "-D")
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"Device Caps\s*:\s*0x[0-9a-fA-F]+\n((?:\t\t.*\n?)+)", info)
    caps = m.group(1) if m else info
    if "Video Capture" not in caps:
        return None
    card = re.search(r"Card type\s*:\s*(.+)", info)
    bus = re.search(r"Bus info\s*:\s*(.+)", info)
    sizes = [(int(w), int(h)) for w, h in
             re.findall(r"Size: Discrete (\d+)x(\d+)", v4l2("-d", dev, "--list-formats-ext"))]
    fit = [s for s in sizes if s[0] * s[1] <= MAX_PIXELS]
    if fit:
        w, h = max(fit, key=lambda s: s[0] * s[1])
    elif sizes:
        w, h = min(sizes, key=lambda s: s[0] * s[1])
    else:
        w, h = 640, 480
    return (card.group(1).strip() if card else os.path.basename(dev),
            bus.group(1).strip() if bus else "", w, h)


class Camera:
    def __init__(self, dev, name, bus, width, height):
        self.id = os.path.basename(dev)
        self.dev = dev
        self.name = name
        self.bus = bus
        self.width = width
        self.height = height
        self.cond = threading.Condition()
        self.frame = None
        self.seq = 0
        self.clients = 0
        self.last_use = 0.0
        self.thread = None
        self.error = None
        self.fps = 0.0
        self.removed = False

    def info(self):
        return {"id": self.id, "device": self.dev, "name": self.name, "bus": self.bus,
                "width": self.width, "height": self.height,
                "active": self.thread is not None, "fps": round(self.fps, 1),
                "error": self.error}

    # --- gestione spettatori -------------------------------------------------
    def _ensure_running(self):
        # chiamare con self.cond acquisito
        self.last_use = time.time()
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="cam-" + self.id, daemon=True)
            self.thread.start()

    def acquire(self):
        with self.cond:
            self.clients += 1
            self._ensure_running()

    def release(self):
        with self.cond:
            self.clients -= 1
            self.last_use = time.time()

    def touch(self):
        with self.cond:
            self._ensure_running()

    def _idle(self):
        return self.removed or (self.clients <= 0 and time.time() - self.last_use > IDLE_STOP)

    def wait_frame(self, last_seq, timeout):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != last_seq and self.frame is not None, timeout)
            return self.seq, self.frame

    # --- cattura ---------------------------------------------------------------
    def _run(self):
        log.info("%s: avvio cattura", self.dev)
        while True:
            try:
                self._capture()
            except Exception as e:  # telecamera scollegata, occupata, ecc.
                log.warning("%s: %s", self.dev, e)
                with self.cond:
                    self.error = str(e)
                    self.cond.notify_all()
                time.sleep(2)
            with self.cond:
                if self._idle():
                    self.thread = None
                    self.frame = None
                    self.fps = 0.0
                    log.info("%s: nessuno spettatore, cattura fermata", self.dev)
                    return

    def _capture(self):
        fmt = "width=%d,height=%d,pixelformat=RGB3" % (self.width, self.height)
        out = v4l2("-w", "-d", self.dev, "--set-fmt-video=" + fmt, "--get-fmt-video")
        m_size = re.search(r"Width/Height\s*:\s*(\d+)/(\d+)", out)
        m_bpl = re.search(r"Bytes per Line\s*:\s*(\d+)", out)
        if "'RGB3'" not in out or not m_size or not m_bpl:
            raise RuntimeError("impossibile impostare il formato RGB: " + out.strip()[-200:])
        w, h, bpl = int(m_size.group(1)), int(m_size.group(2)), int(m_bpl.group(1))
        size = bpl * h
        proc = subprocess.Popen(
            ["v4l2-ctl", "-w", "-d", self.dev, "--set-fmt-video=" + fmt,
             "--stream-mmap", "--stream-count=0", "--stream-to=-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        buf = bytearray(size)
        view = memoryview(buf)
        t0, n = time.time(), 0
        try:
            while True:
                with self.cond:
                    if self._idle():
                        return
                got = 0
                while got < size:
                    r = proc.stdout.readinto(view[got:])
                    if not r:
                        raise RuntimeError("stream interrotto (codice %s)" % proc.poll())
                    got += r
                img = Image.frombuffer("RGB", (w, h), bytes(buf), "raw", "RGB", bpl, 1)
                out_jpg = io.BytesIO()
                img.save(out_jpg, "JPEG", quality=80)
                n += 1
                now = time.time()
                with self.cond:
                    self.frame = out_jpg.getvalue()
                    self.seq += 1
                    self.error = None
                    self.width, self.height = w, h
                    if now - t0 >= 2:
                        self.fps, t0, n = n / (now - t0), now, 0
                    self.cond.notify_all()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


class Registry:
    def __init__(self):
        self.lock = threading.Lock()
        self.cams = {}

    def scan(self):
        devs = sorted(glob.glob("/dev/video*"), key=lambda d: int(re.sub(r"\D", "", d) or 0))
        found = {}
        for dev in devs:
            cid = os.path.basename(dev)
            with self.lock:
                cam = self.cams.get(cid)
            if cam is None:
                p = probe(dev)
                if p is None:
                    continue
                cam = Camera(dev, *p)
                log.info("trovata telecamera %s: %s %dx%d", dev, cam.name, cam.width, cam.height)
            found[cid] = cam
        with self.lock:
            for cid, cam in self.cams.items():
                if cid not in found:
                    log.info("telecamera rimossa: %s", cam.dev)
                    with cam.cond:
                        cam.removed = True
            self.cams = found

    def loop(self):
        while True:
            time.sleep(RESCAN_EVERY)
            try:
                self.scan()
            except Exception:
                log.exception("errore durante la scansione")

    def get(self, cid):
        with self.lock:
            return self.cams.get(cid)

    def all(self):
        with self.lock:
            return list(self.cams.values())


REG = Registry()


class Handler(BaseHTTPRequestHandler):
    server_version = "mysee/1.0"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/cameras":
            body = json.dumps({"host": socket.gethostname(),
                               "cameras": [c.info() for c in REG.all()]}).encode()
            return self._send(200, body, "application/json")
        m = re.fullmatch(r"/(stream|snapshot)/([\w.-]+?)(?:\.mjpg|\.jpg)?", path)
        if m:
            cam = REG.get(m.group(2))
            if cam is None:
                return self._send(404, b"telecamera non trovata", "text/plain")
            return self._stream(cam) if m.group(1) == "stream" else self._snapshot(cam)
        self._send(404, b"not found", "text/plain")

    def _snapshot(self, cam):
        cam.touch()
        _, frame = cam.wait_frame(-1, 10)
        if frame is None:
            return self._send(503, (cam.error or "nessun fotogramma").encode(), "text/plain")
        self._send(200, frame, "image/jpeg")

    def _stream(self, cam):
        cam.acquire()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=" + BOUNDARY)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seq = -1
            while not cam.removed:
                new_seq, frame = cam.wait_frame(seq, 10)
                if frame is None:
                    continue
                seq = new_seq
                self.wfile.write(("--%s\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                                  % (BOUNDARY, len(frame))).encode() + frame + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        finally:
            cam.release()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    REG.scan()
    threading.Thread(target=REG.loop, name="rescan", daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    log.info("mysee in ascolto su http://%s:%d/ (%d telecamere)", a.host, a.port, len(REG.all()))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
