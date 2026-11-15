"""Clip discovery/ordering and the combined GPS track on the stitched timeline."""
import datetime
import json
import math
import os
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction

import numpy as np

from . import gpmf, mp4


@dataclass
class Clip:
    path: str
    fps: Fraction
    nb_frames: int
    width: int
    height: int
    pix_fmt: str
    codec: str
    color_range: str
    color_space: str
    color_transfer: str
    color_primaries: str
    has_audio: bool
    gpmd_index: int            # ffmpeg stream index of the gpmd track, or -1
    creation_time: float = 0.0
    mtime: float = 0.0
    gps: list = field(default_factory=list)  # (t_local, lat, lon, alt, speed, fix, dop, utc)

    @property
    def duration(self):
        return float(self.nb_frames / self.fps)

    @property
    def name(self):
        return os.path.basename(self.path)

    @property
    def gps_start(self):
        for s in self.gps:
            if s[7]:
                return s[7] - s[0]
        return 0.0


def _ffprobe(path, ffprobe="ffprobe"):
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json", "-show_format",
             "-show_streams", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise SystemExit("ffprobe not found on PATH (or pass --ffprobe).")
    if r.returncode != 0:
        raise ValueError(r.stderr.strip() or "ffprobe failed")
    return json.loads(r.stdout)


def probe(path, ffprobe="ffprobe"):
    info = _ffprobe(path, ffprobe)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v is None:
        raise ValueError("no video stream")
    fps = Fraction(v.get("r_frame_rate", "0/1"))
    if not fps:
        fps = Fraction(v.get("avg_frame_rate", "30/1"))
    dur = float(v.get("duration") or info.get("format", {}).get("duration") or 0)
    nb = int(v["nb_frames"]) if str(v.get("nb_frames", "")).isdigit() else round(dur * fps)
    gp = next((s["index"] for s in streams if s.get("codec_tag_string") == "gpmd"), -1)
    clip = Clip(
        path=path, fps=fps, nb_frames=nb, width=int(v["width"]), height=int(v["height"]),
        pix_fmt=v.get("pix_fmt", "yuv420p"), codec=v.get("codec_name", ""),
        color_range=v.get("color_range", "tv"),
        color_space=v.get("color_space", "bt709"),
        color_transfer=v.get("color_transfer", "bt709"),
        color_primaries=v.get("color_primaries", "bt709"),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        gpmd_index=gp, mtime=os.path.getmtime(path))
    try:
        mv = mp4.parse(path)
        clip.creation_time = mv.creation_time
        tr = mv.gpmd()
        if tr is not None:
            for st, du, payload in mp4.read_samples(path, tr):
                pts = gpmf.gps_samples(payload)
                n = len(pts)
                for k, p in enumerate(pts):
                    clip.gps.append((st + du * k / n,) + p)
    except (ValueError, OSError, IndexError) as e:  # keep going without GPS
        print(f"  warning: could not read telemetry from {clip.name}: {e}")
    return clip


def discover(src, sort="meta", ffprobe="ffprobe"):
    """src: a folder (top-level *.mp4 only) or a single file. Returns ordered clips."""
    if os.path.isdir(src):
        paths = [os.path.join(src, n) for n in os.listdir(src)
                 if n.lower().endswith(".mp4") and not n.startswith(".")]
    elif os.path.isfile(src):
        paths = [src]
    else:
        raise SystemExit(f"Not found: {src}")
    clips = []
    for p in sorted(paths):
        try:
            clips.append(probe(os.path.abspath(p), ffprobe))
        except ValueError as e:
            print(f"  skipping {os.path.basename(p)}: {e}")
    if not clips:
        raise SystemExit(f"No readable .mp4 files in {src}")
    if sort == "name":
        clips.sort(key=lambda c: c.name.lower())
    elif sort == "mtime":
        clips.sort(key=lambda c: (c.mtime, c.name.lower()))
    else:  # embedded creation time, then GPS clock, then name
        clips.sort(key=lambda c: (c.creation_time, c.gps_start, c.name.lower()))
    return clips


