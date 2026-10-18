#!/usr/bin/env python3
"""
yt-dlp Downloader: a Windows 7 styled (7.css) frontend for yt-dlp, with a
Convert tab (powered by ffmpeg) for shrinking videos for devices like the iPod.

Setup
    pip install -U "yt-dlp[default]"
    install ffmpeg and make sure it is on your PATH (needed for MP3, for
    merging separate video and audio streams, and for the Convert tab)
    install Deno (needed for YouTube downloads)

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
import tempfile
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

VIDEO_EXTS = {".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts",
              ".wmv", ".mpg", ".mpeg", ".3gp"}
CONVERT_CONTAINERS = {"mp4", "m4v", "mkv"}
CONVERT_AUDIO_BITRATES = {64, 96, 128, 160, 192, 256, 320}
SPEEDS = {"fast": "veryfast", "balanced": "medium", "slow": "slow"}
FIT_MODES = {"letterbox", "crop", "stretch", "shrink"}
ACTIVE = ("queued", "downloading", "processing", "converting")

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
ENC_CACHE = {"path": None, "enc": {}}


def find_exe(name):
    """Return (path, on_path). Falls back to the usual install folders when PATH is stale."""
    exe = shutil.which(name)
    if exe:
        return exe, True
    win = sys.platform.startswith("win")
    home = Path.home()
    dirs = []
    if name == "deno":
        if os.environ.get("DENO_INSTALL"):
            dirs.append(Path(os.environ["DENO_INSTALL"]) / "bin")
        dirs.append(home / ".deno" / "bin")
    if win:
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        pkgs = local / "Microsoft" / "WinGet" / "Packages"
        dirs.append(local / "Microsoft" / "WinGet" / "Links")
        if name == "deno":
            dirs += sorted(pkgs.glob("DenoLand.Deno*"))
        else:
            dirs += sorted(pkgs.glob("Gyan.FFmpeg*/ffmpeg-*/bin"))
            dirs += [Path("C:/ffmpeg/bin"), Path("C:/Program Files/ffmpeg/bin")]
        dirs += [home / "scoop" / "shims",
                 Path(os.environ.get("ProgramData") or "C:/ProgramData") / "chocolatey" / "bin"]
    else:
        dirs += [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), home / ".local" / "bin"]
    for d in dirs:
        f = d / (name + (".exe" if win else ""))
        if f.is_file():
            return str(f), False
    return None, False


def find_deno():
    return find_exe("deno")


def ffmpeg_path():
    return find_exe("ffmpeg")[0]


def ffprobe_path():
    return find_exe("ffprobe")[0]


def ffmpeg_args():
    """Tell yt-dlp where ffmpeg is when it isn't on PATH."""
    path, on_path = find_exe("ffmpeg")
    if path and not on_path:
        return ["--ffmpeg-location", str(Path(path).parent)]
    return []


def encoders():
    """Which ffmpeg encoders this build has (checked once, then cached)."""
    path = ffmpeg_path()
    if not path:
        return {}
    if ENC_CACHE["path"] != path:
        try:
            out = run([path, "-hide_banner", "-encoders"], timeout=20).stdout
        except Exception:
            out = ""
        ENC_CACHE.update(path=path, enc={
            n: f" {n} " in out for n in ("libx264", "libx265", "aac", "libmp3lame")})
    return ENC_CACHE["enc"]


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
    def __init__(self, job_id, spec, kind="download"):
        self.id = job_id
        self.spec = spec
        self.kind = kind
        self.url = spec.get("url", "")
        self.name = spec.get("title") or self.url
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
        self.output = ""
        self.insize = 0
        self.outsize = 0
        self.final = None
        self.dest = None
        self.files = []

    def public(self):
        keys = ("id", "kind", "name", "label", "status", "percent", "speed", "eta",
                "item", "error", "output", "insize", "outsize")
        return {k: getattr(self, k) for k in keys}


JOBS = []
JOBS_LOCK = threading.Lock()
JOB_Q = queue.Queue()
CONV_Q = queue.Queue()
NEXT_ID = [1]
RECENT = []  # video files from finished downloads, oldest first
RECENT_LOCK = threading.Lock()

DL_RE = re.compile(r"\[dl\]\s+([\d.]+)%\s+(\S+)\s+(\S+)")
ITEM_RE = re.compile(r"Downloading item (\d+) of (\d+)")
MERGE_RE = re.compile(r'^\[Merger\] Merging formats into "(.+)"$')
ALREADY_RE = re.compile(r"^\[download\] (.+) has already been downloaded")
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

    out_dir = Path(os.path.expanduser(str(d.get("dir") or default_dir()))).resolve()
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
    return a + deno_args() + ffmpeg_args()


def remember_files(paths):
    with RECENT_LOCK:
        for f in paths:
            if Path(f).suffix.lower() in VIDEO_EXTS and f not in RECENT:
                RECENT.append(f)
        del RECENT[:-30]


def run_job(job):
    job.status = "downloading"
    tmp = Path(tempfile.gettempdir()) / f"aerodlp_{os.getpid()}_{job.id}.txt"
    cmd = (YTDLP + build_args(job.spec)
           + ["--print-to-file", "after_move:filepath", tmp.as_posix(), "--", job.url])
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    code = None
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
            # remember where the finished file went (backup for --print-to-file)
            m = MERGE_RE.match(line)
            if m:
                job.final = m.group(1)
            elif line.startswith("[ExtractAudio] Destination:"):
                job.final = line.split("Destination:", 1)[1].strip()
            else:
                m = ALREADY_RE.match(line)
                if m:
                    job.final = m.group(1)
                elif line.startswith("[download] Destination:"):
                    job.dest = line.split("Destination:", 1)[1].strip()
                    if not job.spec["title"] and not job.spec["playlist"]:
                        job.name = Path(job.dest).stem
            job.log.append(line)
            if len(job.log) > 600:
                del job.log[:100]
        code = job.proc.wait()
    except Exception as e:
        job.status, job.error = "error", str(e)
        job.log.append(f"ERROR: {e}")
    finally:
        files = []
        try:
            if tmp.is_file():
                files = [l.strip() for l in
                         tmp.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
        except OSError:
            pass
        try:
            tmp.unlink()
        except OSError:
            pass
        if not files:
            guess = job.final or job.dest
            files = [guess] if guess else []
        job.files = [f for f in files if os.path.isfile(f)]
        job.output = job.files[-1] if job.files else ""
        remember_files(job.files)
    if code is None:
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


# ----------------------------------------------------------------- convert
def probe(path):
    ffprobe = ffprobe_path()
    if not ffprobe:
        raise RuntimeError("ffprobe wasn’t found. It comes with ffmpeg, so install ffmpeg first.")
    p = run([ffprobe, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)], timeout=60)
    if p.returncode != 0:
        raise RuntimeError("Couldn’t read that file: " + error_line(p.stderr or p.stdout))
    d = json.loads(p.stdout or "{}")
    streams = d.get("streams") or []
    v = next((x for x in streams if x.get("codec_type") == "video"
              and not (x.get("disposition") or {}).get("attached_pic")), None)
    a = next((x for x in streams if x.get("codec_type") == "audio"), None)
    fmt = d.get("format") or {}

    def num(x, default=0.0):
        try:
            return float(x)
        except (TypeError, ValueError):
            return default

    fps = 0.0
    if v:
        try:
            n, den = str(v.get("avg_frame_rate") or "0/1").split("/")
            fps = float(n) / float(den) if float(den) else 0.0
        except (ValueError, ZeroDivisionError):
            fps = 0.0
    return {
        "has_video": bool(v), "has_audio": bool(a),
        "width": int((v or {}).get("width") or 0), "height": int((v or {}).get("height") or 0),
        "vcodec": (v or {}).get("codec_name") or "", "acodec": (a or {}).get("codec_name") or "",
        "fps": round(fps, 3),
        "duration": num(fmt.get("duration")) or num((v or {}).get("duration")),
        "size": int(num(fmt.get("size"))),
        "abr": int(num((a or {}).get("bit_rate")) / 1000),
    }


