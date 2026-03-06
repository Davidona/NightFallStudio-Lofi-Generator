# Nightfall Studio GUI Guide (Detailed)

This guide explains every control in the desktop app, what it changes in the engine, and how to use it in practice.

## 1. What the app does

Nightfall Studio builds one continuous mix from multiple songs.

Core pipeline:
1. Discover tracks in `Songs Folder`.
2. Analyze tracks (duration, loudness, and optional BPM/key/adaptive metrics).
3. Build a timeline with overlaps (crossfades).
4. Render final audio (`.mp3` or `.wav`) or a preview excerpt.

## 2. Important behavior clarifications

### BPM and ordering
1. BPM/key can be used for both transition decisions and ordering, but only when `Smart Ordering` is enabled.
2. If `Smart Ordering` is OFF, initial order is alphabetical (or shuffled if `Shuffle` is ON).
3. After analysis, manual drag/drop order always becomes the active order.

### Smart Crossfade vs Smart Ordering
1. `Smart Crossfade` controls how long each overlap is and whether key-mask logic is applied.
2. `Smart Ordering` controls track sequence before the plan is built.
3. `Smart Ordering` is only available when `Smart Crossfade` is enabled.

### Duration and size indicators
1. The bottom bar shows `Timeline`, `Render Scope`, and `Estimated Size`.
2. `Timeline` is expected full mix length after overlaps.
3. `Render Scope` is either full timeline or preview duration (if preview mode is ON).
4. `Estimated Size` is calculated from render scope plus selected output format.

## 3. Screen layout

1. Main workspace tabs:
   - `Lo-Fi Studio`
   - `MP3 Splitter`
   - `MP4 Stitcher`
2. `Lo-Fi Studio` top strip: mode tabs (`Simple` / `Advanced`) and grouped control cards:
   - `Sound Profile`
   - `Transitions And Ordering`
   - `Length And Scope`
   - `Rain And Mastering`
3. `Lo-Fi Studio` middle:
   - Left panel: folder and track list (ordering)
   - Center panel: `Reports` + timeline visualization
   - Right panel: per-track adaptive metrics and rationale
4. `Lo-Fi Studio` bottom: output settings, estimates, logs, preview controls, and render actions.

## 4. Top strip controls (full explanation)

### Workspace mode tabs

`Simple` tab
1. Shows a reduced control set for faster setup.
2. Keeps advanced DSP choices pinned to recommended baseline values.
3. Exposes only key decisions:
   - preset
   - output format
   - optional loop target
   - optional preview tuning toggle

`Advanced` tab
1. Shows the full control strip.
2. Lets you tune every available parameter.
3. On app startup, recommended defaults are preloaded.
4. The simple quick-controls panel is hidden in advanced mode to keep layout compact.

`Reset To Recommended`
1. Applies the recommended baseline profile immediately.
2. Works in both modes.

### Sound Profile card

`Preset` dropdown
1. Selects base sound profile from `effects_presets.py`.
2. Each preset has different baseline DSP values (LPF, saturation, compression, stereo width, wow/flutter behavior).

`Preset Editor`
1. Opens override editor for currently selected preset only.
2. Changes are per-preset, not global.

`Reset Preset`
1. Resets overrides for the active preset only.

`Reset All Presets`
1. Clears overrides for all presets.

`Quality` (`fast`, `balanced`, `best`)
1. `fast`: quickest loudness pipeline, least precise leveling.
2. `balanced`: good middle ground for normal usage.
3. `best`: highest loudness consistency, highest processing cost.

### Transitions And Ordering card

`Adaptive Lo-Fi`
1. Enables per-track metric analysis.
2. Applies track-specific lo-fi decisions instead of one fixed profile for all tracks.

`Smart Crossfade`
1. Enables transition-level analysis for overlap length selection.
2. Uses RMS edge curves (and BPM confidence when available) to prefer smoother joins.
3. Can extend transition and add LPF ducking behavior on harmonically distant transitions (key-mask path).
4. OFF means fixed overlap from `Crossfade (s)` (still clamped for short tracks).

`Smart Ordering`
1. Reorders initial track list by BPM/key proximity at analysis time.
2. Requires `Smart Crossfade` ON.
3. Does not override your later manual drag/drop order.

`Smart Ordering Mode`
1. `BPM First`: prioritize BPM closeness strongly, key is a lighter factor.
2. `BPM + Key Balanced`: stronger key weighting while still considering BPM.

`Shuffle`
1. Randomizes initial order before planning (seeded internally).
2. Manual reorder after analysis still takes precedence.

### Rain And Mastering card

`Rain File` + `Rain...`
1. Optional ambience layer.
2. If set, the rain track is looped across the mix duration.
3. Render runs a short rain compatibility preflight before full processing.
4. If preflight fails, render stops early with a rain-specific error (it does not auto-disable rain).

