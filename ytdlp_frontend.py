#!/usr/bin/env python3
"""
yt-dlp Downloader: a Windows 7 styled (7.css) frontend for yt-dlp.

Setup
    pip install yt-dlp
    install ffmpeg and make sure it is on your PATH (needed for MP3 and for
    merging separate video and audio streams)

Run
    python ytdlp_frontend.py

The app starts a small server on 127.0.0.1 only and opens your browser.
7.css is loaded from a CDN. For offline use, save 7.css next to this file.
"""
import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8765
HERE = Path(__file__).resolve().parent
CSS_CDN = "https://unpkg.com/7.css"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

VIDEO_CONTAINERS = {"mp4", "mkv", "webm"}
AUDIO_FORMATS = {"mp3", "m4a", "opus", "vorbis", "flac", "wav"}
AUDIO_BITRATES = {64, 96, 128, 192, 256, 320}
LOSSLESS = {"flac", "wav"}

LOG_FILE = HERE / "ytdlp_frontend.log"
YTDLP = None
SERVER = None
YTDLP_VERSION = ""


# ----------------------------------------------------------------- helpers
def log_error(text):
    """Print an error and keep a copy in ytdlp_frontend.log next to this file."""
    try:
        print(text, file=sys.stderr, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n\n")
    except Exception:
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        log_error("Request error:\n" + traceback.format_exc())


def find_ytdlp():
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    if importlib.util.find_spec("yt_dlp"):
        return [sys.executable, "-m", "yt_dlp"]
    return None


def run(cmd, timeout=None):
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", creationflags=NOWIN, timeout=timeout,
    )


def ytdlp_version():
    if not YTDLP:
        return ""
    try:
        return run(YTDLP + ["--version"], timeout=20).stdout.strip()
    except Exception:
        return ""


DENO_CACHE = {"path": None, "version": ""}


def find_deno():
    """Return (path, on_path). Falls back to the usual install folders when PATH is stale."""
    exe = shutil.which("deno")
    if exe:
        return exe, True
    win = sys.platform.startswith("win")
    home = Path.home()
    dirs = []
    if os.environ.get("DENO_INSTALL"):
        dirs.append(Path(os.environ["DENO_INSTALL"]) / "bin")
    dirs.append(home / ".deno" / "bin")
    if win:
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        dirs.append(local / "Microsoft" / "WinGet" / "Links")
        dirs += sorted((local / "Microsoft" / "WinGet" / "Packages").glob("DenoLand.Deno*"))
        dirs += [home / "scoop" / "shims",
                 Path(os.environ.get("ProgramData") or "C:/ProgramData") / "chocolatey" / "bin"]
    else:
        dirs += [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), home / ".local" / "bin"]
    for d in dirs:
        f = d / ("deno.exe" if win else "deno")
        if f.is_file():
            return str(f), False
    return None, False


def deno_status():
    path, on_path = find_deno()
    if not path:
        return {"found": False, "ok": False, "version": ""}
    if DENO_CACHE["path"] != path:
        version = ""
        try:
            m = re.search(r"deno (\d+)\.(\d+)\.(\d+)", run([path, "--version"], timeout=20).stdout)
            if m:
                version = ".".join(m.groups())
        except Exception:
            pass
        DENO_CACHE.update(path=path, version=version)
    v = DENO_CACHE["version"]
    ok = tuple(int(x) for x in v.split(".")) >= (2, 3, 0) if v else True
    return {"found": True, "path": path, "on_path": on_path, "version": v, "ok": ok}


def deno_args():
    """Tell yt-dlp where Deno is when it isn't on PATH."""
    d = deno_status()
    if d["found"] and not d["on_path"]:
        return ["--js-runtimes", "deno:" + d["path"]]
    return []


def default_dir():
    d = Path.home() / "Downloads"
    return str(d if d.is_dir() else Path.home())


def error_line(output):
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    errs = [l for l in lines if l.startswith("ERROR")]
    if errs:
        return errs[-1].replace("ERROR: ", "", 1)
    return lines[-1] if lines else "yt-dlp failed without a message."