def parse_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    msg = f"“{text}” isn’t a valid time. Use seconds, mm:ss or hh:mm:ss."
    try:
        parts = [float(x) for x in text.split(":")]
    except ValueError:
        raise ValueError(msg)
    if len(parts) > 3 or any(x < 0 for x in parts):
        raise ValueError(msg)
    t = 0.0
    for x in parts:
        t = t * 60 + x
    return t


def opt_int(value, lo, hi, what):
    if value in (None, ""):
        return None
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number.")
    if not lo <= n <= hi:
        raise ValueError(f"{what} must be between {lo} and {hi}.")
    return n


def clean_convert_spec(d):
    raw = str(d.get("source") or "").strip().strip('"')
    if not raw:
        raise ValueError("Choose a video to convert first.")
    src = Path(os.path.expanduser(raw))
    if not src.is_file():
        raise ValueError("That file doesn’t exist.")
    src = src.resolve()
    out_raw = str(d.get("outdir") or "").strip().strip('"')
    outdir = (Path(os.path.expanduser(out_raw)) if out_raw else src.parent).resolve()
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ValueError(f"Can’t use that output folder: {e}")

    container = d.get("container")
    if container not in CONVERT_CONTAINERS:
        raise ValueError("Unknown container.")
    vcodec = d.get("vcodec")
    if vcodec not in ("h264", "h265", "copy"):
        raise ValueError("Unknown video codec.")
    enc = encoders()
    if vcodec == "h264" and not enc.get("libx264"):
        raise ValueError("This ffmpeg build can’t encode H.264 (libx264 is missing).")
    if vcodec == "h265" and not enc.get("libx265"):
        raise ValueError("This ffmpeg build can’t encode H.265 (libx265 is missing).")
    mode = d.get("mode")
    if vcodec != "copy" and mode not in ("crf", "bitrate", "size"):
        raise ValueError("Unknown quality mode.")
    speed = d.get("speed")
    if speed not in SPEEDS:
        speed = "balanced"
    profile = d.get("profile") or ""
    if profile not in ("", "baseline", "main", "high"):
        raise ValueError("Unknown H.264 profile.")
    level = str(d.get("level") or "")
    if level not in ("", "1.3", "3.0", "3.1", "4.0", "4.1", "4.2"):
        raise ValueError("Unknown H.264 level.")
    fit = d.get("fit")
    if fit not in FIT_MODES:
        fit = "letterbox"
    acodec = d.get("acodec")
    if acodec not in ("aac", "mp3", "copy", "none"):
        raise ValueError("Unknown audio codec.")
    if acodec == "aac" and not enc.get("aac"):
        raise ValueError("This ffmpeg build can’t encode AAC.")
    if acodec == "mp3" and not enc.get("libmp3lame"):
        raise ValueError("This ffmpeg build can’t encode MP3 (libmp3lame is missing).")
    abr = opt_int(d.get("abr"), 32, 320, "Audio bitrate") or 128
    if abr not in CONVERT_AUDIO_BITRATES:
        raise ValueError("Unknown audio bitrate.")
    ar = opt_int(d.get("ar"), 8000, 96000, "Sample rate")
    ch = opt_int(d.get("ch"), 1, 2, "Channels")

    start = parse_time(d.get("start"))
    end = parse_time(d.get("end"))
    if end is not None and end <= (start or 0):
        raise ValueError("The end time has to be after the start time.")

    width = opt_int(d.get("width"), 16, 8192, "Width")
    height = opt_int(d.get("height"), 16, 8192, "Height")
    suffix = re.sub(r'[\\/:*?"<>|]+', "", str(d.get("suffix") or "")).strip()[:40]

    spec = {
        "title": src.stem, "source": str(src), "outdir": str(outdir),
        "label": str(d.get("label") or "Custom")[:60], "suffix": suffix,
        "container": container, "vcodec": vcodec, "mode": mode, "speed": speed,
        "crf": opt_int(d.get("crf"), 0, 51, "Quality (CRF)"),
        "vbitrate": opt_int(d.get("vbitrate"), 50, 50000, "Video bitrate"),
        "size_mb": opt_int(d.get("size_mb"), 1, 100000, "Target size"),
        "profile": profile, "level": level,
        "maxrate": opt_int(d.get("maxrate"), 50, 100000, "Max bitrate"),
        "bufsize": opt_int(d.get("bufsize"), 50, 200000, "Buffer size"),
        "refs": opt_int(d.get("refs"), 1, 16, "Reference frames"),
        "width": width // 2 * 2 if width else None,
        "height": height // 2 * 2 if height else None,
        "fit": fit, "fpsmax": opt_int(d.get("fpsmax"), 1, 120, "Max frame rate"),
        "acodec": acodec, "abr": abr, "ar": ar, "ch": ch,
        "start": start, "end": end,
    }
    if vcodec != "copy":
        key, what = {"crf": ("crf", "a quality (CRF) value"),
                     "bitrate": ("vbitrate", "a video bitrate"),
                     "size": ("size_mb", "a target size")}[mode]
        if spec[key] is None:
            raise ValueError(f"Enter {what}.")
    return spec


def unique_path(p):
    if not p.exists():
        return p
    for i in range(2, 1000):
        q = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not q.exists():
            return q
    raise RuntimeError("There are too many files with that name already.")


