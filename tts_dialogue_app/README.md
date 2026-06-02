# TTS Dialogue App — ElevenLabs Multi-Voice Converter

A Windows desktop app (PySide6) that turns a written **dialogue** into
**multi-voice audio** using the [ElevenLabs](https://elevenlabs.io)
Text-to-Speech (and Text-to-Dialogue) APIs — built for a **video-creation
workflow** (one scene / image per dialogue chunk).

Paste a script like:

```
A: hello
B: abcde
C: abcdef
A: bbbb
```

…the app detects the characters, lets you assign a distinct voice + style preset
to each, converts every line, and lets you download each line, export per
character, merge the whole dialogue, and export **timeline + subtitles** for
CapCut / Camtasia / Veo.

---

## ✨ Features

**Core**
- Auto-detect characters from `Character: dialogue` (supports `A:`, `Mom:`,
  `Lucy:`, `Narrator:`, `Mẹ:`, `Bé:`, full-width `：`, etc.).
- Live **voices** and **models** loaded from your ElevenLabs account (all voices,
  paginated — not just the first page).
- **Voice Library browser**: search the huge public ElevenLabs catalogue filtered
  by **language / gender / age / category / keyword**, preview, and **Add** a
  voice to your account so it's selectable per character. Voice dropdowns show a
  clear **Nam / Nữ / Trẻ em · độ tuổi · ngôn ngữ** descriptor (type to filter).
- Per-character config: voice, model, preset, speed, stability, similarity,
  style, speaker boost — with searchable voice dropdown, auto-assign, duplicate.
- 10 style presets (Neutral, Fast, Slow, Strong, Soft, Emotional, Happy, Calm,
  Childlike, Narration), all still hand-tweakable.

**v2 advanced**
- **Convert modes**: *Line-by-line TTS* (stable, default), *Dialogue API*
  (natural, short scripts), and *Auto* (decides by length + voice count, and
  **falls back to line-by-line** automatically if the Dialogue API is
  unavailable or errors — never crashes).
- **Smart batch splitter**: never splits a line/sentence; configurable
  *max chars per batch*; **Preview Batches**; warns on over-long lines.
- **Pronunciation** tab: find/replace rules (Đậu → Dow, Lucy → Loo-see …),
  Add/Delete, **Import/Export CSV**, toggle apply-to-preview / apply-to-conversion.
  Uses the API pronunciation-dictionary locator when available, otherwise
  substitutes text — **the original text is always kept for subtitles/export**.
- **Model manager**: lists only models with `can_do_text_to_speech`; disables
  the *style* slider / *speaker boost* checkbox when a model can't use them.
  **Refresh Models** + **Set Default Model**.
- **Local cache** (SHA256 of text+voice+model+settings): identical lines reuse
  the previous audio → saves credits. Enable/Clear/size display + **Force
  regenerate selected**.
- **Usage / cost estimate**: lines, characters, total chars, per-character
  breakdown, **billable chars after cache**, and warnings.
- **Scene grouping** for video: each line / on speaker change / every N lines /
  manual — preserved in the timeline export.
- **Exports for video editing**: merged audio, per-character audio, **SRT** from
  real merged durations, and **timeline CSV + JSON** (scene_index, line_index,
  character, text, start/end/duration, audio_file, voice_name, voice_id,
  model_id).
- **Audio post-processing**: normalize, trim silence, fade in/out, sample rate
  (44100/48000), MP3 bitrate (128/192/320k), silences between lines & on speaker
  change.
- **Pause / Resume / Cancel**, **Retry failed**, **Retry selected**, per-line
  retry, and **exponential backoff** on rate limits (HTTP 429).
- Detailed **log panel** (API connected, voices/models loaded, parsed, cache
  hit/miss, converting x/y, exports, errors) with **Save log** / **Clear log**.
- **Save / Load project** (`.json`, includes pronunciation rules + all settings),
  recent projects. API key stored locally (`config.json` or `.env`), never
  hard-coded.

---

## 🗂 Project structure

```
tts_dialogue_app/
├─ main.py
├─ requirements.txt
├─ README.md
├─ build_exe.bat
├─ app/
│  ├─ gui/
│  │  ├─ main_window.py     # the whole UI (3 tabs + log)
│  │  ├─ widgets.py         # config table (+ model caps) + searchable combo
│  │  └─ workers.py         # QThread workers (API / convert / merge / preview)
│  ├─ core/
│  │  ├─ parser.py            # parse_dialogue()
│  │  ├─ elevenlabs_client.py # voices, models, TTS, backoff
│  │  ├─ dialogue_api_client.py # Text-to-Dialogue API + fallback
│  │  ├─ batch_splitter.py    # smart batching
│  │  ├─ pronunciation_manager.py
│  │  ├─ cache_manager.py     # SHA256 audio cache
│  │  ├─ usage_estimator.py   # char/cost estimate
│  │  ├─ timeline_exporter.py # CSV + JSON timeline
│  │  ├─ subtitle_exporter.py # .srt from real durations
│  │  ├─ audio_processor.py     # low-level: load/duration/speed
│  │  ├─ audio_postprocessor.py # merge/normalize/trim/fade/bitrate
│  │  ├─ project_manager.py   # save/load .json
│  │  └─ models.py            # dataclasses
│  └─ config/
│     └─ settings.py          # presets, models, local config, cache dir
├─ assets/                  # optional icon.ico
└─ outputs/                 # generated audio (per project)
```

---

## ▶️ Run from source (step by step)

You need **Python 3.11+** (tested on 3.13) on Windows.

```bat
REM 1. Open a terminal in the tts_dialogue_app folder

REM 2. Install Python 3.11+ from https://www.python.org/downloads/ (tick "Add to PATH")

REM 3. (recommended) create & activate a virtual environment
python -m venv .venv
.venv\Scripts\activate

REM 4. install requirements
pip install -r requirements.txt

REM 5. install ffmpeg (see the ffmpeg section below)

REM 6. run the app
python main.py
```

> **Python 3.13 note:** `pydub` depends on the stdlib `audioop` module, which was
> **removed in Python 3.13** (PEP 594). `requirements.txt` automatically installs
> the `audioop-lts` backport on 3.13+, so merging/export keep working. On Python
> ≤ 3.12 nothing extra is needed.

### Using the app (recommended flow)

1. **Setup & Voices** tab → enter **API Key** → **Test API**.
2. **Load Voices**, then **Load Models** (enables capability-aware controls).
3. Paste your dialogue → **Detect Characters**.
4. Pick a **voice** per character (or **Auto-assign Voices**), choose presets,
   tweak sliders. Use **▶ Preview** to listen.
5. (Optional) **Pronunciation** tab → add rules / import CSV.
6. **Queue & Convert** tab → set output folder/format, **Convert mode**, silences,
   **audio post-processing**, **cache**, **scene** grouping.
7. **Build Queue** → optionally **Estimate Usage** / **Preview Batches**.
8. **Convert** (Pause/Resume/Cancel, Retry failed/selected as needed).
9. **Merge Full Dialogue** / **Export by Character** / **Export .srt** /
   **Export Timeline (CSV+JSON)**.

Outputs:

```
<output folder>/<project name>/
├─ lines/         0001_A.mp3, 0002_B.mp3, ...   (line-by-line mode)
│                 batch_0001.mp3, ...           (dialogue mode)
├─ merged/        full_dialogue.mp3
├─ by_character/  A_all.mp3, B_all.mp3, ...
├─ subtitles/     full_dialogue.srt
├─ timeline/      timeline.csv, timeline.json
└─ previews/      preview_<character>.mp3
```

> In **Dialogue API mode** the audio for a batch is one file carried by the
> batch's first line, so per-line subtitles/timeline are most precise in
> **Line-by-line mode**.

---

## 🎬 ffmpeg (required for merge / export / wav / speed / normalize)

`pydub` needs **ffmpeg** for anything beyond raw mp3 download.

**Option A — install and add to PATH (recommended):**
1. Download from <https://www.gyan.dev/ffmpeg/builds/> (`ffmpeg-release-essentials.zip`),
   or `winget install Gyan.FFmpeg`.
2. Extract (e.g. `C:\ffmpeg`) and add `C:\ffmpeg\bin` to your **PATH**.
3. New terminal → verify with `ffmpeg -version`.

**Option B — drop ffmpeg next to the app:**
Place `ffmpeg.exe` (and `ffprobe.exe`) next to `main.py` (from source) or next to
`TTS_Dialogue_App.exe` (built). The app auto-detects them on startup.

---

## 📦 Build a Windows `.exe`

```bat
build_exe.bat
```
This creates `.venv`, installs requirements, runs
`pyinstaller --onefile --windowed main.py`, and produces
**`dist\TTS_Dialogue_App.exe`**. ffmpeg is still required at runtime (PATH or
next to the `.exe`).

---

## 🔑 Changing API key, model, and voice settings

- **API key**: enter in the app → saved to `config.json`. Or create `.env`:
  ```
  ELEVENLABS_API_KEY=xi-your-key-here
  ```
  Never hard-coded in source.
- **Models**: **Load Models** pulls the live list; pick a **Default model** and
  **Set Default Model**, or change per character in the table. Fallback defaults
  live in `AVAILABLE_MODELS` in `app/config/settings.py`.
- **Voice settings / presets**: edit sliders per character or tune the `PRESETS`
  dict in `app/config/settings.py`.

### `speed` handling
ElevenLabs doesn't support `speed` on every model. The app sends it when
expected; if the API rejects it (422) it retries without `speed` and applies the
change locally via pydub. Known-unsupported models are in `MODELS_WITHOUT_SPEED`
in `app/core/elevenlabs_client.py`. The app never crashes if a parameter is
unsupported — it falls back gracefully.

---

## 🧰 Troubleshooting

| Problem | Fix |
| --- | --- |
| `Invalid API key (401)` | Re-check the key; **Test API**. |
| `Rate limit / quota (429)` | The app waits with exponential backoff and retries; or upgrade your plan. |
| `No module named 'audioop'` | You're on Python 3.13 — `pip install -r requirements.txt` (pulls `audioop-lts`). |
| Merge/export error mentioning ffmpeg | Install ffmpeg or drop `ffmpeg.exe` next to the app. |
| Dialogue mode silent / errors | App auto-falls back to line-by-line; check the log. |
| No voices/models in dropdowns | Click **Load Voices** / **Load Models** after entering the key. |

---

## 📝 Disclaimer

This app calls a third-party paid API (ElevenLabs). Audio generation consumes
your character quota (the cache and usage estimate help you control it). Use
responsibly and comply with ElevenLabs' terms of service.