# -------------------------------------------------------------------- info
def get_info(url, playlist):
    if not YTDLP:
        raise RuntimeError("yt-dlp isn’t installed. Run: pip install yt-dlp")
    cmd = YTDLP + ["-J", "--no-warnings", "--flat-playlist"] + deno_args()
    if not playlist:
        cmd.append("--no-playlist")
    cmd += ["--", url]
    p = run(cmd, timeout=90)
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError(error_line(p.stderr or p.stdout))
    info = json.loads(p.stdout)

    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        return {
            "playlist": True,
            "title": info.get("title") or "Playlist",
            "uploader": info.get("uploader") or info.get("channel") or "",
            "count": len(entries),
            "heights": [],
        }

    formats = info.get("formats") or []

    def size(f):
        return f.get("filesize") or f.get("filesize_approx") or 0

    def has_video(f):
        return f.get("vcodec") not in (None, "none")

    def audio_only(f):
        return not has_video(f) and f.get("acodec") not in (None, "none")

    best_audio = max([size(f) for f in formats if audio_only(f)] or [0])
    heights = {}
    for f in formats:
        h = f.get("height")
        if h and has_video(f):
            heights[h] = max(heights.get(h, 0), size(f))
    return {
        "playlist": False,
        "title": info.get("title") or url,
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "heights": [
            {"h": h, "size": (s + best_audio) if s else 0}
            for h, s in sorted(heights.items(), reverse=True)
        ],
    }


# -------------------------------------------------------------------- jobs
class Job:
    def __init__(self, job_id, spec):
        self.id = job_id
        self.spec = spec
        self.url = spec["url"]
        self.name = spec["title"] or spec["url"]
        self.label = spec["label"]
        self.status = "queued"
        self.percent = 0.0
        self.speed = ""
        self.eta = ""
        self.item = ""
        self.error = ""
        self.log = []
        self.proc = None
        self.cancelled = False

    def public(self):
        keys = ("id", "name", "label", "status", "percent", "speed", "eta", "item", "error")
        return {k: getattr(self, k) for k in keys}


JOBS = []
JOBS_LOCK = threading.Lock()
JOB_Q = queue.Queue()
NEXT_ID = [1]

DL_RE = re.compile(r"\[dl\]\s+([\d.]+)%\s+(\S+)\s+(\S+)")
ITEM_RE = re.compile(r"Downloading item (\d+) of (\d+)")
PROCESSING = (
    "[Merger]", "[ExtractAudio]", "[EmbedThumbnail]", "[Metadata]",
    "[EmbedSubtitle]", "[VideoConvertor]", "[VideoRemuxer]",
    "[ThumbnailsConvertor]", "[Fixup",
)


def clean_spec(d):
    url = str(d.get("url", "")).strip()
    if not url:
        raise ValueError("Paste a link first.")
    kind = d.get("kind")
    if kind not in ("video", "audio"):
        raise ValueError("Choose video or audio.")

    out_dir = Path(os.path.expanduser(str(d.get("dir") or default_dir())))
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ValueError(f"Can’t use that folder: {e}")

    spec = {
        "url": url,
        "title": str(d.get("title") or "")[:200],
        "kind": kind,
        "dir": str(out_dir),
        "playlist": bool(d.get("playlist")),
        "thumb": bool(d.get("thumb")),
        "meta": bool(d.get("meta")),
        "subs": bool(d.get("subs")) and kind == "video",
    }
    if kind == "video":
        c = d.get("container")
        if c not in VIDEO_CONTAINERS:
            raise ValueError("Unknown video format.")
        h = d.get("height")
        h = int(h) if h else None
        if h is not None and not 100 <= h <= 8640:
            raise ValueError("Unknown video quality.")
        spec.update(container=c, height=h)
        spec["label"] = f"{c.upper()}, {str(h) + 'p' if h else 'best'}"
    else:
        f = d.get("aformat")
        if f not in AUDIO_FORMATS:
            raise ValueError("Unknown audio format.")
        abr = d.get("abr")
        abr = int(abr) if abr else None
        if abr is not None and abr not in AUDIO_BITRATES:
            raise ValueError("Unknown audio bitrate.")
        spec.update(aformat=f, abr=abr)
        name = "OGG" if f == "vorbis" else f.upper()
        spec["label"] = f"{name}, {str(abr) + ' kbps' if abr and f not in LOSSLESS else 'best'}"
    return spec


