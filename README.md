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

### Clip order

`--sort meta` (default) uses the creation time embedded in each file, then the
GPS clock, then the name. `--sort mtime` uses the file modified time and
`--sort name` the file name. `info` shows the resulting order.

### Overlay look

| Option | Default | Notes |
|---|---|---|
| `--style map\|path` | `map` | `map` = basemap tiles with the route; `path` = route line only, no downloads |
| `--view fit\|follow` | `fit` | `fit` shows the whole route; `follow` is zoomed in and the map scrolls (`--zoom`, default 16) |
| `--shape circle\|rounded\|square` | circle (map), square (path) | |
| `--opacity` | `0.8` | |
| `--position br\|bl\|tr\|tl` | `br` | `--margin` px from the edge |
| `--size` | 20% of video height | map diameter in px |
| `--tiles osm\|topo\|satellite\|URL` | `osm` | or any `{z}/{x}/{y}` URL template |
| `--color`, `--rest-color`, `--marker-color`, `--line-width` | | travelled line, route ahead, position dot |
| `--backdrop 0..1` | `0` | path style: dark panel behind the line |
| `--no-coords`, `--font`, `--font-size` | | lat/lon text under the map, updated once a second |

`docs/style-samples.png` shows the four main variants.

Map tiles are downloaded once and cached in `%LOCALAPPDATA%\gopromap\tiles`.
The public tile servers are for light personal use and require the attribution
that is drawn on the map; check the provider's terms (especially for
`satellite`, which is Esri World Imagery) before publishing videos.

### Trimming

`--keep START-END` (repeatable, or comma-separated) keeps only those ranges of
the stitched timeline, frame-accurately, in the same encode as the overlay:

```
python -m gopromap render <folder> --keep 0:30-4:10 --keep 9:00-12:45
```

With more than one range the audio is re-encoded to AAC 192k; with one range
(or none) it is copied untouched.

### Encoding

Default is `hevc_nvenc` Main10, `-cq 21`, preset p6. `--quality` changes the
CQ (lower = bigger/better), `--bitrate 45M` targets a bitrate instead,
`--encoder libx265` uses the CPU (`--quality` is then CRF, default 18).
`--hwaccel cuda` also decodes on the GPU. `--encoder-args "..."` appends raw
ffmpeg output options.

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
