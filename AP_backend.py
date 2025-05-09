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

app = FastAPI()

TMP_DIR = "tmp_audio"
os.makedirs(TMP_DIR, exist_ok=True)

# Supabase config
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ========== Models ==========
class UploadRequest(BaseModel):
    yt_url: str

class EffectsRequest(BaseModel):
    audio_id: str
    speed: Optional[float] = 1.0
    reverb: Optional[float] = 0.0
    bass_boost: Optional[bool] = False

# ========== Utility Functions ==========
def short_id():
    return uuid.uuid4().hex[:6]

def download_youtube_audio(yt_url: str, audio_id: str) -> str:
    output_path = os.path.join(TMP_DIR, f"{audio_id}_raw.%(ext)s")
    try:
        subprocess.run([
            "yt-dlp",
            "-x", "--audio-format", "mp3",
            "-o", output_path,
            yt_url
        ], check=True)
    except subprocess.CalledProcessError:
        raise HTTPException(status_code=400, detail="Failed to download YouTube audio")

    matching_files = glob.glob(os.path.join(TMP_DIR, f"{audio_id}_raw.*"))
    if not matching_files:
        raise HTTPException(status_code=500, detail="Downloaded file not found")
    return matching_files[0]

def apply_audio_effects(input_file: str, output_file: str, speed: float, reverb: float, bass_boost: bool):
    filters = []
    if speed and speed != 1.0:
        filters.append(f"atempo={speed}")
    if reverb and reverb > 0:
        delay = 50 + (reverb * 0.5)
        decay = 0.2 + (reverb / 200)
        filters.append(f"aecho=0.8:0.88:{int(delay)}:{decay:.2f}")
    if bass_boost:
        filters.append("bass=g=10")

    filter_str = ",".join(filters) if filters else None
    command = ["ffmpeg", "-y", "-i", input_file]
    if filter_str:
        command += ["-af", filter_str]
    command.append(output_file)

    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError:
        raise HTTPException(status_code=500, detail="Failed to apply audio effects")

def upload_to_supabase(file_path: str, destination_name: str) -> str:
    with open(file_path, "rb") as f:
        res = supabase.storage.from_(SUPABASE_BUCKET).upload(destination_name, f, {
            "content-type": mimetypes.guess_type(file_path)[0] or "audio/mpeg",
            "x-upsert": "true"
        })
        if not res:
            raise HTTPException(status_code=500, detail="Upload to Supabase failed")
    return supabase.storage().from_(SUPABASE_BUCKET).get_public_url(destination_name)

def cleanup_file(file_path: str):
    try:
        os.remove(file_path)
    except Exception:
        pass

# Optional: add periodic cleanup of old TMP_DIR files here

# ========== Endpoints ==========
@app.post("/upload")
def upload_audio(req: UploadRequest):
    audio_id = short_id()
    downloaded_path = download_youtube_audio(req.yt_url, audio_id)
    destination = os.path.join(TMP_DIR, f"{audio_id}.mp3")
    os.rename(downloaded_path, destination)
    return {"audio_id": audio_id}

@app.post("/effects")
def apply_effects(req: EffectsRequest, background_tasks: BackgroundTasks):
    raw_audio_path = os.path.join(TMP_DIR, f"{req.audio_id}.mp3")
    if not os.path.exists(raw_audio_path):
        raise HTTPException(status_code=404, detail="Original audio not found")

    effects_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"{req.audio_id}_{req.speed}_{req.reverb}_{req.bass_boost}").hex[:8]
    final_audio_path = os.path.join(TMP_DIR, f"{effects_id}.mp3")

    apply_audio_effects(
        raw_audio_path,
        final_audio_path,
        req.speed,
        req.reverb,
        req.bass_boost
    )

    destination_path = f"processed/{effects_id}.mp3"
    public_url = upload_to_supabase(final_audio_path, destination_path)

    background_tasks.add_task(cleanup_file, final_audio_path)

    return {"effects_id": effects_id, "public_url": public_url}

@app.get("/stream/{effects_id}")
def stream_effects(effects_id: str):
    file_path = os.path.join(TMP_DIR, f"{effects_id}.mp3")
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="audio/mpeg")

    # Optional fallback: stream directly from Supabase
    supabase_url = f"https://{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/processed/{effects_id}.mp3"
    return {"url": supabase_url}

@app.get("/download/{effects_id}")
def download_effects(effects_id: str):
    file_path = os.path.join(TMP_DIR, f"{effects_id}.mp3")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found or expired")
    return FileResponse(file_path, filename=f"{effects_id}.mp3", media_type="audio/mpeg")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
