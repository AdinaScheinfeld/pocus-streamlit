"""
Assigns anonymized case IDs to patients in one or more worklist files.

Usage:
    python scripts/assign_case_ids.py data/worklist_user1.json data/worklist_user2.json ...

For each patient entry in each given worklist file:
  - If "patient" is already a known case ID (this file was anonymized by
    a previous run), the entry is left untouched -- safe to re-run.
  - Otherwise "patient" is treated as a real patient ID (e.g. "VR-5-10-23"):
    its case ID is its 1-indexed position in data/patient_alpha_order.json
    (the canonical alphabetical patient list), and the entry is rewritten
    with "patient" = case ID and "real_patient_id" = the original real ID.

Case numbers come from patient_alpha_order.json, not from the order
patients happen to be encountered in -- so a given real patient always
gets the same case ID no matter which worklist (or how many) it appears
in, and onboarding new patients/users never renumbers anyone else.
data/patient_crosswalk.json is kept as a real_id -> case_id record of
every patient actually used so far (handy for the results sheet /
coordinator lookup), not as the source of the numbering itself.
"""
import json
import sys
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
CROSSWALK_PATH = DATA_DIR / "patient_crosswalk.json"
ALPHA_ORDER_PATH = DATA_DIR / "patient_alpha_order.json"


def load_crosswalk() -> dict:
    if CROSSWALK_PATH.exists():
        return json.loads(CROSSWALK_PATH.read_text())
    return {}  # real_id -> case_id


def save_crosswalk(crosswalk: dict) -> None:
    CROSSWALK_PATH.write_text(json.dumps(crosswalk, indent=2) + "\n")


def load_alpha_order() -> list:
    return json.loads(ALPHA_ORDER_PATH.read_text())


def case_id_for(real_id: str, alpha_order: list) -> str:
    try:
        idx = alpha_order.index(real_id)
    except ValueError:
        raise SystemExit(
            f"'{real_id}' is not in {ALPHA_ORDER_PATH} -- add it to the canonical "
            "alphabetical patient list before anonymizing a worklist that uses it."
        )
    return f"Case-{idx + 1:03d}"


def process_worklist(path: Path, crosswalk: dict, alpha_order: list, known_case_ids: set) -> bool:
    data = json.loads(path.read_text())
    changed = False
    for entry in data:
        current = entry["patient"]
        if current in known_case_ids:
            continue  # already anonymized in a previous run
        real_id = current
        case_id = case_id_for(real_id, alpha_order)
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
    alpha_order = load_alpha_order()
    known_case_ids = set(crosswalk.values())
    for p in paths:
        path = Path(p)
        changed = process_worklist(path, crosswalk, alpha_order, known_case_ids)
        print(f"{path}: {'updated' if changed else 'no change (already anonymized)'}")
    save_crosswalk(crosswalk)
    print(f"Crosswalk now has {len(crosswalk)} patients -> {CROSSWALK_PATH}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1:])