def build_args(s):
    a = [
        "--newline", "--no-colors", "--progress",
        "--progress-template",
        "download:[dl] %(progress._percent_str)s %(progress._speed_str)s %(progress._eta_str)s",
        "-P", s["dir"],
    ]
    if s["kind"] == "video":
        sort = f"res:{s['height']}" if s["height"] else "res"
        if s["container"] == "mp4":
            sort += ",vcodec:h264,acodec:m4a"
        elif s["container"] == "webm":
            sort += ",vcodec:vp9,acodec:opus"
        a += ["-f", "bv*+ba/b", "-S", sort, "--merge-output-format", s["container"]]
    else:
        a += ["-f", "ba/b", "-x", "--audio-format", s["aformat"]]
        if s["aformat"] not in LOSSLESS:
            a += ["--audio-quality", f"{s['abr']}K" if s["abr"] else "0"]

    if s["playlist"]:
        a += ["--yes-playlist", "-o", "%(playlist_title)s/%(playlist_index)s - %(title)s.%(ext)s"]
    else:
        a += ["--no-playlist", "-o", "%(title)s.%(ext)s"]
    if s["thumb"]:
        a += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]
    if s["meta"]:
        a.append("--embed-metadata")
    if s["subs"]:
        a += ["--write-subs", "--sub-langs", "en.*", "--embed-subs"]
    return a + deno_args()


def run_job(job):
    job.status = "downloading"
    cmd = YTDLP + build_args(job.spec) + ["--", job.url]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        job.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", env=env, bufsize=1,
            creationflags=NOWIN,
        )
        for raw in job.proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            m = DL_RE.search(line)
            if m:
                job.status = "downloading"
                job.percent = float(m.group(1))
                job.speed = "" if m.group(2) in ("Unknown", "N/A") else m.group(2)
                job.eta = "" if m.group(3) in ("Unknown", "N/A") else m.group(3)
                continue
            m = ITEM_RE.search(line)
            if m:
                job.item = f"{m.group(1)}/{m.group(2)}"
                job.percent = 0.0
            elif line.startswith(PROCESSING):
                job.status = "processing"
                job.speed = job.eta = ""
            elif (line.startswith("[download] Destination:") and not job.spec["title"]
                  and not job.spec["playlist"]):
                job.name = Path(line.split("Destination:", 1)[1].strip()).stem
            job.log.append(line)
            if len(job.log) > 600:
                del job.log[:100]
        code = job.proc.wait()
    except Exception as e:
        job.status, job.error = "error", str(e)
        job.log.append(f"ERROR: {e}")
        return
    if job.cancelled:
        job.status = "canceled"
    elif code == 0:
        job.status, job.percent, job.speed, job.eta = "done", 100.0, "", ""
    else:
        job.status = "error"
        job.error = error_line("\n".join(job.log[-30:]))


def worker():
    while True:
        job = JOB_Q.get()
        if job.cancelled:
            continue
        try:
            run_job(job)
        except Exception:
            log_error("Download error:\n" + traceback.format_exc())
            job.status = "error"
            job.error = "The app hit an internal error. See ytdlp_frontend.log."


def add_job(spec):
    if not YTDLP:
        raise RuntimeError("yt-dlp isn’t installed. Run: pip install yt-dlp")
    with JOBS_LOCK:
        job = Job(NEXT_ID[0], spec)
        NEXT_ID[0] += 1
        JOBS.append(job)
    JOB_Q.put(job)
    return job


def find_job(job_id):
    with JOBS_LOCK:
        return next((j for j in JOBS if j.id == job_id), None)


def cancel_job(job):
    if job.status in ("done", "error", "canceled"):
        return
    job.cancelled = True
    if job.status == "queued":
        job.status = "canceled"
    elif job.proc and job.proc.poll() is None:
        job.proc.terminate()


# ------------------------------------------------------------- system bits
def browse_dir(start):
    code = (
        "import sys, tkinter as t, tkinter.filedialog as f\n"
        "r = t.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
        "print(f.askdirectory(initialdir=sys.argv[1], title='Choose a download folder'))\n"
    )
    p = run([sys.executable, "-c", code, start])
    if p.returncode != 0:
        raise RuntimeError("The folder picker isn’t available here. Type the path instead.")
    return p.stdout.strip()


def open_folder(path):
    p = Path(path)
    if not p.is_dir():
        raise ValueError("That folder doesn’t exist yet.")
    if sys.platform.startswith("win"):
        os.startfile(str(p))  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])


