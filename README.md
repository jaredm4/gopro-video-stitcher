# gopromap

Stitch GoPro chapter files losslessly and burn in a GPS mini-map, keeping the
footage's bit depth, colour tags and range exactly as shot.

## Setup (Windows)

Needs Python 3.10+, plus `ffmpeg` and `ffprobe` on PATH (an NVENC-enabled build
for the default encoder).

```
cd gopro-video-stitcher
pip install -r requirements.txt
```

Run everything from this folder as `python -m gopromap <command> <folder>`.
`<folder>` is the directory holding the camera's `.MP4` files (top level only).
Sources are never modified; outputs go to `<folder>\output\` unless `-o` is given.

## Commands

| Command | What it does |
|---|---|
| `info` | Lists clips in stitch order, video format, and how much usable GPS there is. Run this first. |
| `stitch` | Joins all clips into one file by stream copy (no re-encode). Keeps the GoPro telemetry track when ffmpeg allows it. |
| `gps` | Exports the track as `.gpx` (or `.csv` if `-o` ends in .csv). |
| `preview` | Writes PNG stills with the overlay, for tuning the look in seconds. |
| `render` | Stitch + overlay + optional trimming in a single encode. |

```
python -m gopromap info    "D:\Video\gopro export 2026-11-15"
python -m gopromap stitch  "D:\Video\gopro export 2026-11-15"
python -m gopromap preview "D:\Video\gopro export 2026-11-15" --at 2:00 --at 9:30
python -m gopromap render  "D:\Video\gopro export 2026-11-15" --start 2:00 --duration 20
python -m gopromap render  "D:\Video\gopro export 2026-11-15"
```

`render` reads the original clips directly, so you don't need to `stitch`
first; `stitch` is for when you want the untouched joined file.

## Option reference

Value formats used below:

- **TIME**: seconds (`90`, `12.5`), `M:SS` (`1:30`) or `H:MM:SS` (`1:02:30`),
  measured on the stitched timeline. Decimals are allowed on the seconds.
- **FRACTION**: a decimal share of the video height, `0.2` = 20%. `20%` is
  also accepted.
- **PX**: whole pixels of the output video (a 4K frame is 3840 x 2160).
- **0..1**: a decimal from 0 to 1.
- **COLOUR**: `#RRGGBB` hex (quote it: `"#19a7ff"`) or a CSS colour name such
  as `orange` or `white`.

### All commands

| Option | Value | Default | Meaning |
|---|---|---|---|
| `source` | folder or file path | required | Folder holding the `.MP4` files (top level only), or one file. Quote paths with spaces. |
| `--sort` | `meta`, `mtime` or `name` | `meta` | Clip order. `meta` = creation time embedded in each file, then the GPS clock, then name. `mtime` = file modified time. `name` = file name. `info` shows the resulting order. |
| `--max-dop` | number | `10` | Drops GPS samples whose dilution of precision is above this. Lower is stricter; 5 is a reasonable tight setting. |
| `--ffmpeg`, `--ffprobe` | path to the .exe | found on PATH | Use a specific ffmpeg/ffprobe build. |
| `-o`, `--output` | path | `<folder>\output\...` | Output file for `stitch`, `gps` and `render` (`gps` writes CSV if the name ends in `.csv`). For `preview` it is an output **folder**. |

### Overlay look (`preview` and `render`)

| Option | Value | Default | Meaning |
|---|---|---|---|
| `--style` | `map` or `path` | `map` | `map` = basemap tiles with the route on top. `path` = route line only, nothing downloaded. |
| `--view` | `fit` or `follow` | `fit` | `fit` shows the whole route and the dot moves. `follow` is zoomed in, the dot stays centred and the map scrolls. |
| `--zoom` | number, decimals allowed | `16` | Only for `--view follow`. Standard web-map zoom level: each +1 shows half as much ground. At 16 the default-size map covers roughly 750 m across at 43°N; 14 is about 3 km; 19 is about 90 m. Any route length works: map tiles are fetched as the camera moves, and `render` prints how many it expects to need. Past the tile source's finest level (19 for `osm`, 17 for `topo`, 18 for `satellite`) the map is enlarged and turns soft. |
| `--shape` | `circle`, `rounded` or `square` | `circle` for map, `square` for path | Outline of the map area. |
| `--size` | FRACTION | `0.2` | Map diameter (or side) relative to video height. `0.2` on 4K is 432 px. |
| `--opacity` | 0..1 | `0.8` | Opacity of the map. `1` is solid. The coordinate text is always at least 0.9. |
| `--position` | `br`, `bl`, `tr` or `tl` | `br` | Corner: bottom/top + right/left. |
| `--margin` | PX | 3% of video height (65 on 4K) | Gap between the overlay and the frame edges. |
| `--tiles` | `osm`, `topo`, `satellite`, or a URL | `osm` | Basemap for `--style map`. A custom URL must contain `{z}`, `{x}` and `{y}`, e.g. `"https://example.com/{z}/{x}/{y}.png"`. |
| `--tile-cache` | folder path | `%LOCALAPPDATA%\gopromap\tiles` | Where downloaded tiles are kept for reuse. |
| `--color` | COLOUR | `#19a7ff` | Route already travelled. |
| `--rest-color` | COLOUR | `#ffffff` | Route still ahead. |
| `--marker-color` | COLOUR | `#ff3b30` | Current-position dot. |
| `--line-width` | PX, decimals allowed | 5 at the default size on 4K; scales with `--size` | Thickness of the route line. |
| `--backdrop` | 0..1 | `0` | `--style path` only: darkness of a panel behind the line. `0` = none, `0.35` = light smoke, `1` = black. |
| `--no-coords` | flag, no value | off | Hides the latitude/longitude text under the map. |
| `--font` | path to a `.ttf`/`.otf` file | Consolas Bold | Font for the coordinate text, e.g. `"C:\Windows\Fonts\segoeuib.ttf"`. A monospaced font keeps the digits from jittering. |
| `--font-size` | PX | `0` = auto | Font size (em height) of the coordinate text. Auto picks the largest size that fits the map width: about 30 at the default size on 4K. |
| `--hwaccel` | ffmpeg method name | none | Decode on the GPU, e.g. `cuda`. Optional; CPU decode is usually fast enough. |

