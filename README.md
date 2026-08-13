# ConsultBae AI Automation Assignment

Merges 3 messy CSV exports into one database, automates skill-tagging with an LLM via n8n, and provides a mini audio collection app with automatic audio analysis.

## Repo structure

```
data/               # original 3 source CSVs
db/                 # generated SQLite database (created by running scripts/01)
scripts/            # Task 1 & 2 pipeline scripts
n8n/                # exported n8n workflow JSON (Task 2)
audio_app/          # Task 3 Flask app
docs/               # data issues report (Task 4), stretch doc (Task 5), n8n export data
```

## Setup

### Prerequisites
- Python 3.10+
- [ffmpeg](https://www.gyan.dev/ffmpeg/builds/) installed and on your PATH (needed for audio format conversion in Task 3)
- An n8n Cloud account (free trial) if you want to re-run the automation live
- A free [Groq API key](https://console.groq.com) if re-running the n8n workflow

### Task 1 — Merge pipeline

```bash
pip install pandas
python scripts/01_merge_pipeline.py
```

This reads the 3 CSVs from `data/`, cleans and normalizes them, resolves duplicate people across sources via a union-find match on phone/email, and writes everything into `db/consultbae.db`. It also writes a machine-readable issues log to `docs/data_issues_log.json`.

### Task 2 — n8n automation

The exported workflow is at `n8n/skill_tagging_flow.json` — importable directly into any n8n instance (Cloud or self-hosted). It:
1. Loads each person + their skills (exported via `scripts/02_export_for_n8n.py`, since n8n Cloud can't reach a local file)
2. Loops through them one at a time
3. Calls the Groq API (Llama 3.3 70B) to classify each person's skill set into `automation-heavy` / `web dev` / `data`
4. Waits 3 seconds between calls (Groq free tier rate limit)
5. Aggregates all results

To re-run it: import the flow into n8n, add your own Groq API key as a Header Auth credential, and execute.

Results are written back into the database with:
```bash
python scripts/03_import_tags.py
```
(expects `docs/n8n_tagged_results.json`, the Aggregate node's output)

<img width="1910" height="708" alt="Untitled" src="https://github.com/user-attachments/assets/23ac2253-d2b1-4e18-adbb-fe1c94c604d8" />


### Task 3 — Audio collection app

```bash
cd audio_app
pip install -r requirements.txt
python app.py
```

Then open `http://127.0.0.1:5000`. Enter a name + phone, either record in-browser (mic access required) or upload an audio file, and submit. Duration, sample rate, bitrate, loudness, and a rough quality estimate are extracted automatically via `librosa` + `ffmpeg`, and a record is written to the same `consultbae.db` from Task 1 (`audio_submissions` table).

View all submissions with play buttons at `http://127.0.0.1:5000/submissions`.

<img width="601" height="253" alt="image" src="https://github.com/user-attachments/assets/89651a5d-e6cc-48ea-888f-55f39f28fbcf" />


## Task 4 — Data issues report

See [`docs/data_issues_report.md`](docs/data_issues_report.md) for the full breakdown of every planted data quality problem found (inconsistent units, formats, casing, duplicate rows, a column-shifted/corrupted row, a duplicated header, and two name-collision traps) and exactly what was done about each.

## Task 5 — Stretch

See [`docs/stretch_scale_5000.md`](docs/stretch_scale_5000.md) for what breaks first if this audio app launched to 5,000 gig workers in one weekend, and what I'd change before that launch.

## Stuck log

**1. Numpy float32 wasn't JSON-serializable, and the bug silently corrupted the database.**
The audio analysis functions (loudness via RMS, sample rate) returned numpy scalar types instead of native Python floats. Flask's `jsonify()` threw `TypeError: Object of type float32 is not JSON serializable` — but by the time that error fired, the SQLite `INSERT` had already committed, since the commit happened before the JSON response was built. That left corrupted rows sitting in the database that broke the "list submissions" endpoint on a completely unrelated request later. I fixed the root cause by explicitly casting every value to native Python types (`float()`, `int()`) before returning them, and cleared the stale rows that had already been written.

**2. Browser-recorded audio (WebM/Opus) wouldn't decode even with ffmpeg installed.**
`librosa.load()` failed with "Format not recognised" on browser-recorded WebM files, despite ffmpeg being correctly installed and on PATH. Soundfile (librosa's default backend) doesn't automatically route through ffmpeg for every codec. Rather than debug backend selection further, I made the pipeline explicitly convert every uploaded file to WAV via an `ffmpeg` subprocess call before analysis — this guarantees consistent decoding regardless of the source format (WebM, MP3, M4A, etc.) instead of depending on which backend happens to support what.

**3. Windows PATH changes weren't propagating to Git Bash even after a full restart.**
After installing ffmpeg via `winget`, `ffmpeg -version` kept failing with "command not found" in Git Bash — even in a completely fresh terminal window, and even after editing the System PATH manually through Windows' environment variable dialog. I verified the actual binary existed at the expected install path (it did), which meant the issue was specifically Git Bash's `$PATH` not picking up the Windows-level change. Instead of continuing to fight Windows' PATH propagation, I appended the ffmpeg `bin` directory directly to Git Bash's own `~/.bashrc`, which is more reliable and persists regardless of how Windows handles system-level PATH updates.

**What I asked Claude for help with, and what I rejected:** Claude's first suggestion for handling numpy types was to wrap the entire `jsonify()` call output, which would have masked the bug rather than fixing where the numpy scalar was created — I asked for the fix to happen at the source (inside `analyze_audio()`) instead, so the database itself never receives non-native types again.
