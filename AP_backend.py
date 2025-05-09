from fastapi import FastAPI, UploadFile, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
import glob
from pydantic import BaseModel
from typing import Optional
import uvicorn
import os
import uuid
import subprocess
import shutil
from supabase import create_client, Client

app = FastAPI()

TMP_DIR = "tmp_audio"
os.makedirs(TMP_DIR, exist_ok=True)

# Supabase config
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_BUCKET = "your-bucket-name"  # Replace with your actual bucket name
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

class ProcessRequest(BaseModel):
    yt_url: str
    speed: Optional[float] = 1.0
    reverb: Optional[float] = 0.0  # Reverb as a percent (0 to 100)
    bass_boost: Optional[bool] = False

def download_youtube_audio(yt_url: str, output_path: str):
    try:
        subprocess.run([
            "yt-dlp",
            "-x", "--audio-format", "mp3",
            "-o", output_path,
            yt_url
        ], check=True)
    except subprocess.CalledProcessError:
        raise HTTPException(status_code=400, detail="Failed to download YouTube audio")

def apply_audio_effects(input_file: str, output_file: str, speed: float, reverb: float, bass_boost: bool):
    filters = []
    if speed and speed != 1.0:
        filters.append(f"atempo={speed}")
    if reverb and reverb > 0:
        # Map 0-100% to reasonable aecho delay and decay
        delay = 50 + (reverb * 0.5)  # 50-100 ms
        decay = 0.2 + (reverb / 200)  # 0.2-0.7
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
            "content-type": "audio/mpeg",
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

@app.post("/process")
def process_audio(request: ProcessRequest, background_tasks: BackgroundTasks):
    audio_id = str(uuid.uuid4())
    raw_audio_path = os.path.join(TMP_DIR, f"{audio_id}_raw.%(ext)s")
    final_audio_path = os.path.join(TMP_DIR, f"{audio_id}_processed.mp3")

    # Download YouTube audio
    download_youtube_audio(request.yt_url, raw_audio_path)

    # Replace placeholder from yt-dlp
    matching_files = glob.glob(os.path.join(TMP_DIR, f"{audio_id}_raw.*"))
    if not matching_files:
        raise HTTPException(status_code=500, detail="Downloaded file not found")
    downloaded_file = matching_files[0]


    # Apply audio effects
    apply_audio_effects(
        downloaded_file,
        final_audio_path,
        request.speed,
        request.reverb,
        request.bass_boost
    )

    # Upload to Supabase
    destination_path = f"processed/{audio_id}.mp3"
    public_url = upload_to_supabase(final_audio_path, destination_path)

    # Cleanup files
    background_tasks.add_task(cleanup_file, downloaded_file)
    background_tasks.add_task(cleanup_file, final_audio_path)

    return {"audio_id": audio_id, "public_url": public_url}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