def update_ytdlp():
    global YTDLP, YTDLP_VERSION
    if importlib.util.find_spec("yt_dlp"):
        p = run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"], timeout=300)
    elif YTDLP:
        p = run(YTDLP + ["-U"], timeout=300)
    else:
        p = run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"], timeout=300)
    YTDLP = find_ytdlp()
    YTDLP_VERSION = ytdlp_version()
    if p.returncode != 0:
        raise RuntimeError(error_line(p.stderr or p.stdout))
    return YTDLP_VERSION


# -------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"))

    def _host_ok(self):
        host = self.headers.get("Host", "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "Forbidden"}, 403)
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            return self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        if u.path == "/7.css":
            f = HERE / "7.css"
            if f.is_file():
                return self._send(200, f.read_bytes(), "text/css")
            self.send_response(302)
            self.send_header("Location", CSS_CDN)
            self.end_headers()
            return
        if u.path == "/api/state":
            with JOBS_LOCK:
                jobs = [j.public() for j in JOBS]
            return self._json({
                "ytdlp": bool(YTDLP), "version": YTDLP_VERSION,
                "ffmpeg": bool(shutil.which("ffmpeg")),
                "deno": deno_status(),
                "default_dir": default_dir(), "jobs": jobs,
            })
        if u.path == "/api/log":
            job = find_job(int(q.get("id", ["0"])[0] or 0))
            if not job:
                return self._json({"error": "No such download."}, 404)
            return self._json({"log": "\n".join(job.log[-300:]), "failure": job.error})
        self._json({"error": "Not found"}, 404)

    def do_POST(self):
        if not self._host_ok():
            return self._json({"error": "Forbidden"}, 403)
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host"):
            return self._json({"error": "Forbidden"}, 403)
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._json({"error": "Unsupported request."}, 415)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"error": "Bad request."}, 400)

        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/info":
                url = str(data.get("url", "")).strip()
                if not url:
                    raise ValueError("Paste a link first.")
                return self._json(get_info(url, bool(data.get("playlist"))))
            if path == "/api/download":
                job = add_job(clean_spec(data))
                return self._json({"id": job.id})
            if path == "/api/cancel":
                job = find_job(int(data.get("id", 0)))
                if job:
                    cancel_job(job)
                return self._json({"ok": True})
            if path == "/api/clear":
                with JOBS_LOCK:
                    JOBS[:] = [j for j in JOBS if j.status in ("queued", "downloading", "processing")]
                return self._json({"ok": True})
            if path == "/api/browse":
                return self._json({"dir": browse_dir(str(data.get("dir") or default_dir()))})
            if path == "/api/open":
                open_folder(str(data.get("dir") or ""))
                return self._json({"ok": True})
            if path == "/api/update":
                return self._json({"version": update_ytdlp()})
            if path == "/api/quit":
                with JOBS_LOCK:
                    for j in JOBS:
                        cancel_job(j)
                self._json({"ok": True})
                threading.Thread(target=SERVER.shutdown, daemon=True).start()
                return
            self._json({"error": "Not found"}, 404)
        except subprocess.TimeoutExpired:
            self._json({"error": "That took too long. Check your connection and try again."}, 504)
        except (ValueError, RuntimeError) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": f"Something went wrong: {e}"}, 500)


