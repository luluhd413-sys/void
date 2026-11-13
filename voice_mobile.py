"""
VoiceForge Studio - Gemini TTS control panel (ek hi file).

Install once:
    pip install flask requests Babel

Run:
    python voice_said.py
VoiceForge Studio opens in your browser.
"""

import base64
import io
import json
import os
import queue
import re
import shutil
import socket
import sys
import threading
import time
import wave
import webbrowser
from datetime import datetime

from babel import Locale
import requests
from flask import Flask, Response, jsonify, request, send_from_directory

GUI_ENABLED = (
    os.environ.get("VOICEFORGE_ENABLE_GUI", "1").lower() not in {"0", "false", "no"}
)
if sys.platform == "linux" and os.environ.get("DISPLAY") is None and os.environ.get("WAYLAND_DISPLAY") is None:
    GUI_ENABLED = False

if GUI_ENABLED and "ANDROID_ARGUMENT" not in os.environ:
    import tkinter as tk
    from tkinter import messagebox, ttk

# The API key is entered in the app and kept only for the current page session.

PORT = int(os.environ.get("PORT", "5055"))
MAX_TEXT_LENGTH = 10_000

if "ANDROID_ARGUMENT" in os.environ:
    from android.storage import app_storage_path

    APP_DATA_DIR = app_storage_path()
    OUTPUT_FOLDER = os.path.join(APP_DATA_DIR, "VoiceForge")
else:
    APP_DATA_DIR = os.environ.get("VOICEFORGE_APP_DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
    OUTPUT_FOLDER = os.environ.get(
        "VOICEFORGE_OUTPUT_FOLDER",
        os.path.join(APP_DATA_DIR, "output"),
    )

# ====== NETWORK FIX: IPv4 force + proxy detection band ======
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
    except socket.gaierror:
        return _orig_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = _ipv4_getaddrinfo

_session = requests.Session()
_session.trust_env = False
# ============================================================

# Generate ki hui voice pehle yahan (cache) me rehti hai.
# Download button dabane par hi OUTPUT_FOLDER me save hoti hai.
CACHE_DIR = os.path.join(APP_DATA_DIR, "cache")
HISTORY_FILE = os.path.join(CACHE_DIR, "history.json")
os.makedirs(CACHE_DIR, exist_ok=True)

MODELS = [
    "gemini-2.5-flash-preview-tts",
    "gemini-3.1-flash-tts-preview",
    "gemini-2.5-pro-preview-tts",
]

VOICES = [
    ("Zephyr", "Bright"), ("Puck", "Upbeat"), ("Charon", "Informative"),
    ("Kore", "Firm"), ("Fenrir", "Excitable"), ("Leda", "Youthful"),
    ("Orus", "Firm"), ("Aoede", "Breezy"), ("Callirrhoe", "Easy-going"),
    ("Autonoe", "Bright"), ("Enceladus", "Breathy"), ("Iapetus", "Clear"),
    ("Umbriel", "Easy-going"), ("Algieba", "Smooth"), ("Despina", "Smooth"),
    ("Erinome", "Clear"), ("Algenib", "Gravelly"), ("Rasalgethi", "Informative"),
    ("Laomedeia", "Upbeat"), ("Achernar", "Soft"), ("Alnilam", "Firm"),
    ("Schedar", "Even"), ("Gacrux", "Mature"), ("Pulcherrima", "Forward"),
    ("Achird", "Friendly"), ("Zubenelgenubi", "Casual"), ("Vindemiatrix", "Gentle"),
    ("Sadachbia", "Lively"), ("Sadaltager", "Knowledgeable"), ("Sulafat", "Warm"),
]

TONES = {
    "warm":     ("Warm",     "in a warm tone"),
    "cheerful": ("Cheerful", "in a cheerful, happy and energetic tone"),
    "calm":     ("Calm",     "in a calm, soothing and relaxed tone"),
    "news":     ("News",     "like a professional news anchor"),
    "whisper":  ("Whisper",  "in a soft whisper"),
}

SPEEDS = {
    "slow":   ("Slow",   "at a slow, clear pace"),
    "normal": ("Normal", "at a normal, natural conversational pace"),
    "fast":   ("Fast",   "at a fast pace with no long pauses"),
    "turbo":  ("Turbo",  "at a very fast, rapid pace with no long pauses"),
}

LANGUAGES = {
    "auto": ("Auto-detect", ""),
}
for language_code, language_name in Locale.parse("en").languages.items():
    if len(language_code) in (2, 3) and language_code not in {"bho", "mul", "und", "zxx", "mis", "sgn"}:
        LANGUAGES[language_code] = (language_name, language_name)
LANGUAGES["bhojpuri"] = ("Bhojpuri", "Bhojpuri")


def language_instruction(language):
    language_name = (
        language.removeprefix("custom:").strip()
        if language.startswith("custom:")
        else LANGUAGES[language][1]
    )
    if not language_name:
        return ""
    return (
        f"Translate the following text into natural {language_name} if needed, "
        f"preserve its meaning, and read it aloud with natural {language_name} pronunciation."
    )

app = Flask(__name__)
lock = threading.Lock()


# ---------------------------------------------------------------- helpers
def load_history():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_history(items):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def pcm_to_wav_bytes(pcm, rate):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def encode_audio(pcm, rate):
    """MP3 (lameenc) try karta hai, na ho to WAV. Return (bytes, ext)."""
    try:
        import lameenc
        enc = lameenc.Encoder()
        enc.set_bit_rate(128)
        enc.set_in_sample_rate(rate)
        enc.set_channels(1)
        enc.set_quality(2)
        return bytes(enc.encode(pcm) + enc.flush()), "mp3"
    except Exception:
        return pcm_to_wav_bytes(pcm, rate), "wav"


def api_error_message(resp):
    try:
        return resp.json()["error"]["message"]
    except Exception:
        return resp.text[:300]


def request_api_key(value):
    api_key = value if isinstance(value, str) else os.environ.get("GEMINI_API_KEY", "")
    api_key = api_key.strip()
    return api_key if 0 < len(api_key) <= 512 else ""


def synth(text, voice, tone, speed, language, api_key=None):
    api_key = request_api_key(api_key)
    if not api_key:
        raise RuntimeError("Enter your Gemini API key to generate speech.")

    tone_text = TONES.get(tone, TONES["warm"])[1]
    speed_text = SPEEDS.get(speed, SPEEDS["normal"])[1]
    instruction = language_instruction(language)
    if instruction:
        prompt = (
            f"{instruction} Read it {speed_text} with clear pronunciation, "
            f"{tone_text}:\n{text}"
        )
    else:
        prompt = f"Read this aloud {speed_text} with clear pronunciation, {tone_text}:\n{text}"

    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}
            },
        },
    }

    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        r = _session.post(
            url,
            headers={"x-goog-api-key": api_key},
            json=body,
            timeout=(15, 120),
            proxies={"http": None, "https": None},
        )
        if r.status_code == 429:
            continue  # is model ka free limit khatam, agla try
        if r.status_code != 200:
            raise RuntimeError(api_error_message(r))
        try:
            part = r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
        except Exception:
            raise RuntimeError("No audio was returned. Please try different text.")
        pcm = base64.b64decode(part["data"])
        m = re.search(r"rate=(\d+)", part.get("mimeType", ""))
        rate = int(m.group(1)) if m else 24000
        return pcm, rate, model

    raise RuntimeError(
        "All available models returned a rate-limit error. Check your quota in "
        "Google AI Studio and try again later."
    )


