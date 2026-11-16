"""Command line interface: info | stitch | gps | preview | render."""
import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile

from .overlay import HDR_TRANSFERS, Renderer, Style, to_yuva420
from .tiles import PRESETS, TileSource
from .track import GpsTrack, check_compatible, discover, write_csv, write_gpx


# ---- helpers -------------------------------------------------------------
def parse_time(s):
    """'90', '1:30', '0:01:30.5' -> seconds."""
    sec = 0.0
    for part in s.strip().split(":"):
        sec = sec * 60 + float(part)
    return sec


def fraction(s):
    """'0.2' or '20%' -> 0.2 (argparse type for sizes relative to video height)."""
    try:
        v = float(s[:-1]) / 100 if s.endswith("%") else float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a fraction like 0.2 (or 20%), got {s!r}")
    if not 0 < v <= 1:
        raise argparse.ArgumentTypeError(
            f"expected a fraction of the video height between 0 and 1 (0.2 = 20%), got {s!r}")
    return v


def fmt_time(sec):
    sec = int(round(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def out_dir(src):
    base = src if os.path.isdir(src) else os.path.dirname(os.path.abspath(src))
    d = os.path.join(base, "output")
    os.makedirs(d, exist_ok=True)
    return d


def base_name(src):
    p = os.path.abspath(src)
    return os.path.basename(p) if os.path.isdir(p) else os.path.splitext(os.path.basename(p))[0]


def guard_output(out, clips):
    o = os.path.normcase(os.path.abspath(out))
    if any(o == os.path.normcase(c.path) for c in clips):
        raise SystemExit(f"Refusing to overwrite a source file: {out}")
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)


def concat_list(clips, tmpdir, durations=False):
    p = os.path.join(tmpdir, "concat.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("ffconcat version 1.0\n")
        for c in clips:
            f.write("file '%s'\n" % c.path.replace("\\", "/").replace("'", "'\\''"))
            if durations:  # lets ffmpeg seek straight to any clip (fast previews/samples)
                f.write(f"duration {c.duration:.6f}\n")
    return p


def input_args(clips, tmpdir):
    if len(clips) == 1:
        return ["-i", clips[0].path]
    return ["-f", "concat", "-safe", "0", "-i", concat_list(clips, tmpdir, durations=True)]


def video_start_offset(args, inp):
    """Seconds between the input's overall start and its first video frame.

    ffmpeg's -ss counts from the earliest stream (often audio, which can lead the
    video by a few ms), so frame-exact seeking has to add this back.
    """
    try:
        r = subprocess.run([args.ffprobe, "-v", "error"] + inp + [
            "-select_streams", "v:0", "-show_entries", "format=start_time:stream=start_time",
            "-of", "json"], capture_output=True, text=True)
        j = json.loads(r.stdout)
        return float(j["streams"][0]["start_time"]) - float(j["format"]["start_time"])
    except (ValueError, KeyError, IndexError, OSError):
        return 0.0


def creation_meta(clips):
    ct = clips[0].creation_time
    if ct <= 0:
        return []
    iso = datetime.datetime.fromtimestamp(ct, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000000Z")
    return ["-metadata", f"creation_time={iso}"]


def load(args):
    print(f"Scanning {args.source} ...")
    clips = discover(args.source, args.sort, args.ffprobe)
    track = GpsTrack(clips, max_dop=args.max_dop)
    return clips, track


def print_summary(clips, track):
    t = 0.0
    print(f"\n  #  {'file':<28} {'start':>8} {'length':>8}  GPS samples")
    for i, c in enumerate(clips, 1):
        print(f" {i:>2}  {c.name:<28} {fmt_time(t):>8} {fmt_time(c.duration):>8}  {len(c.gps)}")
        t += c.duration
    c = clips[0]
    print(f"\n  Video : {c.width}x{c.height} {c.codec} {c.pix_fmt} @ {float(c.fps):.3f} fps, "
          f"range={c.color_range} matrix={c.color_space} transfer={c.color_transfer}")
    print(f"  Total : {fmt_time(t)} across {len(clips)} file(s)")
    if len(track):
        pct = 100.0 * len(track) / max(track.total, 1)
        print(f"  GPS   : {len(track)} usable fixes of {track.total} ({pct:.0f}%), "
              f"first at {fmt_time(track.t[0])}, route {track.distance_km():.2f} km")
        la, lo = track.at(track.t[0])
        print(f"          starts near {la:.5f}, {lo:.5f}")
    else:
        print(f"  GPS   : no usable fixes ({track.total} raw samples). Was GPS on and locked?")
    for p in check_compatible(clips):
        print(f"  WARNING (lossless join may fail): {p}")
    print()


# ---- stitch --------------------------------------------------------------
def cmd_stitch(args):
    clips, track = load(args)
    print_summary(clips, track)
    out = args.output or os.path.join(out_dir(args.source), base_name(args.source) + "_stitched.mp4")
    guard_output(out, clips)
    with tempfile.TemporaryDirectory() as tmp:
        head = [args.ffmpeg, "-hide_banner", "-loglevel", "warning", "-stats", "-y",
                "-f", "concat", "-safe", "0", "-i", concat_list(clips, tmp),
                "-map", "0:v:0", "-map", "0:a:0?"]
        tail = ["-c", "copy", "-map_metadata", "0"] + creation_meta(clips) + [out]
        gp = clips[0].gpmd_index
        attempts = []
        if gp >= 0 and all(c.gpmd_index == gp for c in clips):
            attempts.append(head + ["-map", f"0:{gp}", "-copy_unknown", "-tag:d", "gpmd"] + tail)
        attempts.append(head + tail)
        for i, cmd in enumerate(attempts):
            keeps_gps = "gpmd" in cmd
            print("Stitching (stream copy, no re-encode)"
                  + (" keeping the GoPro telemetry track ..." if keeps_gps else " ..."))
            if subprocess.run(cmd).returncode == 0:
                if not keeps_gps:
                    print("Note: the telemetry (GPS) track was not carried into the stitched "
                          "file.\n      Run 'render'/'gps' on the original folder, not the "
                          "stitched file.")
                print(f"\nDone: {out}")
                return 0
            if i + 1 < len(attempts):
                print("ffmpeg could not copy the telemetry track; retrying without it.")
    raise SystemExit("ffmpeg failed; see messages above.")


# ---- gps export ------------------------------------------------------------
def cmd_gps(args):
    clips, track = load(args)
    print_summary(clips, track)
    if not len(track):
        raise SystemExit("Nothing to export.")
    out = args.output or os.path.join(out_dir(args.source), base_name(args.source) + ".gpx")
    guard_output(out, clips)
    if out.lower().endswith(".csv"):
        write_csv(track, out)
    else:
        write_gpx(track, out)
    print(f"Wrote {out}")
    return 0


# ---- overlay rendering -----------------------------------------------------
def make_style(args, clip):
    h = clip.height
    shape = args.shape or ("circle" if args.style == "map" else "square")
    size = int(round(h * args.size))
    k = size / 432.0
    return Style(style=args.style, view=args.view, shape=shape, size=size,
                 opacity=args.opacity, zoom=args.zoom,
                 line_width=args.line_width or max(2.0, 5.0 * k),
                 color=args.color, rest_color=args.rest_color,
                 marker_color=args.marker_color, marker_radius=max(4.0, 9.0 * k),
                 border=max(1.0, 3.0 * k), backdrop=args.backdrop,
                 coords=not args.no_coords, font=args.font or "", font_size=args.font_size)


def encoder_args(args, clip, depth):
    cr = clip.color_range if clip.color_range in ("pc", "tv") else "tv"
    tags = ["-color_range", cr, "-colorspace", clip.color_space,
            "-color_primaries", clip.color_primaries, "-color_trc", clip.color_transfer,
            "-tag:v", "hvc1", "-g", str(round(float(clip.fps)))]
    if args.encoder == "libx265":
        q = args.quality if args.quality is not None else 18
        x = (f"range={'full' if cr == 'pc' else 'limited'}:colorprim={clip.color_primaries}:"
             f"transfer={clip.color_transfer}:colormatrix={clip.color_space}")
        enc = ["-c:v", "libx265", "-preset", args.preset or "medium", "-crf", str(q),
               "-pix_fmt", "yuv420p10le" if depth == 10 else "yuv420p", "-x265-params", x]
    else:  # hevc_nvenc
        q = args.quality if args.quality is not None else 21
        enc = ["-c:v", "hevc_nvenc", "-preset", args.preset or "p6", "-tune", "hq",
               "-profile:v", "main10" if depth == 10 else "main",
               "-pix_fmt", "p010le" if depth == 10 else "yuv420p", "-spatial-aq", "1"]
        if args.bitrate:
            enc += ["-rc", "vbr", "-b:v", args.bitrate, "-maxrate", args.bitrate,
                    "-bufsize", args.bitrate]
        else:
            enc += ["-rc", "vbr", "-cq", str(q), "-b:v", "0", "-maxrate", "150M",
                    "-bufsize", "300M"]
    return enc + tags + (args.encoder_args.split() if args.encoder_args else [])


def position_xy(args, clip, pw, ph):
    m = args.margin if args.margin is not None else int(round(clip.height * 0.03))
    x = m if "l" in args.position else clip.width - pw - m
    y = m if "t" in args.position else clip.height - ph - m
    return x - x % 2, y - y % 2


def run_overlay(args, clips, track, segments, out, still=False):
    """Composite the map over frames [f0,f1) for each segment (stitched frame numbers)."""
    c0 = clips[0]
    fps = c0.fps
    depth = 10 if "10" in c0.pix_fmt else 8
    if "p" not in c0.pix_fmt or "420" not in c0.pix_fmt:
        print(f"  warning: unexpected pixel format {c0.pix_fmt}; output will be 4:2:0")
    if c0.color_transfer in HDR_TRANSFERS:
        print(f"  HDR footage ({c0.color_transfer}): overlay graphics are mapped into "
              "BT.2020 at reference white.")
    st = make_style(args, c0)
    tiles = TileSource(args.tiles, args.tile_cache) if st.style == "map" else None
    rend = Renderer(track, st, tiles)
    pw, ph = rend.panel_w, rend.panel_h
    x, y = position_xy(args, c0, pw, ph)
    full = c0.color_range == "pc"

    first, last = segments[0][0], segments[-1][1]
    total = sum(c.nb_frames for c in clips)
    whole = len(segments) == 1 and first == 0 and last >= total
    cmd = [args.ffmpeg, "-hide_banner", "-loglevel", "warning", "-y"]
    if not still:
        cmd += ["-stats"]
    if args.hwaccel:
        cmd += ["-hwaccel", args.hwaccel]
    with tempfile.TemporaryDirectory() as tmp:
        inp = input_args(clips, tmp)
        if first > 0:
            ss = video_start_offset(args, inp) + float((first - 0.5) / fps)
            cmd += ["-ss", f"{ss:.6f}"]
        if not whole:
            cmd += ["-t", f"{float((last - first - (0 if first else 0.5)) / fps):.6f}"]
        cmd += inp
        cmd += ["-f", "rawvideo", "-pix_fmt", "yuva420p10le" if depth == 10 else "yuva420p",
                "-s", f"{pw}x{ph}", "-framerate", f"{fps.numerator}/{fps.denominator}",
                "-i", "pipe:0"]
        multi = len(segments) > 1
        main = "[0:v]"
        fc = ""
        if multi:
            sel = "+".join(f"between(n,{a - first},{b - first - 1})" for a, b in segments)
            fc += f"[0:v]select='{sel}',setpts=N/FRAME_RATE/TB[m];"
            main = "[m]"
        fc += (f"[1:v]setparams=range={'pc' if full else 'tv'}:colorspace={c0.color_space}[ov];"
               f"{main}[ov]overlay={x}:{y}:format={'yuv420p10' if depth == 10 else 'yuv420'}"
               f":eof_action=repeat[v]")
        if still:
            cmd += ["-filter_complex", fc, "-map", "[v]", "-frames:v", "1", "-update", "1",
                    "-pix_fmt", "rgb24", out]
        else:
            audio = []
            if c0.has_audio and multi:
                asel = "+".join(f"between(t,{float((a - first) / fps):.6f},"
                                f"{float((b - first) / fps):.6f})" for a, b in segments)
                fc += f";[0:a:0]aselect='{asel}',asetpts=N/SR/TB[a]"
                audio = ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
            elif c0.has_audio:
                audio = ["-map", "0:a:0", "-c:a", "copy"]
            cmd += ["-filter_complex", fc, "-map", "[v]"] + audio
            cmd += encoder_args(args, c0, depth)
            cmd += ["-fps_mode", "passthrough", "-map_metadata", "0"] + creation_meta(clips) + [out]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        except FileNotFoundError:
            raise SystemExit("ffmpeg not found on PATH (or pass --ffmpeg).")
        try:
            for a, b in segments:
                for n in range(a, b):
                    P, A = rend.frame(float(n / fps))
                    proc.stdin.write(to_yuva420(P, A, depth, c0.color_space, full, c0.color_transfer))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass  # ffmpeg exited early; its own message explains why
        except KeyboardInterrupt:
            proc.kill()
            raise SystemExit("\nInterrupted.")
        if proc.wait() != 0:
            raise SystemExit("ffmpeg failed; see messages above.")


def segments_from_args(args, clips):
    fps = clips[0].fps
    total = sum(c.nb_frames for c in clips)
    spans = []
    for k in args.keep or []:
        for part in k.split(","):
            a, _, b = part.partition("-")
            spans.append((parse_time(a), parse_time(b) if b.strip() else total / fps))
    if args.start is not None or args.duration is not None:
        s = parse_time(args.start) if args.start is not None else 0.0
        e = s + parse_time(args.duration) if args.duration is not None else total / fps
        spans.append((s, e))
    if not spans:
        return [(0, total)]
    segs = []
    for s, e in sorted(spans):
        a, b = max(0, round(s * fps)), min(total, round(e * fps))
        if b <= a:
            continue
        if segs and a <= segs[-1][1]:
            segs[-1] = (segs[-1][0], max(segs[-1][1], b))
        else:
            segs.append((a, b))
    if not segs:
        raise SystemExit("--keep/--start/--duration select nothing inside the footage.")
    return segs


def cmd_render(args):
    clips, track = load(args)
    print_summary(clips, track)
    segs = segments_from_args(args, clips)
    out = args.output or os.path.join(out_dir(args.source), base_name(args.source) + "_map.mp4")
    guard_output(out, clips)
    fps = clips[0].fps
    kept = sum(b - a for a, b in segs)
    print(f"Rendering {fmt_time(kept / fps)} of video ({kept} frames, "
          f"{len(segs)} segment(s)) with {args.encoder} ...")
    run_overlay(args, clips, track, segs, out)
    print(f"\nDone: {out}")
    return 0


def cmd_preview(args):
    clips, track = load(args)
    print_summary(clips, track)
    fps = clips[0].fps
    total = sum(c.nb_frames for c in clips)
    times = [parse_time(t) for t in args.at] if args.at else [
        track.duration * f for f in (0.25, 0.5, 0.75)]
    d = args.output or out_dir(args.source)
    os.makedirs(d, exist_ok=True)
    for t in times:
        n = min(total - 1, max(0, round(t * fps)))
        out = os.path.join(d, f"preview_{args.style}_{args.view}_{fmt_time(t).replace(':', '')}.png")
        run_overlay(args, clips, track, [(n, n + 1)], out, still=True)
        print(f"Wrote {out}")
    return 0


def cmd_info(args):
    clips, track = load(args)
    print_summary(clips, track)
    return 0


# ---- argument parsing ------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="gopromap",
        description="Stitch GoPro chapter files losslessly and burn in a GPS mini-map "
                    "without touching bit depth or colour.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("source", help="folder containing the GoPro .MP4 files (or one file)")
        sp.add_argument("--sort", choices=["meta", "mtime", "name"], default="meta",
                        help="clip order: embedded creation time (default), file modified "
                             "time, or file name")
        sp.add_argument("--max-dop", type=float, default=10.0, metavar="NUMBER",
                        help="ignore GPS samples with dilution of precision above this "
                             "(default 10; lower = stricter)")
        sp.add_argument("--ffmpeg", default="ffmpeg")
        sp.add_argument("--ffprobe", default="ffprobe")

    def look(sp):
        g = sp.add_argument_group("overlay look")
        g.add_argument("--style", choices=["map", "path"], default="map",
                       help="map = basemap tiles + route; path = route line only")
        g.add_argument("--view", choices=["fit", "follow"], default="fit",
                       help="fit = whole route visible; follow = zoomed in, map moves")
        g.add_argument("--zoom", type=float, default=16.0, metavar="LEVEL",
                       help="web-map zoom level for --view follow; decimals allowed, "
                            "+1 = twice as close (default 16)")
        g.add_argument("--shape", choices=["circle", "rounded", "square"],
                       help="default: circle for map, square for path")
        g.add_argument("--size", type=fraction, default=0.2, metavar="FRACTION",
                       help="map diameter/side as a fraction of video height: 0.2 or 20%% "
                            "(default 0.2)")
        g.add_argument("--opacity", type=float, default=0.8, metavar="0..1",
                       help="overlay opacity, 0 = invisible, 1 = solid (default 0.8)")
        g.add_argument("--position", choices=["br", "bl", "tr", "tl"], default="br")
        g.add_argument("--margin", type=int, metavar="PX",
                       help="gap to the frame edge in video pixels (default 3%% of height)")
        g.add_argument("--tiles", default="osm",
                       help=f"basemap: {', '.join(PRESETS)}, or a URL template with "
                            "{z} {x} {y}")
        g.add_argument("--tile-cache", metavar="FOLDER", help="folder for downloaded map tiles")
        g.add_argument("--color", default="#19a7ff", metavar="COLOUR",
                       help="travelled route: #RRGGBB hex or a CSS name like 'orange'")
        g.add_argument("--rest-color", default="#ffffff", metavar="COLOUR",
                       help="route still ahead (same formats)")
        g.add_argument("--marker-color", default="#ff3b30", metavar="COLOUR",
                       help="position dot (same formats)")
        g.add_argument("--line-width", type=float, metavar="PX",
                       help="route line thickness in video pixels (default scales with --size)")
        g.add_argument("--backdrop", type=float, default=0.0, metavar="0..1",
                       help="path style: darkness of a panel behind the line (default 0 = none)")
        g.add_argument("--no-coords", action="store_true", help="hide the lat/lon text")
        g.add_argument("--font", metavar="FILE", help="path to a .ttf/.otf font for the text")
        g.add_argument("--font-size", type=int, default=0, metavar="PX",
                       help="text height in video pixels (default 0 = fit to map width)")
        sp.add_argument("--hwaccel", metavar="NAME",
                        help="ffmpeg decode acceleration method, e.g. cuda")

    sp = sub.add_parser("info", help="list clips in stitch order and summarise the GPS data")
    common(sp)
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("stitch", help="join the clips into one file with no re-encode")
    common(sp)
    sp.add_argument("-o", "--output")
    sp.set_defaults(func=cmd_stitch)

    sp = sub.add_parser("gps", help="export the GPS track (.gpx, or .csv by extension)")
    common(sp)
    sp.add_argument("-o", "--output")
    sp.set_defaults(func=cmd_gps)

    sp = sub.add_parser("preview", help="write still frames with the overlay, to tune the look")
    common(sp)
    look(sp)
    sp.add_argument("--at", action="append", metavar="TIME",
                    help="time as seconds, M:SS or H:MM:SS, e.g. 1:30 (repeatable). "
                    "Default: 25%%, 50%%, 75%% through")
    sp.add_argument("-o", "--output", help="output folder")
    sp.set_defaults(func=cmd_preview)

    sp = sub.add_parser("render", help="stitch + overlay (+ optional trimming) in one encode")
    common(sp)
    look(sp)
    sp.add_argument("-o", "--output")
    sp.add_argument("--keep", action="append", metavar="START-END",
                    help="keep only this time range of the stitched timeline, e.g. "
                         "0:30-2:10 (repeatable, or comma-separated)")
    sp.add_argument("--start", metavar="TIME",
                    help="render a sample starting here: seconds, M:SS or H:MM:SS")
    sp.add_argument("--duration", metavar="TIME", help="sample length, same formats, e.g. 20")
    sp.add_argument("--encoder", choices=["hevc_nvenc", "libx265"], default="hevc_nvenc")
    sp.add_argument("--quality", type=float,
                    help="NVENC -cq (default 21) or x265 -crf (default 18); lower = better")
    sp.add_argument("--bitrate", help="NVENC target bitrate instead of --quality, e.g. 45M")
    sp.add_argument("--preset", help="encoder preset (NVENC p1..p7, x265 names)")
    sp.add_argument("--encoder-args", help="extra ffmpeg output options, quoted")
    sp.set_defaults(func=cmd_render)
    return p


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    return args.func(args)
