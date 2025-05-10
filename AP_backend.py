from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import os
import uuid
import subprocess
import glob
import mimetypes
from supabase import create_client, Client
from pysndfx import AudioEffectsChain

app = FastAPI()
TMP_DIR = "tmp_audio"
os.makedirs(TMP_DIR, exist_ok=True)

# Supabase config
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET")
if not all([SUPABASE_URL, SUPABASE_KEY, SUPABASE_BUCKET]):
    raise Exception("Missing Supabase environment variables.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

class UploadRequest(BaseModel):
    yt_url: str

class EffectsRequest(BaseModel):
    audio_id: str
    speed: Optional[float] = 1.0
    reverb: Optional[float] = 0.0
    bass_boost: Optional[bool] = False

def short_id():
    return uuid.uuid4().hex[:6]

def convert_to_mp3(input_path: str, output_path: str):
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", input_path, "-codec:a", "libmp3lame", output_path
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"MP3 conversion error: {e.stderr.decode()}")

def download_audio(url: str, audio_id: str) -> str:
    track_dir = os.path.join(TMP_DIR, audio_id)
    os.makedirs(track_dir, exist_ok=True)

    if "youtube.com" in url or "youtu.be" in url:
        output_path = os.path.join(track_dir, "%(title)s.%(ext)s")
        try:
            subprocess.run([
                "yt-dlp",
                "-x", "--audio-format", "wav",
                "-o", output_path,
                url
            ], check=True)
        except subprocess.CalledProcessError as e:
            raise HTTPException(status_code=400, detail=f"Failed to download YouTube audio: {e}")
    elif "spotify.com" in url:
        output_path = os.path.join(track_dir, "%(title)s.%(ext)s")
        try:
            subprocess.run([
                "spotdl",
                url,
                "--output", output_path,
                "--audio", "wav"
            ], check=True)
        except subprocess.CalledProcessError as e:
            raise HTTPException(status_code=400, detail=f"Failed to download Spotify audio: {e}")
    else:
        raise HTTPException(status_code=400, detail="Unsupported URL. Only YouTube and Spotify are supported.")

    # Find the downloaded file
    downloaded_files = glob.glob(os.path.join(track_dir, "*.wav"))
    if not downloaded_files:
        raise HTTPException(status_code=500, detail="Audio download completed, but file not found.")

    return downloaded_files[0]


def apply_audio_effects(input_file: str, output_file: str, speed: float, reverb: float, bass_boost: bool):
    fx = AudioEffectsChain()

    if speed != 1.0:
        fx = fx.tempo(speed,'s')
    if reverb > 0:
        fx = fx.reverb(reverberance=reverb)
    if bass_boost:
        fx = fx.bass(gain=10)

    # Apply a smoothing filter
    fx = fx.lowpass(3000)  # cutoff frequency around 3kHz — adjust as needed

    try:
        fx(input_file, output_file)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Audio processing error: {e}")


def upload_to_supabase(file_path: str, destination_name: str) -> str:
    try:
        with open(file_path, "rb") as f:
            res = supabase.storage.from_(SUPABASE_BUCKET).upload(destination_name, f, {
                "content-type": mimetypes.guess_type(file_path)[0] or "audio/mpeg",
                "x-upsert": "true"
            })
        if hasattr(res, "error") and res.error is not None:
            raise Exception(res.error.message)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase upload failed: {e}")
    try:
        public_url_res = supabase.storage.from_(SUPABASE_BUCKET).get_public_url(destination_name)
        return public_url_res
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get public URL: {e}")

def cleanup_file(file_path: str):
    try:
        os.remove(file_path)
    except Exception:
        pass

@app.post("/upload")
def upload_audio(req: UploadRequest):
    audio_id = short_id()
    downloaded_path = download_audio(req.yt_url, audio_id)
    final_path = os.path.join(TMP_DIR, f"{audio_id}.wav")
    os.rename(downloaded_path, final_path)
    return {"audio_id": audio_id}

@app.post("/effects")
def apply_effects(req: EffectsRequest, background_tasks: BackgroundTasks):
    raw_audio_path = os.path.join(TMP_DIR, f"{req.audio_id}.wav")
    if not os.path.exists(raw_audio_path):
        raise HTTPException(status_code=404, detail="Original audio not found.")

    suffix = "rawcopy" if (req.speed == 1.0 and req.reverb == 0.0 and not req.bass_boost) else f"{req.speed}_{req.reverb}_{req.bass_boost}"
    effects_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"{req.audio_id}_{suffix}").hex[:8]

    effected_wav_path = os.path.join(TMP_DIR, f"{effects_id}.wav")
    mp3_path = os.path.join(TMP_DIR, f"{effects_id}.mp3")

    apply_audio_effects(raw_audio_path, effected_wav_path, req.speed, req.reverb, req.bass_boost)
    convert_to_mp3(effected_wav_path, mp3_path)

    public_url = upload_to_supabase(mp3_path, f"processed/{effects_id}.mp3")
    background_tasks.add_task(cleanup_file, mp3_path)
    background_tasks.add_task(cleanup_file, effected_wav_path)

    return {"effects_id": effects_id, "public_url": public_url}

@app.get("/stream/{effects_id}")
def stream_effects(effects_id: str):
    file_path = os.path.join(TMP_DIR, f"{effects_id}.mp3")
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="audio/mpeg")
    supabase_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/processed/{effects_id}.mp3"
    return {"url": supabase_url}

@app.get("/download/{effects_id}")
def download_effects(effects_id: str):
    wav_path = os.path.join(TMP_DIR, f"{effects_id}.wav")
    mp3_path = os.path.join(TMP_DIR, f"{effects_id}.mp3")
    if not os.path.exists(wav_path):
        raise HTTPException(status_code=404, detail="WAV file not found.")

    convert_to_mp3(wav_path, mp3_path)
    return FileResponse(mp3_path, filename=f"{effects_id}.mp3", media_type="audio/mpeg")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