# ---------------------------------------------------------------- routes
@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.route("/api/config")
def config():
    return jsonify(
        voices=[{"name": n, "desc": d} for n, d in VOICES],
        tones=[{"id": k, "label": v[0]} for k, v in TONES.items()],
        speeds=[{"id": k, "label": v[0]} for k, v in SPEEDS.items()],
        languages=[
            {"id": key, "label": LANGUAGES[key][0]}
            for key in ["auto"] + sorted(k for k in LANGUAGES if k != "auto")
        ],
        folder=OUTPUT_FOLDER,
    )


@app.route("/api/preview", methods=["POST"])
def preview_voice():
    data = request.get_json(force=True, silent=True) or {}
    voice = data.get("voice")
    api_key = request_api_key(data.get("apiKey"))
    if not api_key:
        return jsonify(error="Enter your Gemini API key first."), 400
    if voice not in {name for name, _ in VOICES}:
        return jsonify(error="Select a valid voice."), 400

    sample = f"Welcome to VoiceForge Studio. I am {voice}. This is my voice sample."
    try:
        pcm, rate, _model = synth(sample, voice, "warm", "normal", "auto", api_key)
        audio, ext = encode_audio(pcm, rate)
    except requests.exceptions.RequestException as error:
        return jsonify(error=f"Network error: {error}"), 502
    except Exception as error:
        return jsonify(error=str(error)), 400

    mime_type = "audio/mpeg" if ext == "mp3" else "audio/wav"
    return Response(audio, mimetype=mime_type, headers={"Cache-Control": "no-store"})


@app.route("/api/tts", methods=["POST"])
def tts():
    data = request.get_json(force=True, silent=True) or {}
    api_key = request_api_key(data.get("apiKey"))
    if not api_key:
        return jsonify(error="Enter your Gemini API key first."), 400
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify(error="Text cannot be empty."), 400
    if len(text) > MAX_TEXT_LENGTH:
        return jsonify(error=f"Text cannot exceed {MAX_TEXT_LENGTH:,} characters."), 400

    voice = data.get("voice") or "Achernar"
    tone = data.get("tone") or "warm"
    speed = data.get("speed") or "normal"
    language = data.get("language") or "auto"
    custom_language = (
        isinstance(language, str)
        and language.startswith("custom:")
        and 0 < len(language.removeprefix("custom:").strip()) <= 80
    )
    if not isinstance(language, str) or (language not in LANGUAGES and not custom_language):
        return jsonify(error="Invalid language option."), 400
    t0 = time.time()
    try:
        pcm, rate, model = synth(text, voice, tone, speed, language, api_key)
    except requests.exceptions.RequestException as e:
        return jsonify(error=f"Network error: {e}"), 502
    except Exception as e:
        return jsonify(error=str(e)), 400

    audio, ext = encode_audio(pcm, rate)
    name = datetime.now().strftime("voice_%Y%m%d_%H%M%S_%f")[:-3] + "." + ext
    with open(os.path.join(CACHE_DIR, name), "wb") as f:
        f.write(audio)

    entry = {
        "file": name,
        "text": text,
        "voice": voice,
        "tone": tone,
        "speed": speed,
        "language": language,
        "model": model,
        "seconds": round(len(pcm) / 2 / rate, 1),
        "took": round(time.time() - t0, 1),
        "time": datetime.now().strftime("%d %b %Y, %I:%M %p"),
    }
    with lock:
        items = load_history()
        items.insert(0, entry)
        save_history(items)

    entry["url"] = f"/voices/{name}"
    return jsonify(entry)