def build_ffmpeg(s, info, out):
    """Return (ffmpeg command, length of the output in seconds or 0 if unknown)."""
    start = s["start"] or 0.0
    total = info["duration"]
    if s["end"]:
        end = min(s["end"], total) if total else s["end"]
        out_len = max(end - start, 0.0)
    else:
        out_len = max(total - start, 0.0) if total else 0.0

    c = [ffmpeg_path(), "-hide_banner", "-nostdin", "-n", "-nostats", "-progress", "pipe:1"]
    if start:
        c += ["-ss", f"{start:.3f}"]
    c += ["-i", s["source"]]
    if s["end"]:
        c += ["-t", f"{out_len:.3f}"]

    keep_audio = s["acodec"] != "none" and info["has_audio"]
    c += ["-map", "0:v:0"]
    if keep_audio:
        c += ["-map", "0:a:0"]
    c += ["-sn", "-dn", "-map_chapters", "-1"]

    # video
    if s["vcodec"] == "copy":
        c += ["-c:v", "copy"]
    else:
        vf = []
        if s["fpsmax"] and info["fps"] and info["fps"] > s["fpsmax"] + 0.01:
            vf.append(f"fps={s['fpsmax']}")
        w, h, fit = s["width"], s["height"], s["fit"]
        if w or h:
            if fit in ("letterbox", "crop", "stretch") and not (w and h):
                fit = "shrink"
            if fit == "letterbox":
                vf.append(f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2")
                vf.append(f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")
            elif fit == "crop":
                vf.append(f"scale={w}:{h}:force_original_aspect_ratio=increase")
                vf.append(f"crop={w}:{h}")
            elif fit == "stretch":
                vf.append(f"scale={w}:{h}")
            else:
                sw = f"min({w},iw)" if w else "iw"
                sh = f"min({h},ih)" if h else "ih"
                vf.append(f"scale='{sw}':'{sh}':force_original_aspect_ratio=decrease:force_divisible_by=2")
        else:
            vf.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
        vf.append("setsar=1")
        c += ["-vf", ",".join(vf)]

        preset = SPEEDS[s["speed"]]
        if s["vcodec"] == "h264":
            c += ["-c:v", "libx264", "-preset", preset, "-pix_fmt", "yuv420p"]
            if s["profile"]:
                c += ["-profile:v", s["profile"]]
            if s["level"]:
                c += ["-level", s["level"]]
            if s["refs"]:
                c += ["-refs", str(s["refs"])]
        else:
            c += ["-c:v", "libx265", "-preset", preset, "-pix_fmt", "yuv420p",
                  "-x265-params", "log-level=error"]
            if s["container"] in ("mp4", "m4v"):
                c += ["-tag:v", "hvc1"]

        if s["mode"] == "crf":
            c += ["-crf", str(s["crf"])]
        elif s["mode"] == "bitrate":
            c += ["-b:v", f"{s['vbitrate']}k"]
        else:
            if out_len <= 0:
                raise ValueError("Couldn’t read this video’s length, so a target file size can’t be used.")
            if s["acodec"] == "none" or not keep_audio:
                audio_kbps = 0
            elif s["acodec"] == "copy":
                audio_kbps = info["abr"] or 128
            else:
                audio_kbps = s["abr"]
            kbps = s["size_mb"] * 8 * 1048576 / out_len / 1000 * 0.97 - audio_kbps
            c += ["-b:v", f"{max(50, int(kbps))}k"]
        if s["maxrate"]:
            c += ["-maxrate", f"{s['maxrate']}k", "-bufsize", f"{s['bufsize'] or s['maxrate'] * 2}k"]

    # audio
    if not keep_audio:
        c += ["-an"]
    elif s["acodec"] == "copy":
        c += ["-c:a", "copy"]
    else:
        c += ["-c:a", "aac" if s["acodec"] == "aac" else "libmp3lame", "-b:a", f"{s['abr']}k"]
        if s["ar"]:
            c += ["-ar", str(s["ar"])]
        if s["ch"]:
            c += ["-ac", str(s["ch"])]

    # container
    if s["container"] == "mkv":
        c += ["-f", "matroska"]
    else:
        ipod = (s["container"] == "m4v" and s["vcodec"] == "h264"
                and (s["acodec"] == "aac" or not keep_audio))
        c += ["-movflags", "+faststart", "-f", "ipod" if ipod else "mp4"]
    c.append(str(out))
    return c, out_len


PROG_RE = re.compile(r"^(frame|fps|bitrate|total_size|out_time_us|out_time_ms|out_time|"
                     r"dup_frames|drop_frames|speed|progress|stream_\d+_\d+_q)=(.*)$")


def fmt_clock(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def run_convert(job):
    s = job.spec
    out = None
    code = None
    job.status = "converting"
    try:
        info = probe(s["source"])
        if not info["has_video"]:
            raise ValueError("That file has no video to convert.")
        job.insize = info["size"]
        ext = s["container"]
        stem = Path(s["source"]).stem
        name = f"{stem} [{s['suffix']}]" if s["suffix"] else f"{stem} (converted)"
        out = unique_path(Path(s["outdir"]) / f"{name}.{ext}")
        cmd, out_len = build_ffmpeg(s, info, out)
        job.log.append("$ " + subprocess.list2cmdline(cmd))
        job.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1, creationflags=NOWIN,
        )
        for raw in job.proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            m = PROG_RE.match(line)
            if not m:
                job.log.append(line)
                if len(job.log) > 600:
                    del job.log[1:100]
                continue
            key, val = m.group(1), m.group(2).strip()
            if key in ("out_time_us", "out_time_ms") and val.lstrip("-").isdigit():
                t = int(val) / 1e6
                if out_len > 0:
                    job.percent = max(0.0, min(99.9, t / out_len * 100))
                    sp = job.speed.rstrip("x")
                    try:
                        job.eta = fmt_clock((out_len - t) / float(sp)) if float(sp) > 0 else ""
                    except ValueError:
                        job.eta = ""
            elif key == "speed" and val not in ("N/A", ""):
                job.speed = val
            elif key == "progress" and val == "end":
                job.percent = 100.0
        code = job.proc.wait()
    except (ValueError, RuntimeError) as e:
        job.status, job.error = "error", str(e)
        job.log.append(f"ERROR: {e}")
    except Exception as e:
        job.status, job.error = "error", str(e)
        job.log.append(f"ERROR: {e}")
        log_error("Convert error:\n" + traceback.format_exc())
    if code is not None:
        if job.cancelled:
            job.status = "canceled"
        elif code == 0:
            job.status, job.percent, job.speed, job.eta = "done", 100.0, "", ""
            job.output = str(out)
            try:
                job.outsize = out.stat().st_size
            except OSError:
                pass
        else:
            job.status = "error"
            job.error = error_line("\n".join(job.log[-30:]))
    if job.status != "done" and out is not None:
        try:
            out.unlink()
        except OSError:
            pass


def conv_worker():
    while True:
        job = CONV_Q.get()
        if job.cancelled:
            continue
        try:
            run_convert(job)
        except Exception:
            log_error("Convert error:\n" + traceback.format_exc())
            job.status = "error"
            job.error = "The app hit an internal error. See ytdlp_frontend.log."


def add_convert(spec):
    if not ffmpeg_path():
        raise RuntimeError("ffmpeg wasn’t found. Install it and restart the app.")
    with JOBS_LOCK:
        job = Job(NEXT_ID[0], spec, kind="convert")
        NEXT_ID[0] += 1
        JOBS.append(job)
    CONV_Q.put(job)
    return job


def recent_videos():
    with RECENT_LOCK:
        files = [f for f in reversed(RECENT) if os.path.isfile(f)]
    return [{"path": f, "name": Path(f).name} for f in files]


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


def browse_file(start):
    code = (
        "import sys, tkinter as t, tkinter.filedialog as f\n"
        "r = t.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
        "types = [('Video files', '*.mp4 *.m4v *.mkv *.webm *.mov *.avi *.flv *.ts *.wmv *.mpg *.mpeg *.3gp'),"
        " ('All files', '*.*')]\n"
        "print(f.askopenfilename(initialdir=sys.argv[1], title='Choose a video to convert', filetypes=types))\n"
    )
    start_dir = start if Path(start).is_dir() else str(Path.home())
    p = run([sys.executable, "-c", code, start_dir])
    if p.returncode != 0:
        raise RuntimeError("The file picker isn’t available here. Type the path instead.")
    return p.stdout.strip()


def open_folder(path):
    p = Path(path)
    if p.is_file():
        p = p.parent
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
                "ffmpeg": bool(ffmpeg_path()),
                "deno": deno_status(),
                "ffprobe": bool(ffprobe_path()), "encoders": encoders(),
                "videos": recent_videos(),
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
                kind = data.get("kind")
                with JOBS_LOCK:
                    JOBS[:] = [j for j in JOBS
                               if j.status in ACTIVE or (kind and j.kind != kind)]
                return self._json({"ok": True})
            if path == "/api/probe":
                f = Path(os.path.expanduser(str(data.get("path") or "").strip().strip('"')))
                if not f.is_file():
                    raise ValueError("That file doesn’t exist.")
                return self._json(probe(f.resolve()))
            if path == "/api/convert":
                job = add_convert(clean_convert_spec(data))
                return self._json({"id": job.id})
            if path == "/api/browse_file":
                return self._json({"path": browse_file(str(data.get("dir") or default_dir()))})
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
  #app { max-width: 780px; margin: 0 auto; }
  .window-body { margin: 0; }
  fieldset { margin-bottom: 10px; }
  .row { display: flex; align-items: center; gap: 8px; margin: 4px 0; flex-wrap: wrap; }
  .row input[type=text] { flex: 1; min-width: 0; }
  .row .lbl { width: 62px; }
  .row .lbl.w { width: 104px; }
  .row .lbl.n { width: auto; margin-left: 12px; }
  .row select { min-width: 200px; }
  .row input[type=number] { width: 90px; }
  .row input.t { width: 90px; flex: none; }
  #note, #cvnote { margin: 6px 0 2px; min-height: 1.2em; }
  #note.err, #cvnote.err { color: #b00020; }
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
  .err-line { color: #b00020; margin: 8px 0 0; }
  .err-line:empty, .hint-line:empty { display: none; }
  .hint-line { margin: 8px 0 0; }
  .buttons { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
  textarea.log { width: 100%; box-sizing: border-box; font: 11px Consolas, "Lucida Console", monospace; resize: vertical; }
  #cvinfo, #cvpnote, #cvest { margin: 6px 0 2px; }
  #cvpnote { min-height: 2.4em; }
  article[role=tabpanel] { padding-top: 12px; }
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

    <section class="tabs">
      <menu role="tablist" aria-label="Sections">
        <button role="tab" data-tab="dl" aria-controls="tab-dl" aria-selected="true">Download</button>
        <button role="tab" data-tab="cv" aria-controls="tab-cv" aria-selected="false">Convert</button>
      </menu>

      <!-- ===================================================== Download -->
      <article role="tabpanel" id="tab-dl">
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
            <button id="toconv" disabled>Convert selected…</button>
          </div>
          <p id="joberr" class="err-line"></p>
          <p id="jobhint" class="hint-line"></p>
        </fieldset>

        <fieldset>
          <legend>Details for the selected download</legend>
          <textarea id="log" class="log" rows="6" readonly placeholder="Select a download above to see what yt-dlp is doing."></textarea>
        </fieldset>
      </article>

      <!-- ====================================================== Convert -->
      <article role="tabpanel" id="tab-cv" hidden>
        <fieldset>
          <legend>Source</legend>
          <div class="row">
            <label class="lbl" for="cvpath">File:</label>
            <input id="cvpath" type="text" placeholder="Choose a video, or paste its path" autocomplete="off">
            <button id="cvbrowse">Browse…</button>
          </div>
          <div class="row">
            <label class="lbl" for="cvrecent">Recent:</label>
            <select id="cvrecent"></select>
          </div>
          <p id="cvinfo"></p>
        </fieldset>

        <fieldset>
          <legend>Preset</legend>
          <div class="row"><label class="lbl" for="cvpreset">Device:</label><select id="cvpreset"></select></div>
          <p id="cvpnote"></p>
        </fieldset>

        <fieldset>
          <legend>Video</legend>
          <div class="row">
            <label class="lbl w" for="cvcontainer">Container:</label>
            <select id="cvcontainer">
              <option value="mp4">MP4</option>
              <option value="m4v">M4V (iPod and iTunes)</option>
              <option value="mkv">MKV</option>
            </select>
            <label class="lbl n" for="cvvcodec">Codec:</label>
            <select id="cvvcodec">
              <option value="h264">H.264 (plays almost everywhere)</option>
              <option value="h265">H.265 (smaller files, slower)</option>
              <option value="copy">Copy video, don’t re-encode</option>
            </select>
          </div>
          <div class="row">
            <label class="lbl w" for="cvmode">Quality:</label>
            <select id="cvmode">
              <option value="crf">Constant quality</option>
              <option value="bitrate">Average bitrate</option>
              <option value="size">Target file size</option>
            </select>
            <input id="cvq" type="number" min="0" step="1">
            <span id="cvqunit"></span>
          </div>
          <div class="row">
            <label class="lbl w" for="cvspeed">Encoder speed:</label>
            <select id="cvspeed">
              <option value="fast">Fast</option>
              <option value="balanced">Balanced</option>
              <option value="slow">Slow (best quality for the size)</option>
            </select>
          </div>
          <div class="row">
            <label class="lbl w" for="cvprofile">H.264 profile:</label>
            <select id="cvprofile">
              <option value="">Automatic</option>
              <option value="baseline">Baseline (oldest devices)</option>
              <option value="main">Main</option>
              <option value="high">High</option>
            </select>
            <label class="lbl n" for="cvlevel">Level:</label>
            <select id="cvlevel">
              <option value="">Automatic</option>
              <option value="1.3">1.3</option><option value="3.0">3.0</option><option value="3.1">3.1</option>
              <option value="4.0">4.0</option><option value="4.1">4.1</option><option value="4.2">4.2</option>
            </select>
          </div>
          <div class="row">
            <label class="lbl w" for="cvmaxrate">Max bitrate:</label>
            <input id="cvmaxrate" type="number" min="0" step="1" placeholder="none"><span>kbps</span>
            <label class="lbl n" for="cvbuf">Buffer:</label>
            <input id="cvbuf" type="number" min="0" step="1" placeholder="auto"><span>kbps</span>
          </div>
        </fieldset>

        <fieldset>
          <legend>Picture</legend>
          <div class="row">
            <label class="lbl w" for="cvw">Fit inside:</label>
            <input id="cvw" type="number" min="0" step="2" placeholder="width"><span>×</span>
            <input id="cvh" type="number" min="0" step="2" placeholder="height"><span>pixels, empty keeps the original</span>
          </div>
          <div class="row">
            <label class="lbl w" for="cvfit">Fit method:</label>
            <select id="cvfit">
              <option value="letterbox">Keep shape, add black bars</option>
              <option value="crop">Keep shape, crop the edges</option>
              <option value="stretch">Stretch to fit</option>
              <option value="shrink">Keep shape, only shrink</option>
            </select>
          </div>
          <div class="row">
            <label class="lbl w" for="cvfps">Frame rate:</label>
            <select id="cvfps">
              <option value="">Same as source</option>
              <option value="30">Up to 30 fps</option>
              <option value="25">Up to 25 fps</option>
              <option value="24">Up to 24 fps</option>
              <option value="15">Up to 15 fps</option>
            </select>
          </div>
        </fieldset>

        <fieldset>
          <legend>Audio</legend>
          <div class="row">
            <label class="lbl w" for="cvacodec">Codec:</label>
            <select id="cvacodec">
              <option value="aac">AAC</option>
              <option value="mp3">MP3</option>
              <option value="copy">Copy original audio</option>
              <option value="none">No audio</option>
            </select>
            <label class="lbl n" for="cvabr">Bitrate:</label>
            <select id="cvabr">
              <option value="64">64 kbps</option><option value="96">96 kbps</option>
              <option value="128">128 kbps</option><option value="160">160 kbps</option>
              <option value="192">192 kbps</option><option value="256">256 kbps</option>
              <option value="320">320 kbps</option>
            </select>
          </div>
          <div class="row">
            <label class="lbl w" for="cvar">Sample rate:</label>
            <select id="cvar">
              <option value="">Automatic</option>
              <option value="44100">44.1 kHz</option>
              <option value="48000">48 kHz</option>
            </select>
            <label class="lbl n" for="cvch">Channels:</label>
            <select id="cvch">
              <option value="">Same as source</option>
              <option value="2">Stereo</option>
              <option value="1">Mono</option>
            </select>
          </div>
        </fieldset>

        <fieldset>
          <legend>Range and output</legend>
          <div class="row">
            <label class="lbl w" for="cvstart">Start at:</label>
            <input id="cvstart" class="t" type="text" placeholder="0:00" autocomplete="off">
            <label class="lbl n" for="cvend">End at:</label>
            <input id="cvend" class="t" type="text" placeholder="end" autocomplete="off">
            <span>mm:ss or hh:mm:ss</span>
          </div>
          <div class="row">
            <label class="lbl w" for="cvdir">Save to:</label>
            <input id="cvdir" type="text" placeholder="Same folder as the video" autocomplete="off">
            <button id="cvdirbrowse">Browse…</button>
            <button id="cvopen">Open folder</button>
          </div>
          <p id="cvest"></p>
        </fieldset>

        <p id="cvnote"></p>
        <div class="actions">
          <span></span>
          <button id="cvgo" class="default">Convert</button>
        </div>

        <fieldset>
          <legend>Conversions</legend>
          <div class="sunken-panel queue">
            <table class="interactive">
              <thead><tr><th>Name</th><th>Preset</th><th>Status</th><th>Progress</th><th>Result</th></tr></thead>
              <tbody id="cvjobs"></tbody>
            </table>
          </div>
          <div class="buttons">
            <button id="cvcancel">Cancel selected</button>
            <button id="cvclear">Clear finished</button>
          </div>
          <p id="cverr" class="err-line"></p>
        </fieldset>

        <fieldset>
          <legend>Details for the selected conversion</legend>
          <textarea id="cvlog" class="log" rows="6" readonly placeholder="Select a conversion above to see the ffmpeg command and its output."></textarea>
        </fieldset>
      </article>
    </section>
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
const VIDEO_FILE = /\.(mp4|m4v|mkv|webm|mov|avi|flv|ts|wmv|mpg|mpeg|3gp)$/i;

/* ---- conversion presets ------------------------------------------------ */
const CV_DEFAULT = {container:'mp4', vcodec:'h264', mode:'crf', crf:22, vbitrate:1500, size_mb:50, speed:'balanced',
  profile:'', level:'', maxrate:'', bufsize:'', refs:'', width:'', height:'', fit:'letterbox', fpsmax:'',
  acodec:'aac', abr:128, ar:'', ch:2};
const PRESETS = [
  {group:'iPod and Apple devices', items:[
    {id:'ipod5', name:'iPod Video 5G, 320×240', suffix:'iPod 5G',
     note:'Matches Apple’s spec for the iPod with video: H.264 Baseline Level 1.3 at 700 kbps (768 kbps cap), 30 fps, AAC audio. Plays on every iPod with video.',
     s:{container:'m4v', mode:'bitrate', vbitrate:700, speed:'slow', profile:'baseline', level:'1.3', maxrate:768, bufsize:2000,
        refs:1, width:320, height:240, fit:'letterbox', fpsmax:30, abr:128, ar:44100, ch:2}},
    {id:'ipod55', name:'iPod 5.5G, Classic and nano, 640×480', suffix:'iPod 640',
     note:'H.264 Baseline Level 3.0 at 1200 kbps (1500 kbps cap). For the Enhanced 5.5G, iPod classic and nano 3G to 5G. The original 5G needs a firmware update to play this size.',
     s:{container:'m4v', mode:'bitrate', vbitrate:1200, speed:'balanced', profile:'baseline', level:'3.0', maxrate:1500, bufsize:3000,
        width:640, height:480, fit:'letterbox', fpsmax:30, abr:128, ar:44100, ch:2}},
  ]},
  {group:'General', items:[
    {id:'fast1080', name:'Fast 1080p30', suffix:'1080p',
     note:'H.264 at constant quality, up to 1920×1080 and 30 fps. A good all-round copy that plays almost everywhere.',
     s:{mode:'crf', crf:22, speed:'fast', width:1920, height:1080, fit:'shrink', fpsmax:30, abr:160}},
    {id:'fast720', name:'Fast 720p30', suffix:'720p',
     note:'H.264 at constant quality, up to 1280×720 and 30 fps.',
     s:{mode:'crf', crf:22, speed:'fast', width:1280, height:720, fit:'shrink', fpsmax:30, abr:128}},
    {id:'fast480', name:'Fast 480p30', suffix:'480p',
     note:'H.264 at constant quality, up to 854×480 and 30 fps. Small and quick.',
     s:{mode:'crf', crf:23, speed:'fast', width:854, height:480, fit:'shrink', fpsmax:30, abr:128}},
    {id:'small480', name:'Small file, 480p (H.265)', suffix:'480p small',
     note:'H.265 at constant quality for much smaller files. Slower to encode, and older devices can’t play it.',
     s:{vcodec:'h265', mode:'crf', crf:28, speed:'balanced', width:854, height:480, fit:'shrink', fpsmax:30, abr:96}},
    {id:'tiny10', name:'Tiny, aim for 10 MB', suffix:'10MB',
     note:'Works out the bitrate that gets close to the target size for this video’s length. Change the number to aim for something else.',
     s:{mode:'size', size_mb:10, speed:'balanced', width:640, height:360, fit:'shrink', fpsmax:30, abr:64, ch:1}},
    {id:'remux', name:'Change container only (no re-encode)', suffix:'remux',
     note:'Copies the video and audio as they are, so it’s fast and loses no quality. Some audio, like Opus, doesn’t fit in MP4. Switch the container to MKV if that fails.',
     s:{vcodec:'copy', acodec:'copy'}},
  ]},
];
const PRESET_LIST = PRESETS.flatMap(g => g.items);
const presetById = id => PRESET_LIST.find(p => p.id === id);

/* ---- state ------------------------------------------------------------- */
let S = {kind:'video', vfmt:'mp4', afmt:'mp3', height:'', abr:'', thumb:true, meta:true, subs:false, dir:'', cvpreset:'ipod5', cvdir:''};
try { Object.assign(S, JSON.parse(localStorage.getItem('ytdlpfe') || '{}')); } catch (e) {}
const save = () => { try { localStorage.setItem('ytdlpfe', JSON.stringify(S)); } catch (e) {} };

let fetched = null, jobs = [], lastJson = '', videos = [], lastVideos = '';
let sel = {dl: null, cv: null}, tab = 'dl';
let srcInfo = null, cvMode = 'crf', cvVals = {crf:22, bitrate:1500, size:50}, cvRefs = '';
let encoderInfo = null;

async function api(path, body) {
  const opt = body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)};
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({error:'The app sent an unreadable reply.'}));
  if (!r.ok || j.error) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}
function note(msg, err) { $('note').textContent = msg; $('note').className = err ? 'err' : ''; }
function cvnote(msg, err) { $('cvnote').textContent = msg; $('cvnote').className = err ? 'err' : ''; }
function setStatus(msg) { $('sb').textContent = msg; }
function fmtSize(b) { return b >= 1e9 ? (b/1e9).toFixed(1) + ' GB' : Math.max(1, Math.round(b/1e6)) + ' MB'; }
function fmtDur(s) {
  if (!s) return '';
  s = Math.round(s);
  const h = Math.floor(s/3600), m = Math.floor(s%3600/60), x = s%60;
  return (h ? h + ':' + String(m).padStart(2,'0') : m) + ':' + String(x).padStart(2,'0');
}
function parseT(v) {
  v = String(v || '').trim();
  if (!v) return null;
  const parts = v.split(':').map(Number);
  if (parts.length > 3 || parts.some(x => isNaN(x) || x < 0)) return null;
  return parts.reduce((t, x) => t * 60 + x, 0);
}
function setSelect(sel, items, value) {
  sel.innerHTML = items.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join('');
  if (items.some(i => String(i[0]) === String(value))) sel.value = value;
}

/* ---- tabs -------------------------------------------------------------- */
function showTab(name) {
  tab = name;
  document.querySelectorAll('[role=tab]').forEach(b => b.setAttribute('aria-selected', b.dataset.tab === name ? 'true' : 'false'));
  $('tab-dl').hidden = name !== 'dl';
  $('tab-cv').hidden = name !== 'cv';
  if (name === 'cv' && !$('cvpath').value && videos.length) loadSource(videos[0].path);
  refreshLog();
}
document.querySelectorAll('[role=tab]').forEach(b => b.onclick = () => showTab(b.dataset.tab));

/* ---- download tab ------------------------------------------------------ */
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
    sel.dl = r.id; lastJson = '';
    note('Added to the download list.');
  } catch (e) {
    note(e.message, true);
  } finally {
    $('go').disabled = false;
  }
}

