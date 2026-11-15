"""Builds small synthetic "GoPro-like" clips for testing without a camera.

Each clip is 10-bit full-range BT.709 HEVC + AAC + a gpmd telemetry track that
contains a GPS9 stream (the HERO11+ layout), following a made-up loop near the
Oregon Dunes.

    python tests/make_fixture.py <out_dir> [--clips 2] [--seconds 6] [--size 1920x1080]
"""
import argparse
import math
import os
import struct
import subprocess

FPS = "24000/1001"
EPOCH_2000 = 946684800


def box(typ, payload):
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def klv(key, typ, size, data):
    rep = len(data) // size if size else 0
    pad = (-len(data)) % 4
    return key + struct.pack(">BBH", ord(typ) if typ else 0, size, rep) + data + b"\0" * pad


def route(u):
    """u in [0,1] -> (lat, lon)."""
    a = 2 * math.pi * u
    lat = 43.5394 + 0.0035 * math.sin(a) + 0.0012 * math.sin(3 * a)
    lon = -124.2407 + 0.0060 * math.cos(a) - 0.0015 * math.cos(2 * a) - 0.0045
    return lat, lon


def payload(t0, total, utc0, nofix=False):
    samples = b""
    for k in range(10):
        t = t0 + k * 0.1001
        lat, lon = route(t / total)
        utc = utc0 + t
        days = int((utc - EPOCH_2000) // 86400)
        secs = (utc - EPOCH_2000) - days * 86400
        samples += struct.pack(">7i2H", round(lat * 1e7), round(lon * 1e7), 12000, 8000, 800,
                               days, round(secs * 1000), 150, 0 if nofix else 3)
    strm = (klv(b"STNM", "c", 1, b"GPS (Lat., Long., Alt., 2D, 3D, days, secs, DOP, fix)")
            + klv(b"TYPE", "c", 1, b"lllllllSS")
            + klv(b"SCAL", "l", 4, struct.pack(">9i", 10**7, 10**7, 1000, 1000, 100, 1, 1000,
                                                100, 1))
            + klv(b"GPS9", "?", 32, samples))
    accl = klv(b"STRM", "", 1, klv(b"SCAL", "s", 2, struct.pack(">h", 417))
               + klv(b"ACCL", "s", 6, struct.pack(">6h", 1, 2, 3, 4, 5, 6)))
    return klv(b"DEVC", "", 1, klv(b"DVID", "L", 4, struct.pack(">I", 1))
               + klv(b"DVNM", "c", 1, b"HERO13 Black") + accl + klv(b"STRM", "", 1, strm))


def gpmd_mp4(path, payloads, creation):
    ts, delta = 1000, 1001
    dur = delta * len(payloads)
    ct = creation + 2082844800
    ftyp = box(b"ftyp", b"mp41" + struct.pack(">I", 0x20130000) + b"mp41")
    off, offsets = len(ftyp) + 8, []
    for p in payloads:
        offsets.append(off)
        off += len(p)
    mdat = box(b"mdat", b"".join(payloads))
    matrix = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    mvhd = box(b"mvhd", struct.pack(">IIIIIIH10x", 0, ct, ct, ts, dur, 0x10000, 0x100)
               + matrix + b"\0" * 24 + struct.pack(">I", 2))
    tkhd = box(b"tkhd", struct.pack(">IIIIII8xHHHH", 7, ct, ct, 1, 0, dur, 0, 0, 0, 0)
               + matrix + struct.pack(">II", 0, 0))
    mdhd = box(b"mdhd", struct.pack(">IIIIIHH", 0, ct, ct, ts, dur, 0x55C4, 0))
    hdlr = box(b"hdlr", struct.pack(">II4s12x", 0, 0, b"meta") + b"GoPro MET\0")
    stsd = box(b"stsd", struct.pack(">II", 0, 1)
               + box(b"gpmd", b"\0" * 6 + struct.pack(">H", 1) + b"\0" * 4))
    n = len(payloads)
    stbl = box(b"stbl", stsd
               + box(b"stts", struct.pack(">IIII", 0, 1, n, delta))
               + box(b"stsc", struct.pack(">IIIII", 0, 1, 1, 1, 1))
               + box(b"stsz", struct.pack(">III", 0, 0, n)
                     + struct.pack(">%dI" % n, *[len(p) for p in payloads]))
               + box(b"stco", struct.pack(">II", 0, n) + struct.pack(">%dI" % n, *offsets)))
    dinf = box(b"dinf", box(b"dref", struct.pack(">II", 0, 1)
                             + box(b"url ", struct.pack(">I", 1))))
    minf = box(b"minf", box(b"nmhd", struct.pack(">I", 0)) + dinf + stbl)
    moov = box(b"moov", mvhd + box(b"trak", tkhd + box(b"mdia", mdhd + hdlr + minf)))
    with open(path, "wb") as f:
        f.write(ftyp + mdat + moov)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--clips", type=int, default=2)
    ap.add_argument("--seconds", type=int, default=6)
    ap.add_argument("--size", default="1920x1080")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    tmp = os.path.join(a.out, "_tmp")
    os.makedirs(tmp, exist_ok=True)
    total = a.clips * a.seconds * 1.001
    utc0 = 1788816995  # 2026-09-07 21:36:35 UTC
    for i in range(a.clips):
        av = os.path.join(tmp, f"av{i}.mp4")
        gp = os.path.join(tmp, f"gp{i}.mp4")
        frames = a.seconds * 24
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", f"testsrc2=s={a.size}:r={FPS}",
             "-f", "lavfi", "-i", f"sine=frequency={440 + 220 * i}:sample_rate=48000",
             "-frames:v", str(frames), "-t", str(frames * 1001 / 24000),
             "-vf", f"setpts=PTS+{i * frames}/({FPS})/TB,drawtext=text='clip {i} %{{n}}':"
                    "x=40:y=h-120:fontsize=64:fontcolor=white:box=1:boxcolor=black,"
                    "setpts=PTS-STARTPTS,scale=out_range=full:out_color_matrix=bt709,"
                    "format=yuv420p10le",
             "-c:v", "libx265", "-preset", "ultrafast", "-crf", "20", "-tag:v", "hvc1",
             "-x265-params", "range=full:colorprim=bt709:transfer=bt709:colormatrix=bt709:"
                             "log-level=error",
             "-color_range", "pc", "-colorspace", "bt709", "-color_primaries", "bt709",
             "-color_trc", "bt709", "-c:a", "aac", "-b:a", "192k", "-ac", "2", av], check=True)
        t0 = i * a.seconds * 1.001
        pls = [payload(t0 + s * 1.001, total, utc0, nofix=(i == 0 and s == 0))
               for s in range(a.seconds)]
        gpmd_mp4(gp, pls, utc0 + int(t0))
        out = os.path.join(a.out, f"GX0{i + 1}0111.MP4")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", av, "-i", gp,
             "-map", "0:v", "-map", "0:a", "-map", "1:0", "-c", "copy", "-copy_unknown",
             "-tag:d", "gpmd", "-metadata",
             "creation_time=2026-09-07T21:%02d:35Z" % (36 + i), out], check=True)
        print("wrote", out)


if __name__ == "__main__":
    main()