# --------------------------------------------------------------------- ui
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>yt-dlp Downloader</title>
<link rel="stylesheet" href="/7.css">
<style>
  [hidden] { display: none !important; }
  html, body { margin: 0; min-height: 100%; }
  body {
    padding: 28px 12px 48px;
    background:
      radial-gradient(ellipse 60% 45% at 72% 18%, rgba(190, 235, 255, .85), rgba(190, 235, 255, 0) 70%),
      radial-gradient(ellipse 90% 70% at 20% 100%, #0a2a66, rgba(10, 42, 102, 0) 70%),
      linear-gradient(160deg, #3d9be0 0%, #1d62b8 45%, #0d3a82 100%);
    background-attachment: fixed;
  }
  #app { max-width: 740px; margin: 0 auto; }
  .window-body { margin: 0; }
  fieldset { margin-bottom: 10px; }
  .row { display: flex; align-items: center; gap: 8px; margin: 4px 0; }
  .row input[type=text] { flex: 1; min-width: 0; }
  .row .lbl { width: 62px; }
  .row select { min-width: 220px; }
  #note { margin: 6px 0 2px; min-height: 1.2em; }
  #note.err { color: #b00020; }
  #preview { display: flex; gap: 12px; margin-top: 8px; align-items: flex-start; }
  #preview img { width: 160px; max-width: 40%; border: 1px solid #7a7a7a; background: #000; }
  #preview .meta p { margin: 0 0 4px; }
  #preview .title { font-weight: bold; }
  .actions { display: flex; justify-content: space-between; margin: 4px 0 12px; }
  #banner { padding: 8px; border: 1px solid #c9a227; background: #fff6d6; margin: 0 0 10px; }
  .queue { height: 150px; }
  .queue table { width: 100%; }
  .queue td { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 220px; }
  .queue td.pct { width: 110px; }
  .queue [role=progressbar] { width: 100%; }
  #joberr { color: #b00020; margin: 8px 0 0; }
  #joberr:empty { display: none; }
  .buttons { display: flex; gap: 8px; margin-top: 8px; }
  #log { width: 100%; box-sizing: border-box; font: 11px Consolas, "Lucida Console", monospace; resize: vertical; }
</style>
</head>
<body>
<div id="app">
<div class="window glass active">
  <div class="title-bar">
    <div class="title-bar-text">yt-dlp Downloader</div>
    <div class="title-bar-controls">
      <button aria-label="Minimize" disabled></button>
      <button aria-label="Maximize" disabled></button>
      <button aria-label="Close" id="close"></button>
    </div>
  </div>
  <div class="window-body has-space">
    <p id="banner" hidden></p>

    <fieldset>
      <legend>Video link</legend>
      <div class="row">
        <input id="url" type="text" placeholder="Paste a YouTube or other video link" autocomplete="off" autofocus>
        <button id="fetch">Get info</button>
      </div>
      <div class="row">
        <input type="checkbox" id="pl"><label for="pl">Download the whole playlist</label>
      </div>
      <p id="note"></p>
      <div id="preview" hidden>
        <img id="thumb" alt="" hidden>
        <div class="meta">
          <p class="title" id="ptitle"></p>
          <p id="pby"></p>
          <p id="pdur"></p>
        </div>
      </div>
    </fieldset>

    <fieldset>
      <legend>Format and quality</legend>
      <div class="row">
        <input type="radio" name="kind" id="kv" value="video"><label for="kv">Video</label>
        <input type="radio" name="kind" id="ka" value="audio"><label for="ka">Audio only</label>
      </div>
      <div class="row"><label class="lbl" for="fmt">Format:</label><select id="fmt"></select></div>
      <div class="row"><label class="lbl" for="qual">Quality:</label><select id="qual"></select><span id="qhint"></span></div>
    </fieldset>

    <fieldset>
      <legend>Options</legend>
      <div class="row"><input type="checkbox" id="othumb"><label for="othumb">Embed cover art</label></div>
      <div class="row"><input type="checkbox" id="ometa"><label for="ometa">Embed title and artist info</label></div>
      <div class="row"><input type="checkbox" id="osubs"><label for="osubs">Embed English subtitles (video only)</label></div>
    </fieldset>

    <fieldset>
      <legend>Save to</legend>
      <div class="row">
        <input id="dir" type="text" autocomplete="off">
        <button id="browse">Browse…</button>
        <button id="openf">Open folder</button>
      </div>
    </fieldset>

    <div class="actions">
      <button id="upd">Update yt-dlp</button>
      <button id="go" class="default">Download</button>
    </div>

    <fieldset>
      <legend>Downloads</legend>
      <div class="sunken-panel queue">
        <table class="interactive">
          <thead><tr><th>Name</th><th>Format</th><th>Status</th><th>Progress</th><th>Speed</th></tr></thead>
          <tbody id="jobs"></tbody>
        </table>
      </div>
      <div class="buttons">
        <button id="cancel">Cancel selected</button>
        <button id="clear">Clear finished</button>
      </div>
      <p id="joberr"></p>
    </fieldset>

    <fieldset>
      <legend>Details for the selected download</legend>
      <textarea id="log" rows="6" readonly placeholder="Select a download above to see what yt-dlp is doing."></textarea>
    </fieldset>
  </div>
  <div class="status-bar">
    <p class="status-bar-field" id="sb" style="flex:1">Ready</p>
    <p class="status-bar-field" id="sbv">yt-dlp</p>
    <p class="status-bar-field" id="sbf">ffmpeg</p>
    <p class="status-bar-field" id="sbd">Deno</p>
  </div>
</div>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

const VIDEO = [['mp4','MP4 (plays everywhere)'], ['mkv','MKV (keeps the best codecs)'], ['webm','WebM (VP9 and Opus)']];
const AUDIO = [['mp3','MP3'], ['m4a','M4A (AAC)'], ['opus','Opus'], ['vorbis','OGG Vorbis'], ['flac','FLAC (lossless)'], ['wav','WAV (lossless)']];
const HEIGHTS = [2160, 1440, 1080, 720, 480, 360, 240, 144];
const HNAME = {2160:'4K', 1440:'2K', 1080:'Full HD', 720:'HD'};
const ABRS = [320, 256, 192, 128, 96, 64];

let S = {kind:'video', vfmt:'mp4', afmt:'mp3', height:'', abr:'', thumb:true, meta:true, subs:false, dir:''};
try { Object.assign(S, JSON.parse(localStorage.getItem('ytdlpfe') || '{}')); } catch (e) {}
const save = () => { try { localStorage.setItem('ytdlpfe', JSON.stringify(S)); } catch (e) {} };

let fetched = null, jobs = [], selected = null, lastJson = '', ffmpegOk = true;

async function api(path, body) {
  const opt = body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)};
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({error:'The app sent an unreadable reply.'}));
  if (!r.ok || j.error) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}