@app.route("/api/history")
def history():
    items = load_history()
    # sirf wahi dikhao jinki file abhi bhi folder me hai
    items = [i for i in items if os.path.exists(os.path.join(CACHE_DIR, i["file"]))]
    for it in items:
        it["url"] = f"/voices/{it['file']}"
    return jsonify(items)


@app.route("/api/history/<name>", methods=["DELETE"])
def delete_item(name):
    name = os.path.basename(name)
    with lock:
        items = [i for i in load_history() if i["file"] != name]
        save_history(items)
    try:
        os.remove(os.path.join(CACHE_DIR, name))
    except OSError:
        pass
    return jsonify(ok=True)


@app.route("/api/save/<name>", methods=["POST"])
def save_to_folder(name):
    """Download button: file ko OUTPUT_FOLDER me save karta hai."""
    name = os.path.basename(name)
    src = os.path.join(CACHE_DIR, name)
    if not os.path.exists(src):
        return jsonify(error="Audio file was not found."), 404
    try:
        os.makedirs(OUTPUT_FOLDER, exist_ok=True)
        dst = os.path.join(OUTPUT_FOLDER, name)
        shutil.copy2(src, dst)
    except Exception as e:
        return jsonify(error=f"Could not save audio: {e}"), 500
    return jsonify(path=dst)


@app.route("/voices/<path:name>")
def get_voice(name):
    return send_from_directory(CACHE_DIR, os.path.basename(name))


