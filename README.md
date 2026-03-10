# Nightfall Studio

Nightfall Studio is a desktop and CLI toolkit for building long-form lo-fi mixes with smooth transitions, adaptive processing, and export utilities.

It includes:
- `Lo-Fi Studio`: analyze tracks, plan transitions, preview, and render full mixes
- `MP3 Splitter`: split one MP3 into fixed-length chunks
- `MP4 Stitcher`: stitch video clips into one MP4 (with optional smart audio ordering/fades)

## Highlights

- GUI + CLI workflows
- Smart crossfade and smart ordering (BPM/key-aware)
- Adaptive lo-fi processing (per-track metrics + rationale)
- Expanded lo-fi preset editor: filters, tape drive/bias, compression timing, bit reduction, wow/flutter, stereo, noise, and atmosphere controls
- Optional rain layer + loudness targeting (LUFS / true peak)
- Preview system (short/full) with playback controls
- Output bitrate selector and size estimates
- Optional post-render MP3 chunk export
- Auto-generated timeline artifacts (`tracklist`, timestamp files, CSV/JSON)

## Requirements

- Python `3.11+`
- `ffmpeg` and `ffprobe` available in `PATH`
- OS: Windows/macOS/Linux (tested mainly on Windows)

## Installation

### 1. Clone

```bash
git clone <your-repo-url>
cd "NightFall Lofi Generator"
```

### 2. Create virtual environment

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
pip install -e .
```

## Run The App

### Desktop GUI

```bash
nightfall-studio
```

Alternative:

```bash
python -m nightfall_desktop
```

### CLI

```bash
nightfall-mix --songs-folder "E:\Songs\Lofi" --output "E:\Output\mix.mp3"
```

More advanced example:

```bash
nightfall-mix ^
  --songs-folder "E:\Songs\Lofi" ^
  --output "E:\Output\nightfall_mix.mp3" ^
  --smart-crossfade ^
  --smart-ordering ^
  --adaptive-lofi ^
  --rain "E:\Ambience\rain.mp3" ^
  --rain-level-db -28 ^
  --crossfade-sec 6 ^
  --lufs -14 ^
  --bitrate 192k ^
  --output-format mp3
```

See all options:

```bash
nightfall-mix --help
```

## GUI Quick Start

1. Open `Lo-Fi Studio` tab.
2. Choose a songs folder.
3. Click `Analyze`.
4. (Optional) Reorder/remove tracks.
5. Set output file path and format.
6. (Optional) Build `Short Preview` or `Full Preview`.
7. Click `Render`.

For full control documentation, see [GUI_TUTORIAL.md](GUI_TUTORIAL.md).

## Main GUI Workspaces

### 1) Lo-Fi Studio

- Smart planning:
  - Smart Crossfade
  - Smart Ordering (`BPM First` / `BPM + Key Balanced`)
- Sound controls:
  - Presets + Preset Editor
  - Adaptive Lo-Fi
  - Target LUFS
  - Optional rain layer
- Scope controls:
  - Loop-to-target duration
  - Preview mode duration
- Output controls:
  - `mp3` / `wav`
  - MP3 bitrate (`128k`..`320k`)
  - Optional `Split MP3` chunks after render

### 2) MP3 Splitter

- Input one MP3
- Choose chunk length + bitrate
- Export sequential chunk files (`_part_001`, `_part_002`, ...)

### 3) MP4 Stitcher

- Load clips from folder (or curated list)
- Reorder/remove clips before stitching
- Default: direct concat/glue
- Optional:
  - Smart ordering by audio features
  - Smart audio fades between clip boundaries

## Render Outputs And Artifacts

When rendering a lo-fi mix, Nightfall also writes:

- `tracklist.txt`
- `tracklist.json`
- `mix_timestamps.txt`
- `mix_timestamps.csv`

If adaptive mode is enabled, it also writes adaptive analysis reports (path configurable in GUI/CLI).

Example preset override payload: [examples/preset_config.sample.json](examples/preset_config.sample.json)

## Project Structure

```text
nightfall_mix/        # Core engine, analysis, planning, ffmpeg graph/commands, CLI
nightfall_desktop/    # PySide6 desktop UI, workers, services
tests/                # Automated tests
main.py               # CLI launcher wrapper
```

## Development

Run tests:

```bash
PYTHONPATH=. pytest -q
```

Type-check/lint can be added if needed (not enforced in current baseline).

## Troubleshooting

- `ffmpeg not found`:
  - Install ffmpeg and ensure `ffmpeg`/`ffprobe` are in `PATH`.

- `No space left on device`:
  - Set a larger `Render Cache Folder` in GUI.
  - Ensure both cache drive and output drive have enough free space.

- Unexpected MP4 concat failure:
  - Container/codec mismatch can break stream-copy concat.
  - Stitcher auto-retries with re-encode fallback.

- Long renders seem stalled:
  - Final master stage can take longer on multi-hour timelines; watch log panel for stage updates.