function note(msg, err) { $('note').textContent = msg; $('note').className = err ? 'err' : ''; }
function setStatus(msg) { $('sb').textContent = msg; }
function fmtSize(b) { return b >= 1e9 ? (b/1e9).toFixed(1) + ' GB' : Math.max(1, Math.round(b/1e6)) + ' MB'; }
function fmtDur(s) {
  if (!s) return '';
  s = Math.round(s);
  const h = Math.floor(s/3600), m = Math.floor(s%3600/60), x = s%60;
  return (h ? h + ':' + String(m).padStart(2,'0') : m) + ':' + String(x).padStart(2,'0');
}
function setSelect(sel, items, value) {
  sel.innerHTML = items.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join('');
  if (items.some(i => String(i[0]) === String(value))) sel.value = value;
}

function renderQuality() {
  const q = $('qual');
  if (S.kind === 'audio') {
    setSelect(q, [['', 'Best available'], ...ABRS.map(b => [b, b + ' kbps'])], S.abr);
    const lossless = ['flac', 'wav'].includes($('fmt').value);
    q.disabled = lossless;
    $('qhint').textContent = lossless ? 'Lossless files have no bitrate to choose.' : '';
  } else {
    const known = fetched && fetched.heights && fetched.heights.length;
    const hs = known ? fetched.heights : HEIGHTS.map(h => ({h, size:0}));
    setSelect(q, [['', 'Best available'], ...hs.map(x =>
      [x.h, `${x.h}p${HNAME[x.h] ? ' (' + HNAME[x.h] + ')' : ''}${x.size ? ', about ' + fmtSize(x.size) : ''}`])], S.height);
    q.disabled = false;
    $('qhint').textContent = known ? '' : 'Press Get info to see what this video offers.';
  }
}
function renderOpts() {
  const f = $('fmt').value, audio = S.kind === 'audio';
  const noThumb = (audio && f === 'wav') || (!audio && f === 'webm');
  $('othumb').disabled = noThumb;
  $('othumb').checked = S.thumb && !noThumb;
  $('osubs').disabled = audio;
  $('osubs').checked = S.subs && !audio;
  $('ometa').checked = S.meta;
}
function renderForm() {
  const audio = S.kind === 'audio';
  $('kv').checked = !audio; $('ka').checked = audio;
  setSelect($('fmt'), audio ? AUDIO : VIDEO, audio ? S.afmt : S.vfmt);
  renderQuality(); renderOpts();
}

document.querySelectorAll('input[name=kind]').forEach(r => r.onchange = () => { S.kind = r.value; save(); renderForm(); });
$('fmt').onchange = () => { S[S.kind === 'audio' ? 'afmt' : 'vfmt'] = $('fmt').value; save(); renderQuality(); renderOpts(); };
$('qual').onchange = () => { S[S.kind === 'audio' ? 'abr' : 'height'] = $('qual').value; save(); };
$('othumb').onchange = () => { S.thumb = $('othumb').checked; save(); };
$('ometa').onchange = () => { S.meta = $('ometa').checked; save(); };
$('osubs').onchange = () => { S.subs = $('osubs').checked; save(); };
$('dir').onchange = () => { S.dir = $('dir').value.trim(); save(); };