`docs/style-samples.png` shows the four main variants.

Map tiles are downloaded once and cached. The public tile servers are for
light personal use and require the attribution that is drawn on the map; check
the provider's terms (especially for `satellite`, which is Esri World Imagery)
before publishing videos.

### `preview` only

| Option | Value | Default | Meaning |
|---|---|---|---|
| `--at` | TIME, repeatable | 25%, 50% and 75% through | Moments to grab. Repeat the option for several: `--at 1:30 --at 9:00`. |

### `render` only

| Option | Value | Default | Meaning |
|---|---|---|---|
| `--keep` | `TIME-TIME`, repeatable | whole video | Keep only these ranges, frame-accurately, in the same encode as the overlay. Repeat the option or separate with commas: `--keep 0:30-4:10 --keep 9:00-12:45`. An open end (`--keep 9:00-`) runs to the end of the video. |
| `--start` | TIME | `0` | Start of a single sample to render. |
| `--duration` | TIME | to the end | Length of that sample, e.g. `20` for 20 seconds. |
| `--encoder` | `hevc_nvenc` or `libx265` | `hevc_nvenc` | GPU (NVIDIA) or CPU encoder. Both write HEVC Main10 for 10-bit sources. |
| `--quality` | number, 0-51, lower = better and larger | `21` (NVENC) / `18` (x265) | Constant-quality level: NVENC `-cq` or x265 `-crf`. Steps of 2-3 are visible in file size. |
| `--bitrate` | ffmpeg bitrate such as `45M` or `60000k` | off | NVENC only: target this bitrate instead of `--quality`. |
| `--preset` | NVENC `p1`..`p7`; x265 `ultrafast`..`veryslow` | `p6` / `medium` | Encoder speed/efficiency trade-off. Higher `p` number or slower x265 name = smaller file at the same quality. |
| `--encoder-args` | quoted string of ffmpeg options | none | Appended to the ffmpeg output options, e.g. `"-movflags +faststart"`. |

With more than one `--keep` range the audio is re-encoded to AAC 192k; with
one range (or none) it is copied untouched.

## How quality is preserved

- The video is never converted: frames stay in their native 4:2:0 10-bit
  format through ffmpeg's `overlay=format=yuv420p10`.
- The overlay is converted to the footage's own matrix, range and bit depth in
  Python and handed to ffmpeg as `yuva420p10le`, so no scaler touches either
  stream.
- Colour tags (range, primaries, transfer, matrix) are copied from the source
  to the output.

Tested with a lossless encode: every pixel outside the overlay box is
bit-identical to the source.

## Notes and limits

- GPS comes from the `gpmd` track (GPS9 on HERO11-13, GPS5 on older cameras).
  Samples without a fix, or with DOP above `--max-dop` (10), are dropped; until
  the first fix the marker sits at the first known position.
- HLG (GoPro "HLG HDR") and PQ footage: the video passes through untouched and
  keeps its BT.2020/HLG tags. The overlay is converted from sRGB into the
  footage's HDR signal with SDR white placed at HDR reference white (203 nits,
  75% HLG), per ITU-R BT.2408, so it matches normal whites in the scene.
  Preview PNGs of HDR footage are not tone-mapped and look washed out; judge
  colour from a short `render` sample on an HDR display instead.
- All clips in a folder are treated as one timeline. Put separate outings in
  separate folders.
- `tests/make_fixture.py <dir>` builds small synthetic 10-bit clips with a GPS
  track for experimenting without camera footage.
