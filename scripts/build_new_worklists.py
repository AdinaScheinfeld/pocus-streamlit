"""
Builds worklist_userN.json for new reviewers.

Usage:
    python scripts/build_new_worklists.py user4 user5 user6 user7 user8 user9

Each new user's worklist is:
  - The same 21 shared "model-arm" patients everyone gets (identified as
    the real patients common to all of worklist_user1/2/3.json).
  - 21 patients independently randomly sampled (no repeats within one
    user's own list; repeats across users, and with user1/2/3, are fine)
    from the full ds2 train-split pool: the 63 already-used random-arm
    patients (pulled wholesale from worklist_user1/2/3.json) plus the
    freshly-onboarded patients in data/_new_patients_pool.json.

Output worklists are written in real-ID form (patient = real ID, no
real_patient_id field yet) -- run scripts/assign_case_ids.py on them
afterward to anonymize, same as the original 3 worklists were.
"""
import json
import random
import sys
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
SEED = 20260909  # fixed for reproducibility
RANDOM_CASES_PER_USER = 21

EXISTING_WORKLISTS = ["worklist_user1.json", "worklist_user2.json", "worklist_user3.json"]


def load_existing():
    by_real_id = {}
    for fname in EXISTING_WORKLISTS:
        data = json.loads((DATA_DIR / fname).read_text())
        for entry in data:
            real_id = entry["real_patient_id"]
            # store a real-ID-form copy (drop the case-ID "patient" field,
            # restore "patient" = real_id, matching the pre-anonymization shape)
            clean = {
                "patient": real_id,
                "total_positive_clips": entry["total_positive_clips"],
                "total_negative_clips": entry["total_negative_clips"],
                "fake_user_interpretation": entry["fake_user_interpretation"],
                "clips": entry["clips"],
            }
            by_real_id[real_id] = clean
    return by_real_id


def main(usernames: list[str]) -> None:
    by_real_id = load_existing()
    sets = []
    for fname in EXISTING_WORKLISTS:
        data = json.loads((DATA_DIR / fname).read_text())
        sets.append(set(e["real_patient_id"] for e in data))
    model_ids = sets[0] & sets[1] & sets[2]
    print(f"model-arm: {len(model_ids)} patients")
    model_entries = [by_real_id[rid] for rid in model_ids]

    random_pool_ids = set(by_real_id.keys()) - model_ids
    print(f"already-used random-arm pool: {len(random_pool_ids)} patients")

    new_pool_path = DATA_DIR / "_new_patients_pool.json"
    new_entries = json.loads(new_pool_path.read_text()) if new_pool_path.exists() else []
    for entry in new_entries:
        by_real_id[entry["patient"]] = entry
        random_pool_ids.add(entry["patient"])
    print(f"+ {len(new_entries)} freshly-onboarded patients -> full random pool: {len(random_pool_ids)}")

    random_pool_ids = sorted(random_pool_ids)  # stable order before seeded sampling

    rng = random.Random(SEED)
    for username in usernames:
        n = username.replace("user", "")
        user_random_ids = rng.sample(random_pool_ids, RANDOM_CASES_PER_USER)
        combined = list(model_entries) + [by_real_id[rid] for rid in user_random_ids]
        rng.shuffle(combined)
        out_path = DATA_DIR / f"worklist_{username}.json"
        out_path.write_text(json.dumps(combined, indent=2) + "\n")
        print(f"{username}: wrote {len(combined)} patients -> {out_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1:])
