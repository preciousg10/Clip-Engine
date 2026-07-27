"""INDEX stage — build a moment index of the ENTIRE source.

Two moment sources, per instructions.md:
  - transcript (faster-whisper 'base', CPU, word timestamps)
  - audio-spike detection (RMS over 1s windows; windows >2 std above the mean are
    screams/laughs/chaos = candidate moments)

Long-VOD handling: audio is processed in time CHUNKS. Each chunk is transcribed +
RMS-scanned, then checkpointed to disk and to state.json, so a crash mid-VOD resumes
with zero redone work. Progress prints as "transcribed 47/180 min".

Offline/degraded mode (CLIPPER_OFFLINE=1) skips whisper (spikes only) so the pipeline
is testable without downloading a model.
"""
import os
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

CHUNK_SEC = 300          # 5-minute chunks (checkpoint granularity)
SR = 16000               # analysis sample rate
WIN_SEC = 1.0            # RMS window
SPIKE_STD = 2.0          # flag windows > mean + SPIKE_STD*std
CLEANUP_TMP = True


# --- footage selection (prefer safe-margin) ------------------------------------
def choose_footage(manifest):
    footage = [d for d in manifest.get("downloads", []) if d.get("kind") == "footage"]
    safe = [d for d in footage if d.get("safe_margin")]
    if safe:
        C.log(f"safe-margin footage present — indexing {len(safe)} safe version(s), "
              f"skipping {len(footage) - len(safe)} non-safe.")
        return safe
    return footage


# --- transcription -------------------------------------------------------------
_model = None


def _get_model():
    global _model
    if _model is None:
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            C.fail("faster-whisper not installed. pip install -r requirements.txt "
                   "(or run offline with CLIPPER_OFFLINE=1 to skip transcription).")
        C.log("loading faster-whisper 'base' (CPU, int8) — first load downloads the model…")
        _model = WhisperModel("base", device="cpu", compute_type="int8")
    return _model


def _extract_chunk_wav(source, start, dur, wav_path):
    C.run_cmd(["ffmpeg", "-v", "error", "-y", "-ss", f"{start}", "-t", f"{dur}",
               "-i", str(source), "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le",
               str(wav_path)])


def _read_wav_int16(wav_path):
    with wave.open(str(wav_path), "rb") as w:
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32)


def _rms_per_second(audio):
    win = int(SR * WIN_SEC)
    n = len(audio) // win
    if n == 0:
        return np.array([])
    a = audio[:n * win].reshape(n, win)
    return np.sqrt((a ** 2).mean(axis=1) + 1e-9)


def _transcribe_wav(wav_path, offset):
    model = _get_model()
    segments, _info = model.transcribe(str(wav_path), word_timestamps=True)
    out = []
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        out.append({"start": round(offset + seg.start, 2),
                    "end": round(offset + seg.end, 2),
                    "text": text})
    return out


# --- moment construction -------------------------------------------------------
def _spike_moments(source, rms):
    if rms.size == 0:
        return []
    mean, std = float(rms.mean()), float(rms.std())
    if std <= 0:
        return []
    thr = mean + SPIKE_STD * std
    hot = rms > thr
    moments, i, n = [], 0, len(hot)
    while i < n:
        if hot[i]:
            j = i
            while j < n and hot[j]:
                j += 1
            seg = rms[i:j]
            z = float((seg.max() - mean) / std)
            # peak = absolute second of the loudest window in the spike (the punchline
            # / crash / scream). Cold-open restructuring opens the clip here.
            peak = float(i + int(seg.argmax()))
            moments.append({"source": source, "start": float(i), "end": float(j),
                            "type": "audio_spike", "intensity": round(z, 2),
                            "peak": peak, "text": ""})
            i = j
        else:
            i += 1
    return moments