def check_compatible(clips):
    """Return a list of human-readable differences that would break a lossless join."""
    a = clips[0]
    problems = []
    for c in clips[1:]:
        for attr in ("codec", "width", "height", "fps", "pix_fmt", "color_transfer"):
            if getattr(c, attr) != getattr(a, attr):
                problems.append(f"{c.name}: {attr} is {getattr(c, attr)}, "
                                f"but {a.name} has {getattr(a, attr)}")
    return problems


def merc(lat, lon):
    """Web-Mercator, normalised so the whole world is [0,1) x [0,1)."""
    lat = np.clip(np.asarray(lat, dtype=np.float64), -85.0511, 85.0511)
    x = (np.asarray(lon, dtype=np.float64) + 180.0) / 360.0
    y = (1.0 - np.arcsinh(np.tan(np.radians(lat))) / math.pi) / 2.0
    return x, y


class GpsTrack:
    def __init__(self, clips, min_fix=2, max_dop=10.0):
        rows = []
        self.total = 0
        frames = 0
        for c in clips:
            off = float(frames / c.fps)
            for (t, lat, lon, alt, spd, fix, dop, utc) in c.gps:
                self.total += 1
                if fix < min_fix or dop > max_dop or abs(lat) > 90 or abs(lon) > 180:
                    continue
                if lat == 0 and lon == 0:
                    continue
                rows.append((off + t, lat, lon, alt, spd, utc or 0.0))
            frames += c.nb_frames
        self.duration = float(frames / clips[0].fps)
        a = np.array(rows, dtype=np.float64).reshape(-1, 6)
        if len(a):
            a = a[np.argsort(a[:, 0], kind="stable")]
        self.t, self.lat, self.lon, self.alt, self.speed, self.utc = a.T
        self.mx, self.my = merc(self.lat, self.lon)

    def __len__(self):
        return len(self.t)

    def at(self, t):
        return float(np.interp(t, self.t, self.lat)), float(np.interp(t, self.t, self.lon))

    def distance_km(self):
        if len(self) < 2:
            return 0.0
        la, lo = np.radians(self.lat), np.radians(self.lon)
        dlat, dlon = np.diff(la), np.diff(lo)
        h = np.sin(dlat / 2) ** 2 + np.cos(la[:-1]) * np.cos(la[1:]) * np.sin(dlon / 2) ** 2
        return float(np.sum(2 * 6371.0 * np.arcsin(np.sqrt(h))))


def _iso(utc):
    return datetime.datetime.fromtimestamp(utc, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def write_gpx(track, path, every=1.0):
    """One point per `every` seconds keeps the file light enough for any map tool."""
    with open(path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<gpx version="1.1" creator="gopromap" '
                'xmlns="http://www.topografix.com/GPX/1/1">\n<trk><name>GoPro</name><trkseg>\n')
        last = -1e9
        for i in range(len(track)):
            if track.t[i] - last < every:
                continue
            last = track.t[i]
            f.write(f'<trkpt lat="{track.lat[i]:.7f}" lon="{track.lon[i]:.7f}">'
                    f'<ele>{track.alt[i]:.1f}</ele>')
            if track.utc[i]:
                f.write(f'<time>{_iso(track.utc[i])}</time>')
            f.write('</trkpt>\n')
        f.write('</trkseg></trk>\n</gpx>\n')


def write_csv(track, path):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("video_seconds,utc,lat,lon,alt_m,speed_mps\n")
        for i in range(len(track)):
            utc = _iso(track.utc[i]) if track.utc[i] else ""
            f.write(f"{track.t[i]:.3f},{utc},{track.lat[i]:.7f},{track.lon[i]:.7f},"
                    f"{track.alt[i]:.1f},{track.speed[i]:.2f}\n")