/* ---- convert tab ------------------------------------------------------- */
function renderPresetSelect() {
  $('cvpreset').innerHTML = PRESETS.map(g =>
    `<optgroup label="${esc(g.group)}">${g.items.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('')}</optgroup>`
  ).join('') + '<option value="custom">Custom settings</option>';
}
function renderQ() {
  $('cvmode').value = cvMode;
  $('cvq').value = cvVals[cvMode];
  $('cvqunit').textContent = {crf:'CRF, lower is better quality (18 to 28 is typical)', bitrate:'kbps', size:'MB for the whole file'}[cvMode];
}
function applyPreset(id) {
  const p = presetById(id);
  if (!p) return;
  const s = {...CV_DEFAULT, ...p.s};
  $('cvpreset').value = id;
  $('cvpnote').textContent = p.note;
  $('cvcontainer').value = s.container; $('cvvcodec').value = s.vcodec;
  cvMode = s.mode; cvVals = {crf:s.crf, bitrate:s.vbitrate, size:s.size_mb};
  $('cvspeed').value = s.speed; $('cvprofile').value = s.profile; $('cvlevel').value = s.level;
  $('cvmaxrate').value = s.maxrate; $('cvbuf').value = s.bufsize; cvRefs = s.refs;
  $('cvw').value = s.width; $('cvh').value = s.height; $('cvfit').value = s.fit; $('cvfps').value = s.fpsmax;
  $('cvacodec').value = s.acodec; $('cvabr').value = s.abr; $('cvar').value = s.ar; $('cvch').value = s.ch;
  renderQ(); updateCvState();
}
function updateCvState() {
  const copy = $('cvvcodec').value === 'copy', h264 = $('cvvcodec').value === 'h264';
  ['cvmode', 'cvq', 'cvspeed', 'cvmaxrate', 'cvbuf', 'cvw', 'cvh', 'cvfit', 'cvfps'].forEach(id => $(id).disabled = copy);
  ['cvprofile', 'cvlevel'].forEach(id => $(id).disabled = copy || !h264);
  const a = $('cvacodec').value;
  ['cvabr', 'cvar', 'cvch'].forEach(id => $(id).disabled = a === 'copy' || a === 'none');
  updateEstimate();
}
function updateEstimate() {
  const el = $('cvest');
  if (!srcInfo || !srcInfo.duration) { el.textContent = ''; return; }
  const a = parseT($('cvstart').value) || 0;
  const e = parseT($('cvend').value);
  const dur = Math.max(0, (e ? Math.min(e, srcInfo.duration) : srcInfo.duration) - a);
  if (!dur) { el.textContent = ''; return; }
  if ($('cvvcodec').value === 'copy') {
    el.textContent = 'The video isn’t re-encoded, so the result is about ' + fmtSize(srcInfo.size * dur / srcInfo.duration) + '.';
    return;
  }
  const ac = $('cvacodec').value;
  const ak = ac === 'none' ? 0 : ac === 'copy' ? (srcInfo.abr || 128) : +$('cvabr').value;
  const q = +$('cvq').value, maxrate = +$('cvmaxrate').value;
  if (cvMode === 'bitrate' && q) el.textContent = 'Estimated size: about ' + fmtSize((q + ak) * 125 * dur) + '.';
  else if (cvMode === 'size' && q) el.textContent = 'Estimated size: about ' + q + ' MB.';
  else el.textContent = 'Constant quality, so the size depends on the video.' +
    (maxrate ? ' At most about ' + fmtSize((maxrate + ak) * 125 * dur) + '.' : '');
}
function onCvEdit(e) {
  if (e.target.id === 'cvq') cvVals[cvMode] = $('cvq').value;
  $('cvpreset').value = 'custom';
  $('cvpnote').textContent = 'Custom settings.';
  updateCvState();
}
['cvcontainer', 'cvvcodec', 'cvq', 'cvspeed', 'cvprofile', 'cvlevel', 'cvmaxrate', 'cvbuf', 'cvw', 'cvh', 'cvfit',
 'cvfps', 'cvacodec', 'cvabr', 'cvar', 'cvch'].forEach(id => {
  $(id).addEventListener('input', onCvEdit);
  $(id).addEventListener('change', onCvEdit);
});
$('cvmode').onchange = () => {
  cvVals[cvMode] = $('cvq').value;
  cvMode = $('cvmode').value;
  renderQ();
  $('cvpreset').value = 'custom'; $('cvpnote').textContent = 'Custom settings.';
  updateEstimate();
};
['cvstart', 'cvend'].forEach(id => $(id).addEventListener('input', updateEstimate));
$('cvpreset').onchange = () => {
  if ($('cvpreset').value === 'custom') { $('cvpnote').textContent = 'Custom settings.'; return; }
  S.cvpreset = $('cvpreset').value; save(); applyPreset(S.cvpreset);
};
$('cvdir').onchange = () => { S.cvdir = $('cvdir').value.trim(); save(); };

