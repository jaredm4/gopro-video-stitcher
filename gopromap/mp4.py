"""Minimal MP4 reader: just enough to pull GoPro's 'gpmd' telemetry track.

Only the moov box is loaded into memory; sample payloads are read by seeking,
so multi-GB files are fine.
"""
import struct
from dataclasses import dataclass, field

MAC_EPOCH = 2082844800  # seconds between 1904-01-01 and 1970-01-01


def _iter_boxes(buf, start, end):
    pos = start
    while pos + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, pos)
        hdr = 8
        if size == 1:
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            break
        yield typ, pos + hdr, pos + size
        pos += size


def _read_moov(f):
    f.seek(0, 2)
    fsize = f.tell()
    pos = 0
    while pos + 8 <= fsize:
        f.seek(pos)
        hdr = f.read(16)
        size, typ = struct.unpack_from(">I4s", hdr)
        hlen = 8
        if size == 1:
            size = struct.unpack_from(">Q", hdr, 8)[0]
            hlen = 16
        elif size == 0:
            size = fsize - pos
        if size < hlen:
            break
        if typ == b"moov":
            f.seek(pos + hlen)
            return f.read(size - hlen)
        pos += size
    raise ValueError("no moov box found (not an MP4, or file is truncated)")


@dataclass
class Track:
    handler: str = ""
    fmt: str = ""
    timescale: int = 1
    stts: list = field(default_factory=list)   # (count, delta)
    stsc: list = field(default_factory=list)   # (first_chunk, samples_per_chunk)
    sizes: list = field(default_factory=list)
    chunk_offsets: list = field(default_factory=list)

    def samples(self):
        """-> list of (file_offset, size, start_seconds, duration_seconds)."""
        times = []
        t = 0
        for count, delta in self.stts:
            for _ in range(count):
                times.append((t, delta))
                t += delta
        out = []
        si = 0
        n = min(len(self.sizes), len(times))
        entry = 0
        for ci, coff in enumerate(self.chunk_offsets, 1):
            while entry + 1 < len(self.stsc) and self.stsc[entry + 1][0] <= ci:
                entry += 1
            spc = self.stsc[entry][1] if self.stsc else 1
            off = coff
            for _ in range(spc):
                if si >= n:
                    return out
                st, du = times[si]
                out.append((off, self.sizes[si], st / self.timescale, du / self.timescale))
                off += self.sizes[si]
                si += 1
        return out


@dataclass
class Movie:
    creation_time: float = 0.0   # unix seconds (as written by the camera)
    tracks: list = field(default_factory=list)

    def gpmd(self):
        for t in self.tracks:
            if t.fmt == "gpmd":
                return t
        return None


def _parse_trak(buf, s, e):
    tr = Track()

    def walk(s, e):
        for typ, bs, be in _iter_boxes(buf, s, e):
            if typ in (b"mdia", b"minf", b"stbl"):
                walk(bs, be)
            elif typ == b"hdlr":
                tr.handler = buf[bs + 8:bs + 12].decode("latin1")
            elif typ == b"mdhd":
                if buf[bs] == 1:
                    tr.timescale = struct.unpack_from(">I", buf, bs + 20)[0]
                else:
                    tr.timescale = struct.unpack_from(">I", buf, bs + 12)[0]
            elif typ == b"stsd":
                if be - bs >= 16:
                    tr.fmt = buf[bs + 12:bs + 16].decode("latin1")
            elif typ == b"stts":
                n = struct.unpack_from(">I", buf, bs + 4)[0]
                v = struct.unpack_from(">%dI" % (2 * n), buf, bs + 8)
                tr.stts = list(zip(v[0::2], v[1::2]))
            elif typ == b"stsc":
                n = struct.unpack_from(">I", buf, bs + 4)[0]
                v = struct.unpack_from(">%dI" % (3 * n), buf, bs + 8)
                tr.stsc = list(zip(v[0::3], v[1::3]))
            elif typ == b"stsz":
                fixed, n = struct.unpack_from(">II", buf, bs + 4)
                if fixed:
                    tr.sizes = [fixed] * n
                else:
                    tr.sizes = list(struct.unpack_from(">%dI" % n, buf, bs + 12))
            elif typ == b"stco":
                n = struct.unpack_from(">I", buf, bs + 4)[0]
                tr.chunk_offsets = list(struct.unpack_from(">%dI" % n, buf, bs + 8))
            elif typ == b"co64":
                n = struct.unpack_from(">I", buf, bs + 4)[0]
                tr.chunk_offsets = list(struct.unpack_from(">%dQ" % n, buf, bs + 8))

    walk(s, e)
    tr.timescale = tr.timescale or 1
    return tr


def parse(path):
    with open(path, "rb") as f:
        moov = _read_moov(f)
    mv = Movie()
    for typ, bs, be in _iter_boxes(moov, 0, len(moov)):
        if typ == b"mvhd":
            if moov[bs] == 1:
                ct = struct.unpack_from(">Q", moov, bs + 4)[0]
            else:
                ct = struct.unpack_from(">I", moov, bs + 4)[0]
            mv.creation_time = float(ct - MAC_EPOCH) if ct else 0.0
        elif typ == b"trak":
            mv.tracks.append(_parse_trak(moov, bs, be))
    return mv


def read_samples(path, track):
    """Yield (start_seconds, duration_seconds, payload_bytes) for a track."""
    with open(path, "rb") as f:
        for off, size, st, du in track.samples():
            f.seek(off)
            yield st, du, f.read(size)
