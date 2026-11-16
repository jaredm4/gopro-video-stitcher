"""Slippy-map tile fetching with an on-disk cache."""
import hashlib
import io
import os
import time
import urllib.request

from PIL import Image

# name -> (url template, attribution drawn on the map, max zoom)
PRESETS = {
    "osm": ("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            "© OpenStreetMap contributors", 19),
    "topo": ("https://tile.opentopomap.org/{z}/{x}/{y}.png",
             "© OpenTopoMap (CC-BY-SA)", 17),
    "satellite": ("https://server.arcgisonline.com/ArcGIS/rest/services/"
                  "World_Imagery/MapServer/tile/{z}/{y}/{x}",
                  "Imagery © Esri, Maxar, Earthstar Geographics", 18),
}
MAX_TILES = 600
USER_AGENT = "gopromap/0.1 (personal GoPro map overlay tool)"


def default_cache_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "gopromap", "tiles")


class TileSource:
    def __init__(self, spec="osm", cache_dir=None, attribution=None, max_zoom=None):
        if spec in PRESETS:
            self.url, self.attribution, self.max_zoom = PRESETS[spec]
            name = spec
        else:
            if "{z}" not in spec:
                raise SystemExit(f"--tiles must be one of {', '.join(PRESETS)} or a URL "
                                 "template containing {z} {x} {y}")
            self.url, self.attribution, self.max_zoom = spec, "", 19
            name = hashlib.sha1(spec.encode()).hexdigest()[:12]
        if attribution is not None:
            self.attribution = attribution
        if max_zoom:
            self.max_zoom = max_zoom
        self.cache = os.path.join(cache_dir or default_cache_dir(), name)
        self.failed = 0
        self._used = False
        self.downloaded = 0

    def tile(self, z, x, y):
        n = 1 << z
        if not 0 <= y < n:
            return None
        x %= n
        p = os.path.join(self.cache, str(z), str(x), f"{y}.img")
        data = None
        if os.path.isfile(p):
            with open(p, "rb") as f:
                data = f.read()
        else:
            url = self.url.format(z=z, x=x, y=y)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = r.read()
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(data)
                self.downloaded += 1
                if url.startswith("http"):
                    time.sleep(0.05)  # be polite to public tile servers
            except Exception as e:  # network errors, 404s, ...
                if self.failed == 0:
                    print(f"  warning: tile download failed ({e}); url was {url}")
                self.failed += 1
                return None
        try:
            im = Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            self.failed += 1
            return None
        return im if im.size == (256, 256) else im.resize((256, 256), Image.LANCZOS)

    def mosaic(self, z, x0, y0, x1, y1):
        """RGB image covering pixel box [x0,x1) x [y0,y1) in zoom-z pixel space."""
        tx0, ty0, tx1, ty1 = x0 // 256, y0 // 256, (x1 - 1) // 256, (y1 - 1) // 256
        count = (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
        if count > MAX_TILES:
            raise SystemExit(f"Map would need {count} tiles (limit {MAX_TILES}). "
                             "Use a lower --zoom, --view fit, or --style path.")
        out = Image.new("RGB", (x1 - x0, y1 - y0), (58, 62, 66))
        failed_before = self.failed
        for ty in range(ty0, ty1 + 1):
            for tx in range(tx0, tx1 + 1):
                im = self.tile(z, tx, ty)
                if im is not None:
                    out.paste(im, (tx * 256 - x0, ty * 256 - y0))
        first, self._used = not self._used, True
        if first and self.failed - failed_before >= count:
            raise SystemExit("No map tiles could be loaded. Check the network / --tiles URL, "
                             "or use --style path (needs no map data).")
        return out