function renderRecent() {
  $('cvrecent').innerHTML = '<option value="">' + (videos.length ? 'Pick a recent download…' : 'Nothing downloaded yet') + '</option>' +
    videos.map(v => `<option value="${esc(v.path)}">${esc(v.name)}</option>`).join('');
}
$('cvrecent').onchange = () => { const v = $('cvrecent').value; if (v) loadSource(v); $('cvrecent').value = ''; };

async function loadSource(path) {
  $('cvpath').value = path; srcInfo = null;
  $('cvinfo').textContent = 'Reading the file…'; cvnote('');
  try {
    const r = await api('/api/probe', {path});
    srcInfo = r;
    const parts = [r.width && r.height ? `${r.width}×${r.height}` : '', r.vcodec.toUpperCase(),
                   r.fps ? `${+r.fps.toFixed(2)} fps` : '', fmtDur(r.duration), r.size ? fmtSize(r.size) : ''].filter(Boolean);
    $('cvinfo').textContent = parts.join(', ') + (r.has_audio ? ', audio: ' + r.acodec.toUpperCase() : ', no audio');
  } catch (e) {
    $('cvinfo').textContent = '';
    cvnote(e.message, true);
  }
  updateEstimate();
}
$('cvpath').onchange = () => { const v = $('cvpath').value.trim().replace(/^"|"$/g, ''); if (v) loadSource(v); };
$('cvbrowse').onclick = async () => {
  try {
    const r = await api('/api/browse_file', {dir: $('cvdir').value.trim()});
    if (r.path) loadSource(r.path);
  } catch (e) { cvnote(e.message, true); }
};
$('cvdirbrowse').onclick = async () => {
  try {
    const r = await api('/api/browse', {dir: $('cvdir').value.trim()});
    if (r.dir) { $('cvdir').value = r.dir; S.cvdir = r.dir; save(); }
  } catch (e) { cvnote(e.message, true); }
};
$('cvopen').onclick = async () => {
  const j = jobs.find(x => x.id === sel.cv);
  const p = (j && j.output) || $('cvdir').value.trim() || $('cvpath').value.trim();
  try { await api('/api/open', {dir: p}); } catch (e) { cvnote(e.message, true); }
};

