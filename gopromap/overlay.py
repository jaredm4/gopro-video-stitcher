"""Renders the mini-map panel (map + coordinate text) for any moment in time.

Everything is drawn at 2x and reduced for antialiasing, composited in
premultiplied alpha, then converted straight to the video's own YUV
matrix/range/bit depth so ffmpeg never has to colour-convert anything.
"""
import math
import os
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageColor, ImageDraw, ImageFont

SS = 2            # supersampling factor
MAX_BASE = 6000   # max basemap side in px for --view follow


@dataclass
class Style:
    style: str = "map"          # map | path
    view: str = "fit"           # fit | follow
    shape: str = "circle"       # circle | rounded | square
    size: int = 432             # map diameter / side in px
    opacity: float = 0.8
    zoom: float = 16.0          # follow view only
    pad: float = 0.12           # fit view: empty border around the route
    line_width: float = 5.0
    color: str = "#19a7ff"      # travelled route
    rest_color: str = "#ffffff"  # not-yet-travelled route
    marker_color: str = "#ff3b30"
    marker_radius: float = 9.0
    border: float = 3.0
    backdrop: float = 0.0       # path style: darkness behind the path (0..1)
    coords: bool = True
    font: str = ""
    font_size: int = 0          # 0 = fit to panel width


def load_font(size, path=""):
    cands = [path] if path else []
    win = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts")
    cands += [os.path.join(win, "consolab.ttf"), os.path.join(win, "consola.ttf"),
              "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
              "/System/Library/Fonts/Menlo.ttc", "DejaVuSansMono-Bold.ttf"]
    for c in cands:
        try:
            return ImageFont.truetype(c, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _premult(img):
    """RGBA image drawn at SS-x -> (premultiplied rgb, alpha) float arrays at 1x."""
    a = np.asarray(img.convert("RGBa").reduce(SS), dtype=np.float32) / 255.0
    return a[..., :3], a[..., 3:]


def format_coords(lat, lon):
    return (f"{abs(lat):.5f}°{'N' if lat >= 0 else 'S'}  "
            f"{abs(lon):.5f}°{'E' if lon >= 0 else 'W'}")


class Renderer:
    def __init__(self, track, st, tiles=None):
        if len(track) < 2:
            raise SystemExit("No usable GPS fixes in this footage; nothing to draw. "
                             "Run the 'info' command to see what the files contain.")
        self.st, self.track, self.tiles = st, track, tiles
        S = self.S = st.size - st.size % 2
        mx, my = track.mx, track.my
        x0, x1, y0, y1 = mx.min(), mx.max(), my.min(), my.max()
        max_zoom = tiles.max_zoom if tiles else 22
        if st.view == "fit":
            if st.shape == "circle":   # keep the bounding box's corners inside the circle
                span = max(math.hypot(x1 - x0, y1 - y0), 2e-6)
            else:
                span = max(x1 - x0, y1 - y0, 2e-6)
            zf = min(math.log2(S * (1 - 2 * st.pad) / span / 256), max_zoom)
        else:
            zf = st.zoom
            if st.style == "map":
                while max(x1 - x0, y1 - y0) * 256 * 2 ** zf + S + 4 > MAX_BASE:
                    zf -= 0.25
                if zf < st.zoom:
                    print(f"  note: route is large; follow zoom reduced to {zf:.2f}")
        self.zf = zf
        scale = 256 * 2.0 ** zf
        # Work relative to an integer origin to keep coordinates small.
        self.ox, self.oy = math.floor(x0 * scale), math.floor(y0 * scale)
        self.px = mx * scale - self.ox
        self.py = my * scale - self.oy
        self.t = track.t

        # Thin the route to ~1 point per 0.75 px so drawing stays fast.
        keep = [0]
        lx, ly = self.px[0], self.py[0]
        for i in range(1, len(self.px)):
            if abs(self.px[i] - lx) + abs(self.py[i] - ly) >= 0.75:
                keep.append(i)
                lx, ly = self.px[i], self.py[i]
        self.dx, self.dy, self.dt = self.px[keep], self.py[keep], self.t[keep]

        if st.view == "fit":
            self.vx0 = int(round((self.px.min() + self.px.max()) / 2 - S / 2))
            self.vy0 = int(round((self.py.min() + self.py.max()) / 2 - S / 2))
            region = (self.vx0, self.vy0, self.vx0 + S, self.vy0 + S)
        else:
            m = S // 2 + 2
            region = (int(math.floor(self.px.min())) - m, int(math.floor(self.py.min())) - m,
                      int(math.ceil(self.px.max())) + m, int(math.ceil(self.py.max())) + m)
        self.base = None
        self.region = region
        if st.style == "map":
            self.base = self._build_base(region)

        self.c_line = ImageColor.getrgb(st.color) + (255,)
        self.c_rest = ImageColor.getrgb(st.rest_color) + (255,)
        self.c_mark = ImageColor.getrgb(st.marker_color) + (255,)
        self.c_case = (20, 24, 28, 255)
        self.lw = max(2, int(round(st.line_width * SS)))

        self._mask = self._make_mask()
        self._deco = self._make_deco()
        self._fit_layer = None
        self._fit_k = 0
        self._text_cache = (None, None)

        # Coordinate text block under the map.
        self.text_h = 0
        self.gap = 0
        if st.coords:
            fs = st.font_size
            if not fs:
                fs = max(12, S // 14)
                while fs > 10:
                    f = load_font(fs * SS, st.font)
                    w = f.getbbox(format_coords(-88.88888, -188.88888), stroke_width=2 * SS)[2]
                    if w <= S * SS:
                        break
                    fs -= 1
            self.font = load_font(fs * SS, st.font)
            self.text_h = int(fs * 1.5) + int(fs * 1.5) % 2
            self.gap = max(6, S // 40) + max(6, S // 40) % 2
        self.panel_w = S
        self.panel_h = S + self.gap + self.text_h

    # ---- static pieces -------------------------------------------------
    def _build_base(self, region):
        ts = self.tiles
        zt = max(0, min(ts.max_zoom, math.ceil(self.zf)))
        f = 2.0 ** (self.zf - zt)          # output px per tile px
        gx0, gy0 = region[0] + self.ox, region[1] + self.oy   # global output px
        gx1, gy1 = region[2] + self.ox, region[3] + self.oy
        tx0, ty0 = math.floor(gx0 / f) - 1, math.floor(gy0 / f) - 1
        tx1, ty1 = math.ceil(gx1 / f) + 1, math.ceil(gy1 / f) + 1
        mos = ts.mosaic(zt, tx0, ty0, tx1, ty1)
        if ts.downloaded:
            print(f"  downloaded {ts.downloaded} map tiles (cached in {ts.cache})")
        out = mos.resize((gx1 - gx0, gy1 - gy0), Image.LANCZOS,
                         box=(gx0 / f - tx0, gy0 / f - ty0, gx1 / f - tx0, gy1 / f - ty0))
        return np.asarray(out, dtype=np.float32) / 255.0

    def _shape(self, draw, box, **kw):
        if self.st.shape == "circle":
            draw.ellipse(box, **kw)
        elif self.st.shape == "rounded":
            draw.rounded_rectangle(box, radius=(box[2] - box[0]) * 0.09, **kw)
        else:
            draw.rectangle(box, **kw)

    def _make_mask(self):
        S, k = self.S, 4
        m = Image.new("L", (S * k, S * k), 0)
        self._shape(ImageDraw.Draw(m), (0, 0, S * k - 1, S * k - 1), fill=255)
        m = m.reduce(k)
        return np.asarray(m, dtype=np.float32)[..., None] / 255.0

    def _make_deco(self):
        """Border ring + map attribution, as a premultiplied layer."""
        S, st = self.S, self.st
        W = S * SS
        im = Image.new("RGBA", (W, W), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        if st.border > 0 and (st.style == "map" or st.backdrop > 0):
            self._shape(d, (0, 0, W - 1, W - 1), outline=(255, 255, 255, 235),
                        width=int(round(st.border * SS)))
        if st.style == "map" and self.tiles and self.tiles.attribution:
            f = load_font(max(9, S // 40) * SS, st.font)
            d.text((W / 2, W * 0.955), self.tiles.attribution, font=f, anchor="ms",
                   fill=(255, 255, 255, 255), stroke_width=SS, stroke_fill=(0, 0, 0, 200))
        return _premult(im)

    def _text(self, lat, lon):
        s = format_coords(lat, lon)
        if self._text_cache[0] != s:
            W, H = self.S * SS, self.text_h * SS
            im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(im).text((W / 2, H / 2), s, font=self.font, anchor="mm",
                                    fill=(255, 255, 255, 255), stroke_width=2 * SS,
                                    stroke_fill=(0, 0, 0, 255))
            self._text_cache = (s, _premult(im))
        return self._text_cache[1]

    # ---- per-frame drawing ---------------------------------------------
    def _runs(self, draw, lo, hi, vx, vy, fill, width):
        """Draw route points [lo, hi) that fall inside the viewport."""
        if hi - lo < 2:
            return
        xs = (self.dx[lo:hi] - vx) * SS
        ys = (self.dy[lo:hi] - vy) * SS
        W, m = self.S * SS, width + 4
        inside = (xs > -m) & (xs < W + m) & (ys > -m) & (ys < W + m)
        vis = inside.copy()
        vis[1:] |= inside[:-1]
        vis[:-1] |= inside[1:]
        if vis.all():
            draw.line(list(zip(xs.tolist(), ys.tolist())), fill=fill, width=width, joint="curve")
            return
        edges = np.flatnonzero(np.diff(np.concatenate(([0], vis.view(np.int8), [0]))))
        for s, e in zip(edges[0::2], edges[1::2]):
            if e - s >= 2:
                draw.line(list(zip(xs[s:e].tolist(), ys[s:e].tolist())),
                          fill=fill, width=width, joint="curve")

    def _route_layer(self, vx, vy, k, cx, cy):
        W = self.S * SS
        n = len(self.dx)
        if self.st.view == "fit":
            if self._fit_layer is None or k < self._fit_k:
                self._fit_layer = Image.new("RGBA", (W, W), (0, 0, 0, 0))
                d = ImageDraw.Draw(self._fit_layer)
                self._runs(d, 0, n, vx, vy, self.c_case, self.lw + 2 * SS)
                self._runs(d, 0, n, vx, vy, self.c_rest, self.lw)
                self._fit_k = 0
            if k > self._fit_k:
                d = ImageDraw.Draw(self._fit_layer)
                self._runs(d, max(0, self._fit_k - 2), k, vx, vy, self.c_line, self.lw)
                self._fit_k = k
            layer = self._fit_layer.copy()
            d = ImageDraw.Draw(layer)
        else:
            layer = Image.new("RGBA", (W, W), (0, 0, 0, 0))
            d = ImageDraw.Draw(layer)
            self._runs(d, 0, n, vx, vy, self.c_case, self.lw + 2 * SS)
            self._runs(d, max(0, k - 1), n, vx, vy, self.c_rest, self.lw)
            self._runs(d, 0, k, vx, vy, self.c_line, self.lw)
        mx, my = (cx - vx) * SS, (cy - vy) * SS
        if k > 0:  # tip of the travelled line, up to the exact current position
            d.line([((self.dx[k - 1] - vx) * SS, (self.dy[k - 1] - vy) * SS), (mx, my)],
                   fill=self.c_line, width=self.lw)
        r = self.st.marker_radius * SS
        for rad, col in ((r + 3 * SS, self.c_case), (r + 2 * SS, (255, 255, 255, 255)),
                         (r, self.c_mark)):
            d.ellipse((mx - rad, my - rad, mx + rad, my + rad), fill=col)
        return layer

    def frame(self, t):
        """-> (premultiplied rgb [H,W,3], alpha [H,W,1]) float32 arrays for time t."""
        S, st = self.S, self.st
        cx = float(np.interp(t, self.t, self.px))
        cy = float(np.interp(t, self.t, self.py))
        if st.view == "fit":
            vx, vy = self.vx0, self.vy0
        else:
            vx, vy = int(round(cx - S / 2)), int(round(cy - S / 2))
        k = int(np.searchsorted(self.dt, t, side="right"))
        lp, la = _premult(self._route_layer(vx, vy, k, cx, cy))
        if self.base is not None:
            bx, by = vx - self.region[0], vy - self.region[1]
            p = lp + self.base[by:by + S, bx:bx + S] * (1 - la)
            a = np.ones_like(la)
        else:
            p = lp
            a = la + st.backdrop * (1 - la)
        dp, da = self._deco
        p = dp + p * (1 - da)
        a = da + a * (1 - da)
        m = self._mask * st.opacity
        P = np.zeros((self.panel_h, self.panel_w, 3), np.float32)
        A = np.zeros((self.panel_h, self.panel_w, 1), np.float32)
        P[:S] = p * m
        A[:S] = a * m
        if st.coords:
            lat, lon = self.track.at(math.floor(t))   # text changes once a second
            tp, ta = self._text(lat, lon)
            o = max(st.opacity, 0.9)
            P[S + self.gap:] = tp * o
            A[S + self.gap:] = ta * o
        return P, A


# ---- pixel format conversion --------------------------------------------
_KR_KB = {"bt709": (0.2126, 0.0722), "bt2020nc": (0.2627, 0.0593),
          "bt2020c": (0.2627, 0.0593), "smpte170m": (0.299, 0.114),
          "bt470bg": (0.299, 0.114)}


_709_TO_2020 = np.array([[0.6274, 0.3293, 0.0433],
                         [0.0691, 0.9195, 0.0114],
                         [0.0164, 0.0880, 0.8956]], dtype=np.float32)
HDR_TRANSFERS = ("arib-std-b67", "smpte2084")


def sdr_to_hdr(rgb, transfer, white_nits=203.0):
    """sRGB/BT.709 graphics -> BT.2020 HLG or PQ signal (ITU-R BT.2408 mapping).

    SDR white lands at the HDR reference "graphics white" (203 nits, which is
    75% signal in HLG), so the overlay sits at the same brightness as ordinary
    whites in the footage instead of glowing at peak.
    """
    lin = np.power(rgb, 2.4) @ _709_TO_2020.T          # display light, 1.0 = SDR white
    if transfer == "smpte2084":                        # PQ: absolute nits
        y = np.power(np.clip(lin * (white_nits / 10000.0), 0, 1), 0.1593017578125)
        return np.power((0.8359375 + 18.8515625 * y) / (1 + 18.6875 * y), 78.84375)
    # HLG: undo the display's OOTF (gamma 1.2 at a nominal 1000-nit peak), then OETF.
    ld = lin * (white_nits / 1000.0)
    yd = ld @ np.array([0.2627, 0.6780, 0.0593], dtype=np.float32)
    es = np.clip(ld * np.power(np.maximum(yd, 1e-6), (1 - 1.2) / 1.2)[..., None], 0, 1)
    a, b, c = 0.17883277, 0.28466892, 0.55991073
    return np.where(es <= 1 / 12, np.sqrt(3 * es),
                    a * np.log(np.maximum(12 * es - b, 1e-6)) + c).astype(np.float32)


def to_yuva420(P, A, depth=10, matrix="bt709", full_range=True, transfer="bt709"):
    """Premultiplied RGB + alpha -> planar yuva420p (8 bit) / yuva420p10le bytes."""
    kr, kb = _KR_KB.get(matrix, _KR_KB["bt709"])
    kg = 1.0 - kr - kb
    a = A[..., 0]
    rgb = np.clip(P / np.maximum(A, 1e-5), 0.0, 1.0)
    if transfer in HDR_TRANSFERS:
        rgb = sdr_to_hdr(rgb, transfer)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    y = kr * r + kg * g + kb * b
    cb = (b - y) / (2 * (1 - kb))
    cr = (r - y) / (2 * (1 - kr))
    h, w = a.shape

    def sub(c):  # alpha-weighted 2x2 chroma average (avoids dark fringes)
        cw = (c * a).reshape(h // 2, 2, w // 2, 2).sum(axis=(1, 3))
        aw = a.reshape(h // 2, 2, w // 2, 2).sum(axis=(1, 3))
        return cw / np.maximum(aw, 1e-5)

    cb, cr = sub(cb), sub(cr)
    maxv = (1 << depth) - 1
    half = 1 << (depth - 1)
    if full_range:
        Y, Cb, Cr = y * maxv, cb * maxv + half, cr * maxv + half
    else:
        k = 1 << (depth - 8)
        Y, Cb, Cr = (219 * y + 16) * k, (224 * cb + 128) * k, (224 * cr + 128) * k
    dt = "<u2" if depth > 8 else "u1"
    planes = [np.clip(np.rint(v), 0, maxv).astype(dt) for v in (Y, Cb, Cr, a * maxv)]
    return b"".join(p.tobytes() for p in planes)


def to_image(P, A):
    """Straight-alpha RGBA PIL image (for quick looks without ffmpeg)."""
    rgb = np.clip(P / np.maximum(A, 1e-5), 0, 1)
    arr = np.concatenate([rgb, np.clip(A, 0, 1)], axis=2)
    return Image.fromarray(np.rint(arr * 255).astype(np.uint8), "RGBA")