`Rain Vol`
1. Rain gain in dB.
2. More negative values are quieter.

### Length And Scope card

`Crossfade` (seconds)
1. Base overlap target between adjacent tracks.
2. If smart crossfade is ON, real transition length can differ.
3. If tracks are very short, safety bounds still limit overlap.

`Target LUFS`
1. LUFS = perceived loudness over time (more useful than peak dB for mix loudness matching).
2. `Target LUFS` is the loudness goal used by normalization.
3. Higher target (for example `-10`) sounds louder but reduces headroom and can feel more compressed.
4. Lower target (for example `-18`) sounds quieter with more headroom and less loudness stress.

`Loop To Target` + numeric field (`min`)
1. The numeric field is **minutes**.
2. OFF: one cycle through track list.
3. ON: repeat cycles until estimated timeline reaches target minutes.
4. The numeric field is disabled until `Loop To Target` is checked. This is expected behavior.

`Preview Mode` + numeric field (`sec`)
1. The numeric field is **seconds**.
2. ON: final `Render` exports only a short excerpt.
3. OFF: final `Render` exports full timeline.
4. Preview duration also drives `Render Scope` and estimate calculations.
5. The numeric field is disabled until `Preview Mode` is checked. This is expected behavior.
6. This toggle affects `Render`; `Short Preview` button always builds a short excerpt regardless of this toggle.

### Reports card (center panel)

`Adaptive Report` + browse button
1. Output path for adaptive report JSON.
2. Used when adaptive mode is enabled.

`Render Cache Folder` + `Cache...`
1. This is the temp workspace used for intermediate render files (large lossless processing files).
2. If left empty, the app uses the system temp folder (often on drive `C:` on Windows).
3. For long mixes, set this to a drive with large free space to avoid render failures.
4. `Cache...` opens a folder picker for this location.

## 5. Left panel

`Songs Folder`
1. Required for analysis.
2. Must exist and be a directory.

`Folder...`
1. Opens folder picker.
2. Preloads track names into the table before full analysis.
3. If valid per-track analysis JSON cache exists, cached BPM/Key are shown immediately.

`Analyze`
1. Scans audio files and runs analysis.
2. Builds timeline and transition plan.
3. Output path is not required for Analyze.
4. Reuses valid per-track analysis cache (`*.nightfall_analysis.json`) when available, so repeated runs are faster.

Validation label (red text)
1. Shows current blocking issue.
2. Render-specific errors (like missing output path) are shown after analysis when relevant.

Track table columns:
1. `#`: timeline order.
2. `Track`: file name.
3. `Duration`: track duration.
4. `BPM`: detected BPM (if available). The app normalizes BPM into a lo-fi-friendly range (roughly 60-120) to reduce common double-time/half-time misreads.
5. `Key`: detected key (if available).
6. `Cache`: reusable analysis cache state (`Full`, `Partial`, `--`).
7. `Lo-Fi`: adaptive need bucket (`Low`, `Mid`, `High`, `--`).

`Cache` meanings:
1. `Full`: per-track analysis cache has loudness + BPM/Key + RMS edge data.
2. `Partial`: only some reusable data exists (for example adaptive metrics only).
3. `--`: no valid cache found for that track.

`Lo-Fi` bucket meaning:
1. `Low`: track already sounds lo-fi enough; adaptive processing is light.
2. `Mid`: moderate adaptation; some lo-fi shaping is applied.
3. `High`: track sounds cleaner/modern; stronger lo-fi adaptation is applied.
4. `--`: adaptive mode is off or score unavailable.
5. Internally, these buckets come from an adaptive score:
   - `< 35` => `Low`
   - `< 70` => `Mid`
   - `>= 70` => `High`

Track drag/drop:
1. Changes timeline order immediately.
2. Rebuilds plan and estimates after reorder.

`Remove` button (next to Analyze):
1. Removes selected rows from the current Lo-Fi working list.
2. If analysis already exists, timeline is rebuilt from remaining tracks.
3. Removed tracks stay excluded until you reload folder / reanalyze with a different list.

## 6. Center panel (Timeline)

1. Shows each track block with start/end positions.
2. Visual overlap regions correspond to crossfades.
3. Rain lane appears when rain file is present.
4. Tooltips show track, start, and end times.

## 7. Right panel (Selected Track Details)

`Adaptive Analysis` box fields:
1. `lufs`
2. `crest_factor_db`
3. `spectral_centroid_hz`
4. `rolloff_hz`
5. `stereo_width`
6. `noise_floor_dbfs`

`Applied Processing` box fields:
1. `lpf_cutoff_hz`
2. `saturation_strength`
3. `compression_strength`
4. `stereo_width_target`
5. `noise_added_db`

