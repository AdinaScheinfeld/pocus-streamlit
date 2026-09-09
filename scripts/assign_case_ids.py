"""
Assigns anonymized case IDs to patients in one or more worklist files.

Usage:
    python scripts/assign_case_ids.py data/worklist_user1.json data/worklist_user2.json ...

For each patient entry in each given worklist file:
  - If "patient" is already a known case ID (this file was anonymized by
    a previous run), the entry is left untouched -- safe to re-run.
  - Otherwise "patient" is treated as a real patient ID (e.g. "VR-5-10-23"),
    looked up in the crosswalk (data/patient_crosswalk.json), assigned the
    next sequential case ID if not already present, and the entry is
    rewritten with "patient" = case ID and "real_patient_id" = the
    original real ID.

The crosswalk is the single source of truth for real ID <-> case ID.
It only ever grows: a real patient already in it keeps its case ID
forever, so running this again on a new worklist (e.g. for a future
user4-8, sharing the same model-patient pool but with different random
patients) reuses existing case IDs for repeated patients and appends
fresh ones only for patients never seen before.
"""
import json
import sys
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
CROSSWALK_PATH = DATA_DIR / "patient_crosswalk.json"


def load_crosswalk() -> dict:
    if CROSSWALK_PATH.exists():
        return json.loads(CROSSWALK_PATH.read_text())
    return {}  # real_id -> case_id


def save_crosswalk(crosswalk: dict) -> None:
    CROSSWALK_PATH.write_text(json.dumps(crosswalk, indent=2) + "\n")


def next_case_id(crosswalk: dict) -> str:
    return f"Case-{len(crosswalk) + 1:03d}"


def process_worklist(path: Path, crosswalk: dict, known_case_ids: set) -> bool:
    data = json.loads(path.read_text())
    changed = False
    for entry in data:
        current = entry["patient"]
        if current in known_case_ids:
            continue  # already anonymized in a previous run
        real_id = current
        case_id = crosswalk.get(real_id)
        if case_id is None:
            case_id = next_case_id(crosswalk)
            crosswalk[real_id] = case_id
            known_case_ids.add(case_id)
        entry["patient"] = case_id
        entry["real_patient_id"] = real_id
        changed = True
    if changed:
        path.write_text(json.dumps(data, indent=2) + "\n")
    return changed


def main(paths: list[str]) -> None:
    crosswalk = load_crosswalk()
    known_case_ids = set(crosswalk.values())
    for p in paths:
        path = Path(p)
        changed = process_worklist(path, crosswalk, known_case_ids)
        print(f"{path}: {'updated' if changed else 'no change (already anonymized)'}")
    save_crosswalk(crosswalk)
    print(f"Crosswalk now has {len(crosswalk)} patients -> {CROSSWALK_PATH}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1:])
