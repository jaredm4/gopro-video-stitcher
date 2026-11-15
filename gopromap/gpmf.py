"""GPMF (GoPro Metadata Format) KLV parsing, GPS streams only.

Handles GPS9 (HERO11 and later, incl. HERO13: per-sample fix, DOP and UTC time)
and GPS5 (HERO5-10: fix/DOP/time given once per payload).
"""
import calendar
import struct

_FMT = {"b": "b", "B": "B", "s": "h", "S": "H", "l": "i", "L": "I",
        "j": "q", "J": "Q", "f": "f", "d": "d"}
_EPOCH_2000 = 946684800  # unix time of 2000-01-01T00:00:00Z


def _klv(buf, start, end):
    pos = start
    while pos + 8 <= end:
        key = buf[pos:pos + 4]
        if key == b"\0\0\0\0":
            break
        typ, size = buf[pos + 4], buf[pos + 5]
        rep = struct.unpack_from(">H", buf, pos + 6)[0]
        n = size * rep
        ds = pos + 8
        if ds + n > end:
            break
        yield key, (chr(typ) if typ else ""), size, rep, ds, ds + n
        pos = ds + ((n + 3) & ~3)


def _nums(buf, typ, s, e):
    c = _FMT.get(typ)
    if not c:
        return ()
    w = struct.calcsize(c)
    return struct.unpack_from(">%d%s" % ((e - s) // w, c), buf, s)


def _parse_gpsu(raw):
    # 'yymmddhhmmss.sss'
    try:
        s = raw.decode("ascii")
        tm = (2000 + int(s[0:2]), int(s[2:4]), int(s[4:6]),
              int(s[6:8]), int(s[8:10]), int(s[10:12]))
        return calendar.timegm(tm) + float("0" + s[12:])
    except (ValueError, IndexError):
        return None


def _scaled(vals, scal):
    if len(scal) == 1:
        scal = scal * len(vals)
    return [v / (s or 1) for v, s in zip(vals, scal)]


def gps_samples(payload):
    """Parse one gpmd payload.

    Returns a list of (lat, lon, alt_m, speed2d_mps, fix, dop, utc_or_None).
    """
    out9, out5 = [], []

    def stream(s, e):
        scal = tstr = None
        fix, dop, utc = 3, 0.0, None
        for key, typ, size, rep, ds, de in _klv(payload, s, e):
            if key == b"SCAL":
                scal = _nums(payload, typ, ds, de)
            elif key == b"TYPE":
                tstr = payload[ds:de].rstrip(b"\0").decode("latin1")
            elif key == b"GPSF":
                v = _nums(payload, typ, ds, de)
                fix = v[0] if v else fix
            elif key == b"GPSP":
                v = _nums(payload, typ, ds, de)
                dop = v[0] / 100.0 if v else dop
            elif key == b"GPSU":
                utc = _parse_gpsu(payload[ds:de])
            elif key == b"GPS9" and scal:
                try:
                    fmt = ">" + "".join(_FMT[c] for c in (tstr or "lllllllSS"))
                except KeyError:
                    continue
                if struct.calcsize(fmt) != size or len(fmt) - 1 < 9:
                    continue
                for i in range(rep):
                    v = _scaled(struct.unpack_from(fmt, payload, ds + i * size), scal)
                    t = _EPOCH_2000 + v[5] * 86400 + v[6]
                    out9.append((v[0], v[1], v[2], v[3], int(v[8]), v[7], t))
            elif key == b"GPS5" and scal and size == 20:
                for i in range(rep):
                    v = _scaled(struct.unpack_from(">5i", payload, ds + i * 20), scal)
                    out5.append((v[0], v[1], v[2], v[3], int(fix), dop, utc))

    def walk(s, e):
        for key, typ, size, rep, ds, de in _klv(payload, s, e):
            if typ == "":
                if key == b"STRM":
                    stream(ds, de)
                else:
                    walk(ds, de)

    walk(0, len(payload))
    return out9 or out5