function readConvert() {
  cvVals[cvMode] = $('cvq').value;
  const num = v => (v === '' || v === null || v === undefined) ? null : +v;
  const p = presetById($('cvpreset').value);
  return {
    source: $('cvpath').value.trim().replace(/^"|"$/g, ''), outdir: $('cvdir').value.trim(),
    label: p ? p.name : 'Custom', suffix: p ? p.suffix : '',
    container: $('cvcontainer').value, vcodec: $('cvvcodec').value, mode: cvMode,
    crf: num(cvVals.crf), vbitrate: num(cvVals.bitrate), size_mb: num(cvVals.size),
    speed: $('cvspeed').value, profile: $('cvprofile').value, level: $('cvlevel').value,
    maxrate: num($('cvmaxrate').value), bufsize: num($('cvbuf').value), refs: num(cvRefs),
    width: num($('cvw').value), height: num($('cvh').value), fit: $('cvfit').value, fpsmax: num($('cvfps').value),
    acodec: $('cvacodec').value, abr: +$('cvabr').value, ar: num($('cvar').value), ch: num($('cvch').value),
    start: $('cvstart').value.trim(), end: $('cvend').value.trim(),
  };
}
$('cvgo').onclick = async () => {
  $('cvgo').disabled = true;
  try {
    const r = await api('/api/convert', readConvert());
    sel.cv = r.id; lastJson = '';
    cvnote('Added to the conversion list.');
  } catch (e) {
    cvnote(e.message, true);
  } finally {
    $('cvgo').disabled = false;
  }
};