def _speech_moments(source, transcript):
    out = []
    for seg in transcript:
        # no audio-spike anchor for pure speech, so peak is None (cold-open only fires
        # on separable audio peaks; speech clips play chronologically).
        out.append({"source": source, "start": seg["start"], "end": seg["end"],
                    "type": "speech", "intensity": round(len(seg["text"]) / 10.0, 2),
                    "peak": None, "text": seg["text"]})
    return out


# --- per-source indexing (chunked + resumable) ---------------------------------
def _partial_paths(name):
    return (C.TRANSCRIPTS / f"{name}.transcript.json",
            C.TRANSCRIPTS / f"{name}.rms.json")


def index_source(entry, state, do_transcribe):
    source = C.ROOT / entry["path"]
    name = os.path.splitext(os.path.basename(entry["path"]))[0]
    duration = entry.get("duration_sec") or C.ffprobe_duration(source)
    chunks_total = max(1, int(np.ceil(duration / CHUNK_SEC)))

    idx_state = state["stages"].setdefault("index", {"done": False, "sources": {}})
    ss = idx_state["sources"].setdefault(name, {"chunks_total": chunks_total, "chunks_done": 0})
    ss["chunks_total"] = chunks_total

    tr_path, rms_path = _partial_paths(name)
    transcript = C.load_json(tr_path, default=[]) or []
    rms_vals = C.load_json(rms_path, default=[]) or []

    tmp_wav = C.TRANSCRIPTS / f"{name}.chunk.wav"
    for ci in range(ss["chunks_done"], chunks_total):
        start = ci * CHUNK_SEC
        dur = min(CHUNK_SEC, duration - start)
        _extract_chunk_wav(source, start, dur, tmp_wav)
        audio = _read_wav_int16(tmp_wav)
        rms_vals.extend([round(float(v), 2) for v in _rms_per_second(audio)])
        if do_transcribe:
            transcript.extend(_transcribe_wav(tmp_wav, start))
        # checkpoint after each chunk
        C.save_json(tr_path, transcript)
        C.save_json(rms_path, rms_vals)
        ss["chunks_done"] = ci + 1
        C.save_state(state)
        done_min = int((ci + 1) * CHUNK_SEC / 60)
        total_min = int(np.ceil(duration / 60))
        C.log(f"  {name}: {'transcribed' if do_transcribe else 'scanned'} "
              f"{min(done_min, total_min)}/{total_min} min")
    if CLEANUP_TMP and tmp_wav.exists():
        tmp_wav.unlink()

    rms = np.array(rms_vals, dtype=np.float32)
    moments = _spike_moments(entry["path"], rms) + _speech_moments(entry["path"], transcript)
    return {"source": entry["path"], "duration_sec": round(duration, 2),
            "safe_margin": entry.get("safe_margin", False),
            "transcript": transcript, "moments": moments}


def run(state):
    C.ensure_dirs()
    manifest = C.load_json(C.CAMPAIGN_MANIFEST)
    if not manifest:
        C.fail("campaign/manifest.json missing — run intake.py first.")
    footage = choose_footage(manifest)
    if not footage:
        C.fail("no footage to index — run intake.py first.")

    do_transcribe = not C.offline_mode()
    if not do_transcribe:
        C.warn("offline mode — skipping whisper transcription (audio spikes only).")

    sources, all_moments = [], []
    for entry in footage:
        C.log(f"indexing {entry['path']} …")
        src = index_source(entry, state, do_transcribe)
        sources.append(src)
        all_moments.extend(src["moments"])

    # assign global ids, newest-intensity first is handled later by select
    for i, m in enumerate(all_moments):
        m["id"] = f"m{i:04d}"

    C.save_json(C.MOMENTS_JSON, {"created_at": C.now_iso(),
                                 "sources": sources, "moments": all_moments})
    C.mark_stage(state, "index", moments=len(all_moments), sources=len(sources))
    C.log(f"index done: {len(all_moments)} moments across {len(sources)} source(s).")


if __name__ == "__main__":
    run(C.load_state())