`Explanation`
1. Text rationale for adaptive decision path.
2. Shows fallback rationale when adaptive analysis is unavailable.

## 8. Bottom panel

### Output row

`Output`
1. Final render destination path.
2. Required for final `Render`.

Format dropdown (`mp3`/`wav`)
1. Chooses final export format.
2. Also used by estimate size math.

`Bitrate`
1. MP3 bitrate selector (`128k`, `160k`, `192k`, `256k`, `320k`).
2. Higher bitrate = larger file and usually higher quality.
3. Disabled when output format is `wav` (WAV uses fixed PCM size behavior).

`Split MP3` + chunk minutes
1. When checked, render also creates chunk files after main output is finished.
2. Chunk duration is in minutes.
3. Example: 35 minutes total with 10-minute chunks -> `10, 10, 10, 5` (4 files).
4. Chunking applies to final MP3 render, not WAV.

`Output...`
1. Opens save dialog.
2. Selecting `.mp3` or `.wav` auto-syncs the format dropdown.

### Estimate row

`Timeline`
1. Full expected mix length from current plan.

`Render Scope`
1. If Preview Mode OFF: same as timeline.
2. If Preview Mode ON: preview duration and `(preview)` tag.

`Estimated Size`
1. Estimate based on `Render Scope` and selected format.
2. WAV estimate assumes 48kHz stereo 16-bit PCM.
3. MP3 estimate uses current default bitrate path (192k).

### Logs and progress

Progress bar:
1. During analysis: processed tracks / total.
2. During render: ffmpeg reported time vs expected scope.

Log console:
1. Analysis/render worker logs.
2. Warning samples and artifact paths.

### Actions

`Save Project`
1. Saves current settings, order, and per-preset overrides to `.nightfall`.
2. Also saves active workspace mode (`Simple` or `Advanced`).

`Load Project`
1. Loads settings/order from `.nightfall`.
2. Triggers analysis using loaded ordering hints.
3. Restores the saved workspace mode.

`Short Preview`
1. Builds a short excerpt preview to temp output (`%TEMP%/nightfall_preview/preview_mix.mp3`).
2. Uses the preview seconds value.
3. Best for quick transition checks.

`Full Preview`
1. Builds a full-length preview file (still temp output).
2. Best when you want player-like navigation across the whole timeline.
3. Does not overwrite your final output path.

Preview seek bar:
1. Appears under estimate row and behaves like a media scrubber.
2. You can jump to any point in the built preview output.
3. With full-timeline preview, this lets you navigate like a normal audio player.

`Play` / `Pause`
1. Plays the most recently built preview file.
2. Button toggles to `Pause` while playing.

`Stop`
1. Stops preview playback and resets player position.

`Preview: ...` status label
1. `not built`: no preview file yet.
2. `ready`: preview file exists and matches current settings.
3. `stale (rebuild recommended)`: settings/order changed since last preview build.
4. `playback unavailable`: Qt multimedia backend missing.

`Render`
1. Final export action.
2. Enabled only after successful analysis and valid output path.
3. Before rendering starts, a metadata dialog is shown so you can optionally set tags:
   - `title`
   - `artist`
   - `album`
   - `album artist`
   - `genre`
   - `year/date`
   - `composer`
   - `comment`
4. You can click `Skip Metadata` in that dialog and continue render without tags.
5. Produces output artifacts:
   - `tracklist.txt`
   - `tracklist.json`
   - `mix_timestamps.txt`
   - `mix_timestamps.csv`

`Cancel`
1. Requests cancellation of active analysis/render worker.

## 9. Preset editor fields

`LPF Cutoff (Hz)`
1. Explicit low-pass cutoff used only when override is enabled.

`Use preset LPF cutoff`
1. Checked: do not force LPF value; use preset baseline LPF.
2. Unchecked: use numeric LPF value from editor as override.

`Saturation Scale`
1. Multiplier for preset saturation strength.
2. `1.0` = preset baseline.

`Compression Scale`
1. Multiplier for preset compression behavior.
2. `1.0` = preset baseline.

## 10. Step-by-step workflows

### Workflow A: First successful full mix

1. Select `Simple` or `Advanced` tab.
2. Set `Songs Folder`.
3. Click `Analyze`.
4. Inspect track list and optionally drag/drop reorder.
5. Set `Output` path and output format.
6. Click `Render`.
7. Check log for `Output:` and generated `tracklist.*` plus `mix_timestamps.*` files.

### Workflow B: Simple mode quick-start

1. Open `Simple` tab.
2. Keep defaults or choose preset/output format.
3. Optionally enable `Set Mix Length`.
4. Click `Analyze`.
5. Click `Render`.
6. If needed, switch to `Advanced` for deeper tuning.