/* ---- job lists --------------------------------------------------------- */
const KINDS = {
  dl: {kind:'download', body:'jobs', err:'joberr', log:'log', empty:'Nothing here yet. Paste a link and press Download.'},
  cv: {kind:'convert', body:'cvjobs', err:'cverr', log:'cvlog', empty:'Nothing here yet. Choose a video and press Convert.'},
};
function statusText(j) {
  const n = j.item ? ' ' + j.item : '';
  return {queued:'Waiting', downloading:'Downloading' + n, processing:'Processing' + n, converting:'Converting',
          done:'Finished', error:'Failed', canceled:'Canceled'}[j.status] || j.status;
}
function detail(j) {
  if (j.kind === 'convert' && j.status === 'done' && j.outsize) return `${fmtSize(j.insize)} to ${fmtSize(j.outsize)}`;
  return [j.speed, j.eta && j.eta + ' left'].filter(Boolean).join(', ');
}
function renderJobs() {
  for (const k of ['dl', 'cv']) {
    const K = KINDS[k], list = jobs.filter(j => j.kind === K.kind);
    $(K.body).innerHTML = list.length ? list.map(j => `
      <tr data-id="${j.id}" class="${j.id === sel[k] ? 'highlighted' : ''}" title="${esc(j.error || j.name)}">
        <td>${esc(j.name)}</td><td>${esc(j.label)}</td><td>${esc(statusText(j))}</td>
        <td class="pct"><div role="progressbar"><div style="width:${j.percent}%"></div></div></td>
        <td>${esc(detail(j))}</td>
      </tr>`).join('')
      : `<tr><td colspan="5">${K.empty}</td></tr>`;
    const s = list.find(j => j.id === sel[k]);
    $(K.err).textContent = s && s.error ? 'Why it failed: ' + s.error : '';
  }
  const d = jobs.find(j => j.id === sel.dl);
  const ready = !!(d && d.status === 'done' && d.output && VIDEO_FILE.test(d.output));
  $('toconv').disabled = !ready;
  $('jobhint').textContent = ready ? 'Finished. Press “Convert selected…” to shrink it for an iPod or another device.' : '';
}
for (const k of ['dl', 'cv']) {
  $(KINDS[k].body).onclick = e => {
    const tr = e.target.closest('tr[data-id]');
    if (!tr) return;
    sel[k] = +tr.dataset.id; $(KINDS[k].log).value = '';
    renderJobs(); refreshLog();
  };
}
$('toconv').onclick = () => {
  const j = jobs.find(x => x.id === sel.dl);
  if (!j || !j.output) return;
  showTab('cv');
  loadSource(j.output);
};

