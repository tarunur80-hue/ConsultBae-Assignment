"""
Task 3: Mini audio collection app.
A person enters name + phone, records audio in-browser OR uploads a file,
and we auto-extract duration, sample rate, bitrate, and loudness (dB),
storing everything in the audio_submissions table (created back in
Task 1's DB schema) alongside the audio file itself.
"""
import os
import sqlite3
import uuid
import subprocess
import shutil
import tempfile
from pathlib import Path
from datetime import datetime

from flask import Flask, request, jsonify, render_template, send_from_directory
import numpy as np
import librosa

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "db" / "consultbae.db"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {"wav", "mp3", "webm", "ogg", "m4a"}


def find_ffmpeg():
    """
    Locate the ffmpeg executable without depending on the PATH of whatever
    shell happened to launch this Flask process (which caused WinError 2
    when the app was started from a terminal that hadn't picked up PATH
    changes yet). Checks PATH first, then common Windows winget/manual
    install locations, and finally an FFMPEG_PATH env var override.
    """
    found = shutil.which("ffmpeg")
    if found:
        return found

    env_override = os.environ.get("FFMPEG_PATH")
    if env_override and Path(env_override).exists():
        return env_override

    candidates = list(Path.home().glob(
        "AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg.Essentials_*/ffmpeg-*/bin/ffmpeg.exe"
    ))
    if candidates:
        return str(candidates[0])

    raise RuntimeError(
        "ffmpeg not found. Install it (winget install \"FFmpeg (Essentials Build)\") "
        "or set the FFMPEG_PATH environment variable to its full .exe path."
    )


FFMPEG_BIN = find_ffmpeg()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB cap per upload


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def analyze_audio(filepath):
    """
    Extract duration, sample rate, bitrate, loudness (dB), and a rough
    noise/quality estimate from an audio file.

    - duration_sec, sample_rate_hz: read directly via librosa
    - bitrate_kbps: estimated from file size / duration (works for any
      codec without needing per-format bitrate metadata parsing)
    - loudness_db: RMS energy converted to decibels (dBFS-style measure)
    - quality_estimate: rough heuristic bucket based on sample rate + loudness
    """
    # Browser recordings arrive as WebM/Opus, which soundfile/librosa can't
    # always decode directly. Convert everything to a temp WAV via ffmpeg
    # first, so decoding is reliable regardless of the source format.
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name
    try:
        subprocess.run(
            [FFMPEG_BIN, "-y", "-i", str(filepath), "-ar", "44100", wav_path],
            check=True, capture_output=True,
        )
        y, sr = librosa.load(wav_path, sr=None, mono=True)
    finally:
        Path(wav_path).unlink(missing_ok=True)

    duration_sec = round(librosa.get_duration(y=y, sr=sr), 2)

    file_size_bytes = os.path.getsize(filepath)
    bitrate_kbps = round((file_size_bytes * 8) / duration_sec / 1000, 1) if duration_sec > 0 else None

    rms = np.sqrt(np.mean(y ** 2)) if len(y) > 0 else 0
    loudness_db = round(20 * np.log10(rms), 2) if rms > 0 else -96.0  # floor for silence

    # rough quality heuristic (bonus ask in the brief - not a real noise
    # estimate, just a sensible signal from sample rate + loudness range)
    if sr >= 44100 and -30 <= loudness_db <= -3:
        quality_estimate = "good"
    elif sr >= 16000 and -45 <= loudness_db <= -1:
        quality_estimate = "acceptable"
    else:
        quality_estimate = "poor (check mic distance / sample rate)"

    return {
        "duration_sec": float(duration_sec),
        "sample_rate_hz": int(sr),
        "bitrate_kbps": float(bitrate_kbps) if bitrate_kbps is not None else None,
        "loudness_db": float(loudness_db),
        "quality_estimate": quality_estimate,
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/submissions")
def submissions_page():
    return render_template("submissions.html")


@app.route("/api/submit", methods=["POST"])
def submit_audio():
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()

    if not name or not phone:
        return jsonify({"error": "Name and phone are required."}), 400

    if "audio" not in request.files:
        return jsonify({"error": "No audio file received."}), 400

    file = request.files["audio"]
    if file.filename == "":
        return jsonify({"error": "Empty filename."}), 400

    # browser recordings often arrive as .webm blobs without an extension
    # set correctly by the client - default to webm if none provided
    orig_name = file.filename
    ext = orig_name.rsplit(".", 1)[-1].lower() if "." in orig_name else "webm"
    if ext not in ALLOWED_EXTENSIONS:
        ext = "webm"

    unique_name = f"{uuid.uuid4().hex}.{ext}"
    filepath = UPLOAD_DIR / unique_name
    file.save(filepath)

    try:
        metrics = analyze_audio(filepath)
    except Exception as e:
        filepath.unlink(missing_ok=True)
        return jsonify({"error": f"Could not process audio: {e}"}), 422

    conn = get_db()
    c = conn.cursor()
    c.execute(
        """INSERT INTO audio_submissions
           (name, phone, filename, duration_sec, sample_rate_hz,
            bitrate_kbps, loudness_db, quality_estimate)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, phone, unique_name, metrics["duration_sec"], metrics["sample_rate_hz"],
         metrics["bitrate_kbps"], metrics["loudness_db"], metrics["quality_estimate"]),
    )
    conn.commit()
    conn.close()

    return jsonify({"success": True, "metrics": metrics})


@app.route("/api/submissions")
def list_submissions():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM audio_submissions ORDER BY submitted_at DESC"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/uploads/<filename>")
def serve_audio(filename):
    return send_from_directory(UPLOAD_DIR, filename)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