function showPreview(r) {
  $('preview').hidden = false;
  $('ptitle').textContent = r.title;
  $('pby').textContent = r.uploader || '';
  $('pdur').textContent = r.playlist ? `Playlist with ${r.count} videos` : fmtDur(r.duration);
  $('thumb').hidden = !r.thumbnail;
  if (r.thumbnail) $('thumb').src = r.thumbnail;
}

async function getInfo() {
  const url = $('url').value.trim();
  if (!url) return note('Paste a link first.', true);
  $('fetch').disabled = true;
  note('Getting info…');
  try {
    const r = await api('/api/info', {url, playlist: $('pl').checked});
    r.url = url;
    fetched = r;
    if (r.playlist) $('pl').checked = true;
    showPreview(r);
    renderQuality();
    note(r.playlist ? 'This link is a playlist, so the playlist box is ticked. Qualities are checked per video.' : '');
  } catch (e) {
    fetched = null;
    $('preview').hidden = true;
    renderQuality();
    note(e.message, true);
  } finally {
    $('fetch').disabled = false;
  }
}

async function go() {
  const url = $('url').value.trim();
  if (!url) return note('Paste a link first.', true);
  const audio = S.kind === 'audio';
  const body = {
    url, kind: S.kind, dir: $('dir').value.trim(),
    title: fetched && fetched.url === url && !fetched.playlist ? fetched.title : '',
    container: $('fmt').value, aformat: $('fmt').value,
    height: !audio && $('qual').value ? +$('qual').value : null,
    abr: audio && $('qual').value ? +$('qual').value : null,
    playlist: $('pl').checked,
    thumb: $('othumb').checked, meta: $('ometa').checked, subs: $('osubs').checked,
  };
  $('go').disabled = true;
  try {
    const r = await api('/api/download', body);
    selected = r.id; lastJson = '';
    note('Added to the download list.');
  } catch (e) {
    note(e.message, true);
  } finally {
    $('go').disabled = false;
  }
}

function statusText(j) {
  const n = j.item ? ' ' + j.item : '';
  return {queued:'Waiting', downloading:'Downloading' + n, processing:'Converting' + n,
          done:'Finished', error:'Failed', canceled:'Canceled'}[j.status] || j.status;
}
function renderJobs() {
  $('jobs').innerHTML = jobs.length ? jobs.map(j => `
    <tr data-id="${j.id}" class="${j.id === selected ? 'highlighted' : ''}" title="${esc(j.error || j.name)}">
      <td>${esc(j.name)}</td><td>${esc(j.label)}</td><td>${esc(statusText(j))}</td>
      <td class="pct"><div role="progressbar"><div style="width:${j.percent}%"></div></div></td>
      <td>${esc([j.speed, j.eta && j.eta + ' left'].filter(Boolean).join(', '))}</td>
    </tr>`).join('')
    : '<tr><td colspan="5">Nothing here yet. Paste a link and press Download.</td></tr>';
  const sel = jobs.find(j => j.id === selected);
  $('joberr').textContent = sel && sel.error ? 'Why it failed: ' + sel.error : '';
}
$('jobs').onclick = e => {
  const tr = e.target.closest('tr[data-id]');
  if (!tr) return;
  selected = +tr.dataset.id; $('log').value = '';
  renderJobs(); refreshLog();
};

const isNetworkError = e => e instanceof TypeError && /fetch|network|load failed/i.test(e.message);

async function refreshLog() {
  if (selected === null || !jobs.some(x => x.id === selected)) return;
  const box = $('log');
  try {
    const l = await api('/api/log?id=' + selected);
    const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 8;
    const text = l.log + (l.failure ? '\n\nFailed: ' + l.failure : '');
    if (box.value !== text) { box.value = text; if (atEnd) box.scrollTop = box.scrollHeight; }
  } catch (e) {
    if (!box.value) box.value = 'Couldn’t load the details: ' + e.message;
  }
}

