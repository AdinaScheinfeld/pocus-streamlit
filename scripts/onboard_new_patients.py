"""
Onboards new ds2 train-split patients: screens their raw clips for color
Doppler, remuxes survivors to mp4, uploads to their Drive subfolder under
DVT_Worklist_Study_Round2, and writes a worklist-entry-shaped dict per
patient.

Usage:
    python scripts/onboard_new_patients.py PATIENT_ID [PATIENT_ID ...]

Reads clip paths/labels from the ds2 train-split manifest and
ground truth / fake_user_interpretation from ds2_ground_truth.json.
Writes results to data/_new_patients_pool.json (list of worklist-entry
dicts, same shape as one element of a worklist_userN.json file, minus
anonymization -- "patient" is still the real ID at this stage).
"""
import json
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

REPO_DIR = Path(__file__).parent.parent
DATA_DIR = REPO_DIR / "data"
TOKEN_PATH = REPO_DIR / ".streamlit" / "drive_token.json"
MANIFEST_PATH = Path(
    "/midtier/paetzollab/scratch/ads4015/pocus/dvt_classifier_simple_sweep_ds2dicom_5010_40_crop10"
    "/shared_manifests/ratio_8.0/train.json"
)
GROUND_TRUTH_PATH = Path(
    "/midtier/paetzollab/scratch/ads4015/claude_tmp/patient_cm/ds2_ground_truth.json"
)
ROOT_FOLDER_NAME = "DVT_Worklist_Study_Round2"
DOPPLER_THRESHOLD = 0.01
TMP_FRAMES = Path("/midtier/paetzollab/scratch/ads4015/claude_tmp/cd_scan_frames_new")
TMP_MP4 = Path("/midtier/paetzollab/scratch/ads4015/claude_tmp/mp4_new")
TMP_FRAMES.mkdir(parents=True, exist_ok=True)
TMP_MP4.mkdir(parents=True, exist_ok=True)

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def frac_colorful(path) -> float:
    im = np.asarray(Image.open(path).convert("RGB")).astype(int)
    sat = np.max(im, axis=-1) - np.min(im, axis=-1)
    return float((sat > 40).mean())


def scan_clip(local_path: str, tag: str) -> float:
    out_pattern = str(TMP_FRAMES / f"{tag}_%03d.jpg")
    for f in TMP_FRAMES.glob(f"{tag}_*.jpg"):
        f.unlink()
    subprocess.run(
        [FFMPEG, "-y", "-i", local_path, "-vf", "select='not(mod(n\\,12))'",
         "-vsync", "vfr", out_pattern],
        check=True, capture_output=True,
    )
    frames = sorted(TMP_FRAMES.glob(f"{tag}_*.jpg"))
    if not frames:
        return -1.0
    best = max(frac_colorful(f) for f in frames)
    for f in frames:
        f.unlink()
    return best


def mp4_name(filename: str) -> str:
    """
    Just swap the extension to .mp4 -- no need to strip pos/neg from the
    name, since the app fetches clip bytes server-side and serves them via
    Streamlit's own media endpoint (dvt_review_app.py's _fetch_clip_bytes);
    the original filename is never sent to the reviewer's browser in any
    form, so it can't leak the label.
    """
    return f"{Path(filename).stem}.mp4"


def remux(local_mov_path: str, out_path: Path):
    subprocess.run(
        [FFMPEG, "-y", "-i", local_mov_path, "-c", "copy", "-movflags", "+faststart", str(out_path)],
        check=True, capture_output=True,
    )


def drive_service():
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), scopes=["https://www.googleapis.com/auth/drive"])
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("drive", "v3", credentials=creds)


def find_folder(service, name, parent_id=None):
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    res = service.files().list(q=q, fields="files(id,name)").execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def create_folder(service, name, parent_id):
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    f = service.files().create(body=meta, fields="id").execute()
    return f["id"]


def get_or_create_folder(service, name, parent_id):
    fid = find_folder(service, name, parent_id=parent_id)
    return fid if fid else create_folder(service, name, parent_id)


def upload_file(service, local_path, name, parent_id):
    media = MediaFileUpload(str(local_path), mimetype="video/mp4", resumable=True)
    meta = {"name": name, "parents": [parent_id]}
    f = service.files().create(body=meta, media_body=media, fields="id").execute()
    return f["id"]


def make_public(service, file_id):
    service.permissions().create(fileId=file_id, body={"role": "reader", "type": "anyone"}).execute()


def main(patient_ids: list[str]) -> None:
    manifest = json.loads(MANIFEST_PATH.read_text())
    ground_truth = json.loads(GROUND_TRUTH_PATH.read_text())

    service = drive_service()
    root_id = find_folder(service, ROOT_FOLDER_NAME)
    if root_id is None:
        raise SystemExit(f"Could not find root Drive folder {ROOT_FOLDER_NAME!r}")

    pool = []
    for pid in patient_ids:
        clips = [e for e in manifest if e.get("subject") == pid]
        if not clips:
            print(f"  [{pid}] SKIP: no clips found in manifest")
            continue
        print(f"[{pid}] {len(clips)} raw clips -- scanning for color Doppler")

        kept = []
        for c in clips:
            tag = f"{pid}_{Path(c['path']).stem}".replace("/", "_")
            score = scan_clip(c["path"], tag)
            if score > DOPPLER_THRESHOLD:
                print(f"    FLAG (doppler, score={score:.4f}): {c['path']}")
                continue
            kept.append(c)

        print(f"  {len(kept)}/{len(clips)} clips survived Doppler screen -- remuxing + uploading")
        patient_folder_id = get_or_create_folder(service, pid, root_id)

        clip_entries = []
        n_pos = n_neg = 0
        for c in kept:
            new_filename = mp4_name(Path(c["path"]).name)
            out_path = TMP_MP4 / f"{pid}__{new_filename}"
            try:
                remux(c["path"], out_path)
            except subprocess.CalledProcessError as e:
                print(f"    FFMPEG FAILED for {c['path']}: {e.stderr.decode()[-300:]}")
                continue
            file_id = upload_file(service, out_path, new_filename, patient_folder_id)
            make_public(service, file_id)
            out_path.unlink(missing_ok=True)
            label = "DVT" if c["label"] == 1 else "No DVT"
            if c["label"] == 1:
                n_pos += 1
            else:
                n_neg += 1
            clip_entries.append({
                "filename": new_filename,
                "stream_url": f"https://drive.google.com/file/d/{file_id}/preview",
                "label": label,
            })

        gt = ground_truth.get(pid, {})
        pool.append({
            "patient": pid,
            "total_positive_clips": n_pos,
            "total_negative_clips": n_neg,
            "fake_user_interpretation": gt.get("fake_user", ""),
            "clips": clip_entries,
        })
        print(f"  [{pid}] done: {n_pos} pos / {n_neg} neg uploaded")

    out_path = DATA_DIR / "_new_patients_pool.json"
    out_path.write_text(json.dumps(pool, indent=2) + "\n")
    print(f"\nWrote {len(pool)} patients -> {out_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1:])
