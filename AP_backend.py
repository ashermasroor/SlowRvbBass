from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import base64
import os
import uuid
import subprocess
import glob
import mimetypes
from supabase import create_client, Client
from pysndfx import AudioEffectsChain
import re

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

YT_COOKIES_BASE64 = os.getenv("YT_COOKIES_BASE64")

if YT_COOKIES_BASE64:
    try:
        with open("cookies.txt","wb") as f:
            f.write(base64.b64decode(YT_COOKIES_BASE64))
    except Exception as e:
        raise Exception(f"Failed to decode YouTube cookies:" + str(e))
else:
    raise Exception("Missing YT_COOKIES_BASE64 environment variable.")

class UploadRequest(BaseModel):
    url: str  # Only one URL field

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

def download_youtube_audio(yt_url: str, audio_id: str) -> str:
    output_path = os.path.join(TMP_DIR, f"{audio_id}_raw.wav")
    try:
        subprocess.run([
            "yt-dlp",
            "-x", "--audio-format", "wav",
            "--cookies", "cookies.txt",
            "-o", os.path.join(TMP_DIR, f"{audio_id}_raw.%(ext)s"),
            yt_url
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        error_output = e.stderr.decode()
        if "Sign in to confirm you’re not a bot" in error_output or "HTTP Error 403" in error_output:
            raise HTTPException(
                status_code=403,
                detail="Download failed: YouTube requires login or cookies may have expired. Please refresh the cookies."
            )
        raise HTTPException(status_code=400, detail=f"Failed to download YouTube audio: {error_output}")

    return output_path

def download_spotify_audio(spotify_url: str, audio_id: str) -> str:
    output_path = os.path.join(TMP_DIR, f"{audio_id}_raw.mp3")
    try:
        subprocess.run([
            "spotdl", spotify_url,
            "--output", os.path.join(TMP_DIR, f"{audio_id}_raw.%(ext)s"),
            "--format", "mp3",
            "--default-search", "ytsearch"
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=400, detail=f"Failed to download Spotify audio: {e.stderr.decode()}")

    # Resolve actual downloaded file (in case spotdl renames it)
    downloaded_files = glob.glob(os.path.join(TMP_DIR, f"{audio_id}_raw.*"))
    if not downloaded_files:
        raise HTTPException(status_code=404, detail="Spotify download failed or returned no file.")
    return downloaded_files[0]

def apply_audio_effects(input_file: str, output_file: str, speed: float, reverb: float, bass_boost: bool):
    fx = AudioEffectsChain()

    if speed != 1.0:
        fx = fx.tempo(speed, 's')
    if reverb > 0:
        fx = fx.reverb(reverberance=reverb)
    if bass_boost:
        fx = fx.bass(gain=10)

    fx = fx.lowpass(3000)

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
    url = req.url

    if "youtube.com" in url or "youtu.be" in url:
        downloaded_path = download_youtube_audio(url, audio_id)
    elif "spotify.com" in url:
        downloaded_path = download_spotify_audio(url, audio_id)
    else:
        raise HTTPException(status_code=400, detail="Invalid URL: Must be YouTube or Spotify URL.")

    final_path = os.path.join(TMP_DIR, f"{audio_id}.wav")
    convert_to_mp3(downloaded_path, final_path)
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