async function poll() {
  try {
    const s = await api('/api/state');
    if (!$('dir').value) $('dir').value = S.dir || s.default_dir;
    $('sbv').textContent = s.ytdlp ? 'yt-dlp ' + s.version : 'yt-dlp missing';
    $('sbf').textContent = s.ffmpeg ? 'ffmpeg found' : 'ffmpeg missing';
    $('sbd').textContent = s.deno.found ? 'Deno ' + (s.deno.version || 'found') : 'Deno missing';
    const msgs = [];
    if (!s.ytdlp) {
      msgs.push('yt-dlp isn’t installed. Run “pip install -U "yt-dlp[default]"” in a terminal, then restart this app.');
    } else {
      if (!s.ffmpeg) msgs.push('ffmpeg wasn’t found. MP3 and other audio conversions, and high-quality video merging, need it. Install ffmpeg and add it to your PATH.');
      if (!s.deno.found) msgs.push('Deno wasn’t found. YouTube downloads need it and may fail with “HTTP Error 403” without it. If you just installed it, close this terminal, open a new one and start the app again so it sees the updated PATH. Otherwise install it from deno.com (on Windows: winget install DenoLand.Deno).');
      else if (!s.deno.ok) msgs.push('Deno ' + s.deno.version + ' is too old. yt-dlp needs Deno 2.3.0 or newer. Update it with “deno upgrade”.');
    }
    $('banner').hidden = !msgs.length;
    $('banner').textContent = msgs.join(' ');
    const j = JSON.stringify(s.jobs);
    if (j !== lastJson) { lastJson = j; jobs = s.jobs; renderJobs(); }
    const active = jobs.filter(x => ['downloading', 'processing', 'queued'].includes(x.status)).length;
    setStatus(active ? `${active} download${active > 1 ? 's' : ''} in progress` : 'Ready');
  } catch (e) {
    console.error(e);
    setStatus(isNetworkError(e)
      ? 'Lost connection to the app. Check the terminal window that’s running it.'
      : 'Problem: ' + e.message);
  }
  await refreshLog();
  setTimeout(poll, 800);
}

$('fetch').onclick = getInfo;
$('url').onkeydown = e => { if (e.key === 'Enter') getInfo(); };
$('go').onclick = go;
$('browse').onclick = async () => {
  try {
    const r = await api('/api/browse', {dir: $('dir').value.trim()});
    if (r.dir) { $('dir').value = r.dir; S.dir = r.dir; save(); }
  } catch (e) { note(e.message, true); }
};
$('openf').onclick = async () => { try { await api('/api/open', {dir: $('dir').value.trim()}); } catch (e) { note(e.message, true); } };
$('cancel').onclick = async () => { if (selected !== null) { await api('/api/cancel', {id: selected}); lastJson = ''; } };
$('clear').onclick = async () => { await api('/api/clear', {}); selected = null; $('log').value = ''; lastJson = ''; };
$('upd').onclick = async () => {
  $('upd').disabled = true; setStatus('Updating yt-dlp…');
  try { const r = await api('/api/update', {}); note('yt-dlp is now at version ' + r.version + '.'); }
  catch (e) { note('Update failed: ' + e.message, true); }
  finally { $('upd').disabled = false; }
};
$('close').onclick = async () => {
  if (!confirm('Close yt-dlp Downloader? Downloads in progress will be canceled.')) return;
  try { await api('/api/quit', {}); } catch (e) {}
  document.body.innerHTML = '<div class="window active" style="max-width:420px;margin:80px auto"><div class="title-bar"><div class="title-bar-text">yt-dlp Downloader</div></div><div class="window-body has-space"><p>The app has closed. You can close this tab.</p></div></div>';
};

renderForm();
if (S.dir) $('dir').value = S.dir;
poll();
</script>
</body>
</html>
"""


# -------------------------------------------------------------------- main
def main():
    global YTDLP, YTDLP_VERSION, SERVER
    YTDLP = find_ytdlp()
    YTDLP_VERSION = ytdlp_version()
    threading.Thread(target=worker, daemon=True).start()

    for port in range(PORT, PORT + 20):
        try:
            SERVER = Server((HOST, port), Handler)
            break
        except OSError:
            continue
    else:
        sys.exit("Couldn't find a free port to listen on.")

    url = f"http://{HOST}:{SERVER.server_address[1]}/"
    print(f"yt-dlp Downloader is running at {url}")
    print("Press Ctrl+C here to stop it.")
    if not YTDLP:
        print("Warning: yt-dlp wasn't found. Run: pip install yt-dlp")
    threading.Timer(0.6, webbrowser.open, [url]).start()
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception:
        log_error("The server stopped unexpectedly:\n" + traceback.format_exc())
        try:
            input("The app stopped. Details are in ytdlp_frontend.log. Press Enter to close.")
        except Exception:
            pass
    finally:
        SERVER.server_close()


if __name__ == "__main__":
    main()
