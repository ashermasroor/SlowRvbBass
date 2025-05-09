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
if not all([SUPABASE_URL, SUPABASE_KEY, SUPABASE_BUCKET]):
    raise Exception("Missing Supabase environment variables.")

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
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=400, detail=f"Failed to download YouTube audio: {e}")

    matching_files = glob.glob(os.path.join(TMP_DIR, f"{audio_id}_raw.*"))
    if not matching_files:
        raise HTTPException(status_code=500, detail="Downloaded file not found.")
    return matching_files[0]

def find_freeverb_plugin() -> str:
    for root, dirs, files in os.walk("/nix/store"):
        for f in files:
            if f == "freeverb_1433.so":
                return os.path.join(root, f)
    raise FileNotFoundError("freeverb plugin not found.")

freeverb = find_freeverb_plugin()
def apply_audio_effects(input_file: str, output_file: str, speed: float, reverb: float, bass_boost: bool):
    filters = []

    # Pitch + tempo scaling (like tape player) using atempo
    if speed != 1.0:
        # FFmpeg atempo only accepts values between 0.5 and 2.0, so chain filters if needed
        atempo_filters = []
        remaining_speed = speed
        while remaining_speed < 0.5 or remaining_speed > 2.0:
            step = 2.0 if remaining_speed > 2.0 else 0.5
            atempo_filters.append(f"atempo={step}")
            remaining_speed /= step
        atempo_filters.append(f"atempo={remaining_speed:.3f}")
        filters.extend(atempo_filters)

    # Smooth reverb using freeverb (natural, stereo reverb)
    if reverb > 0:
        # reverb level: 0.0 to 1.0 (use reverb/100.0 to scale)
        mix = min(1.0, reverb / 100.0)
        filters.append(f"ladspa={freeverb}:freeverb_1433")

    # Bass boost using equalizer
    if bass_boost:
        filters.append("bass=g=10")  # You can also try "equalizer=f=60:t=q:w=1:g=10"

    filter_str = ",".join(filters) if filters else "anull"
    command = ["ffmpeg", "-y", "-i", input_file, "-af", filter_str, output_file]

    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"FFmpeg error: {e.stderr.decode()}")

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

    # Get the public URL safely
    try:
        public_url_res = supabase.storage.from_(SUPABASE_BUCKET).get_public_url(destination_name)
        print (public_url_res)
        return public_url_res
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get public URL: {e}")


def cleanup_file(file_path: str):
    try:
        os.remove(file_path)
    except Exception:
        pass

# ========== Endpoints ==========

@app.post("/upload")
def upload_audio(req: UploadRequest):
    audio_id = short_id()
    downloaded_path = download_youtube_audio(req.yt_url, audio_id)
    final_path = os.path.join(TMP_DIR, f"{audio_id}.mp3")
    os.rename(downloaded_path, final_path)
    return {"audio_id": audio_id}

@app.post("/effects")
def apply_effects(req: EffectsRequest, background_tasks: BackgroundTasks):
    raw_audio_path = os.path.join(TMP_DIR, f"{req.audio_id}.mp3")
    if not os.path.exists(raw_audio_path):
        raise HTTPException(status_code=404, detail="Original audio not found.")

    no_effects = req.speed == 1.0 and req.reverb == 0.0 and not req.bass_boost
    suffix = "rawcopy" if no_effects else f"{req.speed}_{req.reverb}_{req.bass_boost}"
    effects_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"{req.audio_id}_{suffix}").hex[:8]

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

    supabase_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/processed/{effects_id}.mp3"
    return {"url": supabase_url}

@app.get("/download/{effects_id}")
def download_effects(effects_id: str):
    file_path = os.path.join(TMP_DIR, f"{effects_id}.mp3")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found or expired.")
    return FileResponse(file_path, filename=f"{effects_id}.mp3", media_type="audio/mpeg")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