# ---------------------------------------------------------------- UI
PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VoiceForge Studio</title>
<style>
:root{--bg:#090b16;--card:rgba(255,255,255,.045);--bd:rgba(255,255,255,.09);--tx:#eceefb;--mut:#8b91b5;
--a:#7c5cff;--b:#00d4ff;--ok:#2bd88a;--err:#ff5c7a;--r:18px}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Inter,Segoe UI,system-ui,sans-serif;color:var(--tx);min-height:100vh;
background:radial-gradient(900px 500px at 10% -10%,rgba(124,92,255,.28),transparent),
radial-gradient(800px 500px at 100% 0%,rgba(0,212,255,.18),transparent),var(--bg)}
.wrap{max-width:1200px;margin:0 auto;padding:22px 18px 60px}
header{display:flex;align-items:center;gap:14px;margin-bottom:22px}
.logo{width:46px;height:46px;border-radius:14px;background:linear-gradient(135deg,var(--a),var(--b));
display:grid;place-items:center;font-size:24px;box-shadow:0 8px 30px rgba(124,92,255,.45)}
h1{font-size:24px}
h1 span{background:linear-gradient(90deg,var(--a),var(--b));-webkit-background-clip:text;color:transparent}
.sub{color:var(--mut);font-size:12.5px;margin-top:2px;word-break:break-all}
.grid{display:grid;grid-template-columns:1.6fr 1fr;gap:18px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--bd);border-radius:var(--r);padding:18px;
backdrop-filter:blur(14px);margin-bottom:18px}
.card h2{font-size:13px;color:var(--mut);font-weight:600;letter-spacing:.8px;text-transform:uppercase;margin-bottom:12px;
display:flex;align-items:center;gap:8px}
textarea,input{width:100%;background:rgba(0,0,0,.35);border:1px solid var(--bd);color:var(--tx);
border-radius:12px;padding:12px 14px;font:inherit;font-size:15px;outline:none;transition:.2s}
select{width:100%;background:#11142a;border:1px solid var(--bd);color:var(--tx);border-radius:10px;
padding:10px 12px;font:inherit;font-size:14px;outline:none}
textarea{min-height:200px;resize:vertical;line-height:1.55}
textarea:focus,input:focus{border-color:var(--a);box-shadow:0 0 0 3px rgba(124,92,255,.22)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{padding:8px 14px;border-radius:99px;border:1px solid var(--bd);background:rgba(255,255,255,.04);
color:var(--tx);font-size:13px;cursor:pointer;transition:.18s;user-select:none}
.chip:hover{border-color:var(--a)}
.chip.on{background:linear-gradient(135deg,var(--a),#5b8cff);border-color:transparent;box-shadow:0 6px 18px rgba(124,92,255,.4)}
.btn{border:0;cursor:pointer;font:inherit;font-weight:600;font-size:15px;padding:13px 22px;border-radius:13px;color:#fff;
transition:.2s;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:8px}
.btn.main{background:linear-gradient(135deg,var(--a),var(--b));box-shadow:0 10px 28px rgba(0,160,255,.3);flex:1;min-width:180px}
.btn.main:hover{filter:brightness(1.1)}
.btn.main:disabled{opacity:.6;cursor:wait}
.btn.ghost{background:rgba(255,255,255,.07);border:1px solid var(--bd);font-size:14px;padding:12px 16px}
.btn.ghost:hover{background:rgba(255,255,255,.14)}
.count{margin-left:auto;font-size:12.5px;color:var(--mut)}
.vgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(118px,1fr));gap:8px;max-height:320px;overflow:auto;padding-right:4px}
.v{padding:10px;border-radius:12px;border:1px solid var(--bd);background:rgba(255,255,255,.035);cursor:pointer;transition:.18s}
.v b{display:block;font-size:14px}.v small{color:var(--mut);font-size:12px}
.v:hover{border-color:var(--b)}
.v.on{background:linear-gradient(135deg,rgba(124,92,255,.5),rgba(0,212,255,.3));border-color:var(--a)}
label{font-size:12.5px;color:var(--mut);display:block;margin:14px 0 8px}
label:first-of-type{margin-top:0}
audio{width:100%;margin-top:6px;filter:invert(.92) hue-rotate(180deg)}
.meta{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;font-size:12.5px;color:var(--mut)}
.meta span{padding:4px 10px;border-radius:99px;background:rgba(255,255,255,.06)}
.empty{color:var(--mut);font-size:14px;padding:8px 0}
.bars{display:none;gap:3px;align-items:flex-end;height:20px}
.busy .bars{display:flex}
.bars i{width:4px;background:#fff;border-radius:3px;animation:b .9s ease-in-out infinite}
.bars i:nth-child(2){animation-delay:.15s}.bars i:nth-child(3){animation-delay:.3s}
.bars i:nth-child(4){animation-delay:.45s}
@keyframes b{0%,100%{height:5px}50%{height:20px}}
.h{display:grid;grid-template-columns:1fr auto;gap:10px;padding:14px;border:1px solid var(--bd);border-radius:14px;
background:rgba(255,255,255,.03);margin-bottom:10px}
.h p{font-size:14.5px;line-height:1.5;margin-bottom:6px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.h small{color:var(--mut);font-size:12px}
.acts{display:flex;gap:6px;align-items:center}
.ib{width:38px;height:38px;border-radius:10px;border:1px solid var(--bd);background:rgba(255,255,255,.06);color:var(--tx);
cursor:pointer;font-size:15px;text-decoration:none;display:grid;place-items:center}
.ib:hover{background:rgba(255,255,255,.16)}
.ib.del:hover{background:rgba(255,92,122,.35)}
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(90px);padding:13px 20px;border-radius:13px;
background:#1b1f38;border:1px solid var(--bd);transition:.3s;z-index:9;max-width:90vw;font-size:14px}
#toast.show{transform:translateX(-50%) translateY(0)}
#toast.err{border-color:var(--err);color:#ffc0cc}#toast.ok{border-color:var(--ok)}
.welcome{position:fixed;inset:0;z-index:10;display:grid;place-items:center;overflow:auto;padding:12px;
padding:12px;padding:env(safe-area-inset-top,12px) env(safe-area-inset-right,12px) env(safe-area-inset-bottom,12px) env(safe-area-inset-left,12px);
background:radial-gradient(900px 500px at 10% -10%,rgba(124,92,255,.28),transparent),
radial-gradient(800px 500px at 100% 0%,rgba(0,212,255,.18),transparent),var(--bg)}
.welcome-card{width:min(760px,100%);max-height:calc(100vh - 24px);overflow:auto;padding:20px;background:rgba(15,18,38,.96);border:1px solid var(--bd);
border-radius:24px;box-shadow:0 24px 80px rgba(0,0,0,.4)}
.welcome-card h1{margin:18px 0 8px}
.welcome-card p{color:var(--mut);margin-bottom:18px}
.welcome-card .vgrid{max-height:30vh;margin:12px 0 18px}
.welcome-card .btn{width:100%}
.welcome-card .btn:disabled{opacity:.5;cursor:not-allowed}
.welcome-status{min-height:20px;color:var(--mut);font-size:13px;margin:8px 0}
.welcome-audio{width:100%;margin-bottom:14px}
button,.v,.chip{touch-action:manipulation}
@media(max-width:600px){
  body{min-width:0}
  .wrap{padding:16px 12px 32px}
  .card{padding:14px;border-radius:14px}
  .welcome-card{padding:16px;border-radius:18px}
  .welcome-card .vgrid{max-height:25vh}
  .welcome-card .btn,.btn{min-height:46px}
  .h{grid-template-columns:minmax(0,1fr);gap:10px}
  .acts{justify-content:flex-start}
  audio{min-height:42px}
}
</style>
</head>
<body>
<section class="welcome" id="welcome">
  <div class="welcome-card">
    <div class="logo">🎙️</div>
    <h1>Welcome to <span>VoiceForge Studio</span></h1>
    <p>Select a voice to hear it introduce itself. You can change it anytime.</p>
    <label for="apiKey">Google AI Studio API key</label>
    <input id="apiKey" type="password" maxlength="512" autocomplete="off" placeholder="Enter your Gemini API key">
    <small style="display:block;color:var(--mut);margin:8px 0">The key stays in this open page and is not saved.</small>
    <input id="welcomeSearch" placeholder="Search voices...">
    <div class="vgrid" id="welcomeVoices"></div>
    <div class="welcome-status" id="welcomeStatus">Click a voice to hear its welcome sample.</div>
    <audio class="welcome-audio" id="welcomeAudio" controls style="display:none"></audio>
    <button class="btn main" id="continue" disabled>Choose a voice to continue</button>
    <p style="margin:12px 0 0">Voice previews use Gemini API quota.</p>
  </div>
</section>

<div class="wrap" id="appContent" style="display:none">
  <header>
    <div class="logo">🎙️</div>
    <div><h1>Voice<span>Forge</span> Studio</h1><div class="sub" id="folder"></div></div>
  </header>

  <div class="grid">
    <div>
      <div class="card" id="mainCard">
        <h2>✍️ Text</h2>
        <textarea id="text" maxlength="10000" placeholder="Enter the text you want spoken... (Ctrl+Enter to generate)"></textarea>
        <div class="row" style="margin-top:16px">
          <button class="btn main" id="gen">🔊 Generate Voice <span class="bars"><i></i><i></i><i></i><i></i></span></button>
          <button class="btn ghost" id="clear">Clear</button>
          <div class="count" id="count">0 / 10,000 chars</div>
        </div>
      </div>

      <div class="card">
        <h2>▶️ Player</h2>
        <div id="playerEmpty" class="empty">No audio yet. Enter text and select Generate Voice.</div>
        <div id="playerBox" style="display:none">
          <audio id="audio" controls></audio>
          <div class="meta" id="meta"></div>
          <div class="row" style="margin-top:14px">
            <button class="btn ghost" id="dl">⬇️ Save to folder</button>
          </div>
        </div>
      </div>
    </div>

    <div>
      <div class="card">
        <h2>⚙️ Style</h2>
        <label>Language</label>
        <input id="languageSearch" list="languageOptions" placeholder="Search or type any language...">
        <datalist id="languageOptions"></datalist>
        <small style="display:block;color:var(--mut);margin-top:7px">Gemini language support and pronunciation quality may vary.</small>
        <label>Tone</label>
        <div class="chips" id="tones"></div>
        <label>Speed</label>
        <div class="chips" id="speeds"></div>
      </div>

      <div class="card">
        <h2>🎭 Voice</h2>
        <input id="vsearch" placeholder="Voice search..." style="margin-bottom:10px">
        <div class="vgrid" id="voices"></div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>🕘 History</h2>
    <div id="hist"></div>
  </div>
</div>
<div id="toast"></div>

<script>
const $ = s => document.querySelector(s);
const saved = JSON.parse(localStorage.getItem('voiceforge2') || '{}');
const st = Object.assign({voice:'Achernar', tone:'warm', speed:'normal', language:'auto'}, saved);
const apiKey = () => $('#apiKey').value.trim();
let cfg = null;
let current = null;
let voiceChosen = false;
let previewBusy = false;
let previewObjectUrl = null;
const persist = () => localStorage.setItem('voiceforge2', JSON.stringify(st));

function toast(msg, type){
  const t = $('#toast'); t.textContent = msg; t.className = 'show ' + (type||'');
  clearTimeout(toast.t); toast.t = setTimeout(() => t.className = '', 4500);
}
function chips(el, items, key){
  el.innerHTML = '';
  items.forEach(it => {
    const d = document.createElement('div');
    d.className = 'chip' + (it.id === st[key] ? ' on' : '');
    d.textContent = it.label;
    d.onclick = () => { st[key] = it.id; persist(); chips(el, items, key); };
    el.appendChild(d);
  });
}
function renderVoiceList(box, query, previewOnClick=false){
  box.innerHTML = '';
  cfg.voices.filter(v => (v.name + ' ' + v.desc).toLowerCase().includes(query)).forEach(v => {
    const card = document.createElement('div');
    card.className = 'v' + (v.name === st.voice ? ' on' : '');
    card.innerHTML = '<b></b><small></small>';
    card.querySelector('b').textContent = v.name;
    card.querySelector('small').textContent = v.desc;
    card.onclick = () => {
      if (previewBusy) return;
      st.voice = v.name;
      persist();
      renderVoices();
      if(previewOnClick){
        previewVoice(v.name);
      }
    };
    box.appendChild(card);
  });
}
function renderVoices(){
  renderVoiceList($('#voices'), $('#vsearch').value.toLowerCase());
  renderVoiceList($('#welcomeVoices'), $('#welcomeSearch').value.toLowerCase(), true);
}
async function previewVoice(name){
  if(!apiKey()){
    $('#welcomeStatus').textContent = 'Enter your Gemini API key to preview voices.';
    $('#apiKey').focus();
    return;
  }
  previewBusy = true;
  $('#continue').disabled = true;
  $('#continue').textContent = 'Preparing voice sample...';
  $('#welcomeStatus').textContent = 'Generating a short welcome sample for ' + name + '...';
  try{
    const r = await fetch('/api/preview', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({voice:name, apiKey:apiKey()})
    });
    if(!r.ok){
      const error = await r.json();
      throw new Error(error.error || 'Could not generate the voice sample.');
    }
    const blob = await r.blob();
    const player = $('#welcomeAudio');
    if(previewObjectUrl) URL.revokeObjectURL(previewObjectUrl);
    previewObjectUrl = URL.createObjectURL(blob);
    player.src = previewObjectUrl;
    player.style.display = 'block';
    voiceChosen = true;
    try{
      await player.play();
      $('#welcomeStatus').textContent = name + ' is speaking: “Welcome to VoiceForge Studio. I am ' + name + '.”';
    }catch(_error){
      $('#welcomeStatus').textContent = 'Sample ready. Press Play to hear ' + name + '.';
    }
  }catch(error){
    $('#welcomeStatus').textContent = error.message;
  }finally{
    previewBusy = false;
    $('#continue').disabled = !voiceChosen;
    $('#continue').textContent = 'Continue with ' + st.voice;
  }
}
function languageLabel(id){
  const option = cfg.languages.find(language => language.id === id);
  if(option) return option.label;
  if(id && id.startsWith('custom:')) return id.slice(7);
  return 'Auto-detect';
}
function setLanguageFromInput(){
  const value = $('#languageSearch').value.trim();
  const option = cfg.languages.find(language => language.label.toLowerCase() === value.toLowerCase());
  st.language = option ? option.id : (value ? 'custom:' + value : 'auto');
  persist();
}
function setBusy(b){
  $('#mainCard').classList.toggle('busy', b);
  $('#gen').disabled = b;
}
function showPlayer(e, autoplay){
  $('#playerEmpty').style.display = 'none';
  $('#playerBox').style.display = 'block';
  const a = $('#audio'); a.src = e.url;
  if (autoplay) a.play().catch(() => {});
  current = e.file;
  $('#meta').innerHTML = '';
  [languageLabel(e.language), e.voice, e.tone, e.speed, e.seconds + 's audio', e.took ? 'Generated in ' + e.took + 's' : ''].filter(Boolean).forEach(x => {
    const s = document.createElement('span'); s.textContent = x; $('#meta').appendChild(s);
  });
}
async function saveFile(file){
  if(!file) return toast('Generate a voice first.', 'err');
  try{
    const r = await fetch('/api/save/' + encodeURIComponent(file), {method:'POST'});
    const j = await r.json();
    if(!r.ok) throw new Error(j.error || 'Could not save audio.');
    toast('Saved: ' + j.path, 'ok');
  }catch(e){ toast(e.message, 'err'); }
}
async function generate(){
  const text = $('#text').value.trim();
  if(!text) return toast('Enter some text first.', 'err');
  if(!apiKey()){ toast('Enter your Gemini API key first.', 'err'); $('#apiKey').focus(); return; }
  setBusy(true);
  try{
    const r = await fetch('/api/tts', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({apiKey:apiKey(), text, voice:st.voice, tone:st.tone, speed:st.speed, language:st.language})});
    const j = await r.json();
    if(!r.ok) throw new Error(j.error || 'An error occurred.');
    showPlayer(j, true);
    loadHistory();
    toast('Voice ready! Select Save to folder to save the audio.', 'ok');
  }catch(e){ toast(e.message, 'err'); }
  finally{ setBusy(false); }
}
async function loadHistory(){
  const items = await (await fetch('/api/history')).json();
  const box = $('#hist'); box.innerHTML = '';
  if(!items.length){ box.innerHTML = '<div class="empty">No history yet.</div>'; return; }
  items.forEach(e => {
    const d = document.createElement('div'); d.className = 'h';
    const left = document.createElement('div');
    const p = document.createElement('p'); p.textContent = e.text;
    const sm = document.createElement('small');
    sm.textContent = [languageLabel(e.language), e.voice, e.tone, e.speed, e.seconds + 's', e.time].join(' • ');
    left.append(p, sm);
    const acts = document.createElement('div'); acts.className = 'acts';
    const play = document.createElement('button'); play.className = 'ib'; play.textContent = '▶'; play.title = 'Play';
    play.onclick = () => { showPlayer(e, true); window.scrollTo({top:0, behavior:'smooth'}); };
    const dl = document.createElement('button'); dl.className = 'ib'; dl.textContent = '⬇'; dl.title = 'Save to folder'; dl.onclick = () => saveFile(e.file);
    const cp = document.createElement('button'); cp.className = 'ib'; cp.textContent = '📋'; cp.title = 'Use this text again';
    cp.onclick = () => { $('#text').value = e.text; countUpdate(); toast('Text added to the editor.', 'ok'); window.scrollTo({top:0, behavior:'smooth'}); };
    const del = document.createElement('button'); del.className = 'ib del'; del.textContent = '🗑'; del.title = 'Delete';
    del.onclick = async () => { if(!confirm('Delete this audio?')) return; await fetch('/api/history/' + encodeURIComponent(e.file), {method:'DELETE'}); loadHistory(); };
    acts.append(play, dl, cp, del);
    d.append(left, acts); box.appendChild(d);
  });
}
function countUpdate(){ $('#count').textContent = $('#text').value.length.toLocaleString() + ' / 10,000 chars'; }

(async function init(){
  cfg = await (await fetch('/api/config')).json();
  $('#folder').textContent = 'Audio files are saved to: ' + cfg.folder;
  const languageOptions = $('#languageOptions');
  cfg.languages.forEach(language => {
    const option = document.createElement('option');
    option.value = language.label;
    languageOptions.appendChild(option);
  });
  if(!cfg.languages.some(language => language.id === st.language) && !st.language.startsWith('custom:')) st.language = 'auto';
  $('#languageSearch').value = languageLabel(st.language);
  $('#languageSearch').onchange = setLanguageFromInput;
  chips($('#tones'), cfg.tones, 'tone');
  chips($('#speeds'), cfg.speeds, 'speed');
  $('#vsearch').oninput = renderVoices;
  $('#welcomeSearch').oninput = () => renderVoiceList($('#welcomeVoices'), $('#welcomeSearch').value.toLowerCase());
  renderVoices(); loadHistory();
  $('#text').oninput = countUpdate;
  $('#text').addEventListener('keydown', e => { if(e.ctrlKey && e.key === 'Enter') generate(); });
  $('#gen').onclick = generate;
  $('#dl').onclick = () => saveFile(current);
  $('#clear').onclick = () => { $('#text').value = ''; countUpdate(); $('#text').focus(); };
  $('#continue').onclick = () => {
    if (!voiceChosen) return;
    $('#welcome').style.display = 'none';
    $('#appContent').style.display = 'block';
  };
})();
window.addEventListener('beforeunload', () => { $('#apiKey').value = ''; });
</script>
</body>
</html>'''


class VoiceForgeDesktop:
    def __init__(self, root):
        self.root = root
        self.root.title("VoiceForge Studio")
        self.root.geometry("1020x760")
        self.root.minsize(820, 600)
        self.results = queue.Queue()
        self.history_items = []
        self.current_file = None

        self.language_names = {label: key for key, (label, _) in LANGUAGES.items()}
        self.tone_names = {label: key for key, (label, _) in TONES.items()}
        self.speed_names = {label: key for key, (label, _) in SPEEDS.items()}
        self.voice_labels = [f"{name} — {desc}" for name, desc in VOICES]
        self.voice_by_label = {label: VOICES[i][0] for i, label in enumerate(self.voice_labels)}

        self._build_ui()
        self.refresh_history()
        self.root.after(100, self._check_results)

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=3)
        outer.columnconfigure(1, weight=2)
        outer.rowconfigure(1, weight=1)

        ttk.Label(outer, text="VoiceForge Studio", font=("Segoe UI", 20, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 14)
        )

        left = ttk.Frame(outer)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)

        settings = ttk.LabelFrame(left, text="Voice settings", padding=12)
        settings.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        for column in (1, 3):
            settings.columnconfigure(column, weight=1)

        self.language_var = tk.StringVar(value="Original / Auto")
        self.tone_var = tk.StringVar(value=TONES["warm"][0])
        self.speed_var = tk.StringVar(value=SPEEDS["normal"][0])
        self.voice_var = tk.StringVar(value=self.voice_labels[19])

        self._add_dropdown(settings, "Language", self.language_var, list(self.language_names), 0, 0)
        self._add_dropdown(settings, "Tone", self.tone_var, list(self.tone_names), 0, 2)
        self._add_dropdown(settings, "Speed", self.speed_var, list(self.speed_names), 1, 0)
        self._add_dropdown(settings, "Voice", self.voice_var, self.voice_labels, 1, 2)

        text_frame = ttk.LabelFrame(left, text="Text (maximum 10,000 characters)", padding=10)
        text_frame.grid(row=1, column=0, sticky="nsew")
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)

        self.text_box = tk.Text(text_frame, wrap="word", undo=True, height=14, font=("Segoe UI", 11))
        scrollbar = ttk.Scrollbar(text_frame, orient="vertical", command=self.text_box.yview)
        self.text_box.configure(yscrollcommand=scrollbar.set)
        self.text_box.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.text_box.bind("<<Modified>>", self._on_text_changed)
        self.text_box.bind("<Control-Return>", self._generate_event)

        text_controls = ttk.Frame(left)
        text_controls.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.count_var = tk.StringVar(value=f"0 / {MAX_TEXT_LENGTH:,} chars")
        self.status_var = tk.StringVar(value="Ready")
        self.generate_button = ttk.Button(text_controls, text="Generate voice", command=self.generate)
        self.generate_button.pack(side="left")
        ttk.Button(text_controls, text="Clear", command=self.clear_text).pack(side="left", padx=8)
        ttk.Label(text_controls, textvariable=self.status_var).pack(side="left", padx=8)
        ttk.Label(text_controls, textvariable=self.count_var).pack(side="right")

        right = ttk.LabelFrame(outer, text="History", padding=10)
        right.grid(row=1, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self.history_list = tk.Listbox(right, activestyle="dotbox", exportselection=False)
        history_scrollbar = ttk.Scrollbar(right, orient="vertical", command=self.history_list.yview)
        self.history_list.configure(yscrollcommand=history_scrollbar.set)
        self.history_list.grid(row=0, column=0, columnspan=2, sticky="nsew")
        history_scrollbar.grid(row=0, column=2, sticky="ns")
        self.history_list.bind("<<ListboxSelect>>", self._show_history_text)

        actions = ttk.Frame(right)
        actions.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        for label, command in (
            ("Play", self.play_selected),
            ("Save to folder", self.save_selected),
            ("Use text", self.use_selected_text),
            ("Delete", self.delete_selected),
        ):
            ttk.Button(actions, text=label, command=command).pack(fill="x", pady=2)

        self.history_text = tk.StringVar(value="Select an item to see its text.")
        ttk.Label(right, textvariable=self.history_text, wraplength=310, justify="left").grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0)
        )
        ttk.Label(
            outer,
            text=f"Generated files are cached locally. Save to: {OUTPUT_FOLDER}",
            wraplength=960,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(12, 0))

    @staticmethod
    def _add_dropdown(parent, label, variable, values, row, column):
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", padx=(0, 8), pady=5)
        ttk.Combobox(
            parent,
            textvariable=variable,
            values=values,
            state="readonly",
            width=24,
        ).grid(row=row, column=column + 1, sticky="ew", pady=5)

    def _on_text_changed(self, _event=None):
        text = self.text_box.get("1.0", "end-1c")
        if len(text) > MAX_TEXT_LENGTH:
            self.text_box.delete(f"1.0+{MAX_TEXT_LENGTH}c", "end-1c")
            text = self.text_box.get("1.0", "end-1c")
        self.count_var.set(f"{len(text):,} / {MAX_TEXT_LENGTH:,} chars")
        self.text_box.edit_modified(False)

    def _generate_event(self, _event=None):
        self.generate()
        return "break"

    def clear_text(self):
        self.text_box.delete("1.0", "end")
        self.text_box.focus_set()

    def generate(self):
        text = self.text_box.get("1.0", "end-1c").strip()
        if not text:
            messagebox.showwarning("Text required", "Enter some text first.")
            return
        if len(text) > MAX_TEXT_LENGTH:
            messagebox.showerror("Text too long", f"Text cannot exceed {MAX_TEXT_LENGTH:,} characters.")
            return

        settings = {
            "text": text,
            "voice": self.voice_by_label.get(self.voice_var.get(), "Achernar"),
            "tone": self.tone_names[self.tone_var.get()],
            "speed": self.speed_names[self.speed_var.get()],
            "language": self.language_names[self.language_var.get()],
        }
        self.generate_button.configure(state="disabled")
        self.status_var.set("Generating...")
        threading.Thread(target=self._generate_worker, args=(settings,), daemon=True).start()

    def _generate_worker(self, settings):
        started = time.time()
        try:
            pcm, rate, model = synth(
                settings["text"], settings["voice"], settings["tone"],
                settings["speed"], settings["language"],
            )
            audio, ext = encode_audio(pcm, rate)
            name = datetime.now().strftime("voice_%Y%m%d_%H%M%S_%f")[:-3] + "." + ext
            with open(os.path.join(CACHE_DIR, name), "wb") as audio_file:
                audio_file.write(audio)
            entry = {
                **settings,
                "file": name,
                "model": model,
                "seconds": round(len(pcm) / 2 / rate, 1),
                "took": round(time.time() - started, 1),
                "time": datetime.now().strftime("%d %b %Y, %I:%M %p"),
            }
            with lock:
                items = load_history()
                items.insert(0, entry)
                save_history(items)
            self.results.put(("success", entry))
        except requests.exceptions.RequestException as error:
            self.results.put(("error", f"Network error: {error}"))
        except Exception as error:
            self.results.put(("error", str(error)))

    def _check_results(self):
        try:
            while True:
                kind, value = self.results.get_nowait()
                self.generate_button.configure(state="normal")
                if kind == "success":
                    self.current_file = value["file"]
                    self.status_var.set(f"Voice ready ({value['seconds']}s)")
                    self.refresh_history()
                    messagebox.showinfo("Voice ready", "Your voice is ready. Select Play or Save to folder from history.")
                else:
                    self.status_var.set("Generation failed")
                    messagebox.showerror("Generation failed", value)
        except queue.Empty:
            pass
        self.root.after(100, self._check_results)

    def refresh_history(self):
        self.history_items = [
            item for item in load_history()
            if os.path.isfile(os.path.join(CACHE_DIR, item.get("file", "")))
        ]
        self.history_list.delete(0, "end")
        for item in self.history_items:
            language = LANGUAGES.get(item.get("language"), LANGUAGES["auto"])[0]
            summary = item.get("text", "").replace("\n", " ")[:48]
            self.history_list.insert(
                "end",
                f"{item.get('time', '')} | {language} | {summary}",
            )

    def _selected_entry(self):
        selection = self.history_list.curselection()
        return self.history_items[selection[0]] if selection else None

    def _show_history_text(self, _event=None):
        entry = self._selected_entry()
        self.history_text.set(entry["text"] if entry else "Select an item to see its text.")

    def use_selected_text(self):
        entry = self._selected_entry()
        if not entry:
            return
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", entry["text"][:MAX_TEXT_LENGTH])
        self._on_text_changed()

    def play_selected(self):
        entry = self._selected_entry()
        name = entry["file"] if entry else self.current_file
        if not name:
            messagebox.showwarning("No audio", "Generate a voice or select one from history first.")
            return
        path = os.path.join(CACHE_DIR, os.path.basename(name))
        try:
            if not os.path.isfile(path):
                raise FileNotFoundError("Audio file was not found.")
            os.startfile(path)
        except Exception as error:
            messagebox.showerror("Playback failed", str(error))

    def save_selected(self):
        entry = self._selected_entry()
        name = entry["file"] if entry else self.current_file
        if not name:
            messagebox.showwarning("No audio", "Generate a voice or select one from history first.")
            return
        src = os.path.join(CACHE_DIR, os.path.basename(name))
        try:
            if not os.path.isfile(src):
                raise FileNotFoundError("Audio file was not found.")
            os.makedirs(OUTPUT_FOLDER, exist_ok=True)
            destination = os.path.join(OUTPUT_FOLDER, os.path.basename(name))
            shutil.copy2(src, destination)
            self.status_var.set(f"Saved: {destination}")
        except Exception as error:
            messagebox.showerror("Save failed", str(error))

    def delete_selected(self):
        entry = self._selected_entry()
        if not entry:
            messagebox.showwarning("Select history", "Select a history item to delete.")
            return
        if not messagebox.askyesno("Delete voice", "Delete the selected audio and history entry?"):
            return
        name = os.path.basename(entry["file"])
        with lock:
            items = [item for item in load_history() if item.get("file") != name]
            save_history(items)
        try:
            os.remove(os.path.join(CACHE_DIR, name))
        except FileNotFoundError:
            pass
        except OSError as error:
            messagebox.showerror("Delete failed", str(error))
        self.refresh_history()
        self.history_text.set("Select an item to see its text.")


def run_desktop_app():
    url = f"http://127.0.0.1:{PORT}/"
    threading.Timer(1, webbrowser.open, args=(url,)).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True, debug=False, use_reloader=False)


def run_android_app():
    app.run(host="127.0.0.1", port=PORT, threaded=True, debug=False, use_reloader=False)


def main():
    if "ANDROID_ARGUMENT" in os.environ:
        run_android_app()
    elif GUI_ENABLED:
        run_desktop_app()
    else:
        app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()