const isNetworkError = e => e instanceof TypeError && /fetch|network|load failed/i.test(e.message);

async function refreshLog() {
  const k = tab, id = sel[k];
  if (id === null || !jobs.some(x => x.id === id)) return;
  const box = $(KINDS[k].log);
  try {
    const l = await api('/api/log?id=' + id);
    const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 8;
    const text = l.log + (l.failure ? '\n\nFailed: ' + l.failure : '');
    if (box.value !== text) { box.value = text; if (atEnd) box.scrollTop = box.scrollHeight; }
  } catch (e) {
    if (!box.value) box.value = 'Couldn’t load the details: ' + e.message;
  }
}

function applyEncoders(enc) {
  const key = JSON.stringify(enc);
  if (key === encoderInfo) return;
  encoderInfo = key;
  const set = (selId, val, ok) => { const o = $(selId).querySelector(`option[value="${val}"]`); if (o) o.disabled = !ok; };
  set('cvvcodec', 'h264', enc.libx264 !== false); set('cvvcodec', 'h265', !!enc.libx265);
  set('cvacodec', 'aac', enc.aac !== false); set('cvacodec', 'mp3', !!enc.libmp3lame);
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
      if (!s.ffmpeg) msgs.push('ffmpeg wasn’t found. MP3 and other audio conversions, high-quality video merging and the Convert tab all need it. Install ffmpeg and add it to your PATH.');
      else if (!s.ffprobe) msgs.push('ffprobe wasn’t found next to ffmpeg. The Convert tab needs it to read video files.');
      else if (s.encoders.libx264 === false) msgs.push('This ffmpeg build can’t encode H.264, so the iPod presets won’t work. Install a full ffmpeg build.');
      if (!s.deno.found) msgs.push('Deno wasn’t found. YouTube downloads need it and may fail with “HTTP Error 403” without it. If you just installed it, close this terminal, open a new one and start the app again so it sees the updated PATH. Otherwise install it from deno.com (on Windows: winget install DenoLand.Deno).');
      else if (!s.deno.ok) msgs.push('Deno ' + s.deno.version + ' is too old. yt-dlp needs Deno 2.3.0 or newer. Update it with “deno upgrade”.');
    }
    $('banner').hidden = !msgs.length;
    $('banner').textContent = msgs.join(' ');
    applyEncoders(s.encoders || {});
    const v = JSON.stringify(s.videos);
    if (v !== lastVideos) { lastVideos = v; videos = s.videos; renderRecent(); }
    const j = JSON.stringify(s.jobs);
    if (j !== lastJson) { lastJson = j; jobs = s.jobs; renderJobs(); }
    const active = jobs.filter(x => ['queued', 'downloading', 'processing', 'converting'].includes(x.status)).length;
    setStatus(active ? `${active} task${active > 1 ? 's' : ''} in progress` : 'Ready');
  } catch (e) {
    console.error(e);
    setStatus(isNetworkError(e)
      ? 'Lost connection to the app. Check the terminal window that’s running it.'
      : 'Problem: ' + e.message);
  }
  await refreshLog();
  setTimeout(poll, 800);
}

/* ---- buttons ----------------------------------------------------------- */
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
$('cancel').onclick = async () => { if (sel.dl !== null) { await api('/api/cancel', {id: sel.dl}); lastJson = ''; } };
$('clear').onclick = async () => { await api('/api/clear', {kind: 'download'}); sel.dl = null; $('log').value = ''; lastJson = ''; };
$('cvcancel').onclick = async () => { if (sel.cv !== null) { await api('/api/cancel', {id: sel.cv}); lastJson = ''; } };
$('cvclear').onclick = async () => { await api('/api/clear', {kind: 'convert'}); sel.cv = null; $('cvlog').value = ''; lastJson = ''; };
$('upd').onclick = async () => {
  $('upd').disabled = true; setStatus('Updating yt-dlp…');
  try { const r = await api('/api/update', {}); note('yt-dlp is now at version ' + r.version + '.'); }
  catch (e) { note('Update failed: ' + e.message, true); }
  finally { $('upd').disabled = false; }
};
$('close').onclick = async () => {
  if (!confirm('Close yt-dlp Downloader? Downloads and conversions in progress will be canceled.')) return;
  try { await api('/api/quit', {}); } catch (e) {}
  document.body.innerHTML = '<div class="window active" style="max-width:420px;margin:80px auto"><div class="title-bar"><div class="title-bar-text">yt-dlp Downloader</div></div><div class="window-body has-space"><p>The app has closed. You can close this tab.</p></div></div>';
};

/* ---- start ------------------------------------------------------------- */
renderForm();
renderPresetSelect();
applyPreset(presetById(S.cvpreset) ? S.cvpreset : 'ipod5');
renderRecent();
if (S.dir) $('dir').value = S.dir;
if (S.cvdir) $('cvdir').value = S.cvdir;
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
    threading.Thread(target=conv_worker, daemon=True).start()

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