### Workflow C: Fast sound tuning with in-app preview

1. Analyze tracks first.
2. Set preview seconds (for example 45-60s).
3. Click `Short Preview`.
4. Click `Play` and listen.
5. Change preset/crossfade/rain/target/etc.
6. Watch status switch to `stale`.
7. Click `Short Preview` again.
8. Repeat until satisfied.
9. Set final output path and click `Render`.

### Workflow D: BPM/key-aware ordering workflow

1. Enable `Smart Crossfade` and `Smart Ordering`.
2. Choose smart ordering mode.
3. Click `Analyze` to apply ordering.
4. Review resulting order in table.
5. Manually drag/drop if specific transitions still need adjustment.

### Workflow E: Adaptive workflow

1. Enable `Adaptive Lo-Fi`.
2. Click `Analyze`.
3. Select tracks and inspect right-panel metrics.
4. Read `Explanation` rationale.
5. Set `Adaptive Report` path.
6. Render and review report outputs plus `mix_timestamps.txt/.csv`.

### Workflow F: Avoid disk-space render failures

1. In `Reports`, set `Render Cache Folder` to a spacious drive/folder.
2. Click `Render` (or preview buttons).
3. The app runs a pre-render storage estimate check.
4. If storage is insufficient, a warning appears with required estimate, available free space, and folder path.
5. Choose a larger cache folder and render again.

## 11. Known limits (current behavior)

1. Playback is preview-file based, not true real-time DSP streaming.
2. `Short Preview` and `Full Preview` rerun render logic before playback.
3. Smart ordering is applied during analysis, not continuously while editing.
4. Size estimation is approximate and codec/container overhead can differ.
5. Storage checks are estimates, not exact byte-for-byte guarantees.

## 12. MP3 Splitter tab

Purpose:
1. Split a single MP3 into fixed-length chunks.
2. Independent utility tab; does not depend on Lo-Fi analysis.

Controls:
1. `Input MP3`: source file.
2. `Output Folder`: destination directory.
3. `Chunk Length`: minutes per chunk.
4. `Bitrate`: output bitrate for generated chunks.
5. `Split MP3`: starts processing.
6. `Cancel`: requests cancellation.
7. Progress/log area: chunk-by-chunk status.

Output naming:
1. `<input_stem>_part_001.mp3`
2. `<input_stem>_part_002.mp3`
3. etc.

## 13. MP4 Stitcher tab

Purpose:
1. Stitch multiple clips from one folder into one MP4 file.
2. Default mode is straight glue concatenation.

Controls:
1. `Input Folder`: folder containing `.mp4/.m4v/.mov/.mkv`.
2. `Output MP4`: result file path.
3. `Load Files`: reads folder clips into editable list.
4. Clip list supports drag/drop reordering.
5. `Remove Selected`: removes selected clips from stitch list.
6. `Smart Ordering`: optional BPM/key-based ordering of clips using audio analysis.
7. `Smart Audio Fade`: optional audio crossfades at boundaries.
8. Crossfade seconds spinbox: base transition length for smart audio fades.
9. `Stitch MP4`: starts processing.
10. `Cancel`: requests cancellation.

Notes:
1. Smart audio fade affects audio transitions; video remains stitched with hard cuts.
2. Default (smart fade OFF) tries copy-concat first, then re-encode if copy-concat is incompatible.

## 14. Planned next version changes

This section describes features that are planned but not fully implemented yet.

### A. Simple mode expansion

1. Add optional guided wizard inside Simple tab (step-by-step prompts).
2. Add one-click profile presets (for example `Study`, `Sleep`, `Cafe`).

### B. Advanced mode transition tooling

1. Add transition inspector panel (reason, key distance, overlap details).
2. Add per-transition manual override controls.

## 15. Troubleshooting

1. `Analyze` disabled:
   - Verify `Songs Folder` exists.

2. `Render` disabled:
   - Analyze first.

3. Error mentions `No space left on device`:
   - Set `Render Cache Folder` to a drive with more free space.
   - Re-run render.
   - If final output folder is also low on space, change output path too.
   - Set a valid `Output` file path.

4. Error mentions rain preflight or rain compatibility:
   - The selected rain file failed a short compatibility test.
   - Choose a different rain file and re-run.
   - Keep rain enabled; the app does not auto-disable it.

5. Preview plays old sound:
   - If status is `stale`, rebuild preview (`Short Preview` or `Full Preview`).

6. No playback controls available:
   - Qt multimedia backend may be missing in runtime environment.

7. Abrupt transitions:
   - Increase `Crossfade (s)`.
   - Enable `Smart Crossfade`.
   - Reorder tracks manually after analysis.

8. Unexpected ordering:
   - Confirm whether `Smart Ordering` and/or `Shuffle` were enabled at analysis time.
