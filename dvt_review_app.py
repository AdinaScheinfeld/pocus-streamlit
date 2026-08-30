"""
DVT Case Review: Streamlit Cloud application for clinician QA review.

Results are saved to a Google Sheet owned by the study coordinator.
Which worklist (model-generated vs. random) this deployment shows is set via
st.secrets["worklist_file"] -- never shown in the UI, keeping the study
single-blind. Clip video is streamed from Google Drive (uploaded separately).
"""

import datetime
import json
from pathlib import Path

import gspread
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from google.oauth2.service_account import Credentials

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────

APP_DIR = Path(__file__).parent
DATA_DIR = APP_DIR / "data"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

REVIEW_OPTIONS = {
    "a": "No action needed: exam and interpretation both correct.",
    "b": "Action required: technique issue; provider needs education.",
    "c": "Action required: interpretation error; patient needs callback.",
}

OPTION_LABELS = list(REVIEW_OPTIONS.values())
OPTION_KEYS = list(REVIEW_OPTIONS.keys())

# ──────────────────────────────────────────────
# Google Sheets helpers
# ──────────────────────────────────────────────


@st.cache_resource
def get_gspread_client():
    creds_dict = dict(st.secrets["gcp_service_account"])
    # Fix private key: Streamlit TOML sometimes keeps literal \n as two chars
    pk = creds_dict.get("private_key", "")
    pk = pk.replace("\\n", "\n")
    # Strip leading/trailing whitespace that TOML triple-quotes may introduce
    pk = pk.strip()
    creds_dict["private_key"] = pk
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def get_or_create_worksheet(spreadsheet, title, headers):
    """Get existing worksheet or create a new one with headers."""
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=200, cols=len(headers))
        ws.append_row(headers)
    return ws


def load_existing_reviews(spreadsheet, clinician: str) -> tuple[dict, str]:
    """Load previously saved reviews and first_login for this clinician."""
    safe_title = _ws_title(clinician)
    try:
        ws = spreadsheet.worksheet(safe_title)
        rows = ws.get_all_records()
    except gspread.WorksheetNotFound:
        return {}, ""

    out = {}
    first_login = ""
    for r in rows:
        if not first_login and r.get("first_login", "").strip():
            first_login = r["first_login"]
        # Only load rows where a decision was actually made
        if r.get("decision", "").strip():
            out[r["patient"]] = {
                "decision": r.get("decision", ""),
                "comments": r.get("comments", ""),
                "reviewed_at": r.get("reviewed_at", ""),
                "time_spent_seconds": float(r.get("time_spent_seconds", 0) or 0),
            }
    return out, first_login


def save_all_reviews(spreadsheet, clinician: str, patients_df, reviews: dict,
                     first_login: str = "", latest_login: str = "",
                     first_name: str = "", last_name: str = ""):
    """Overwrite the clinician's worksheet with current reviews."""
    headers = [
        "case_number",
        "patient",
        "total_positive_clips",
        "total_negative_clips",
        "fake_user_interpretation",
        "decision",
        "comments",
        "clinician_first_name",
        "clinician_last_name",
        "reviewed_at",
        "time_spent_seconds",
        "first_login",
        "latest_login",
    ]
    ws = get_or_create_worksheet(spreadsheet, _ws_title(clinician), headers)

    rows = [headers]  # start fresh
    for i, (_, row) in enumerate(patients_df.iterrows(), start=1):
        pid = row["patient"]
        rev = reviews.get(pid, {})
        rows.append([
            i,
            pid,
            int(row["total_positive_clips"]),
            int(row["total_negative_clips"]),
            row.get("fake_user_interpretation", ""),
            rev.get("decision", ""),
            rev.get("comments", ""),
            first_name,
            last_name,
            rev.get("reviewed_at", ""),
            round(rev.get("time_spent_seconds", 0), 1),
            first_login,
            latest_login,
        ])

    ws.clear()
    ws.update(rows, value_input_option="RAW")


def _ws_title(clinician: str) -> str:
    """Worksheet title from clinician name (max 100 chars for Sheets)."""
    return clinician.strip().lower().replace(" ", "_")[:100]


# ──────────────────────────────────────────────
# Data
# ──────────────────────────────────────────────


@st.cache_data
def load_worklist():
    """
    Loads the worklist assigned to this deployment via st.secrets["worklist_file"]
    (e.g. "worklist_model.json" or "worklist_random.json") -- never surfaced in
    the UI, so clinicians can't tell which arm they're on.

    Returns:
        patients_df : DataFrame with one row per patient (summary fields only)
        clips_by_patient : dict[patient_id] -> list of {filename, stream_url, label}
    """
    worklist_file = st.secrets.get("worklist_file", "worklist_model.json")
    path = DATA_DIR / worklist_file
    data = json.loads(path.read_text())

    patients_df = pd.DataFrame([
        {k: v for k, v in p.items() if k != "clips"} for p in data
    ])
    clips_by_patient = {p["patient"]: p["clips"] for p in data}
    return patients_df, clips_by_patient


# ──────────────────────────────────────────────
# Page config & CSS
# ──────────────────────────────────────────────

st.set_page_config(page_title="DVT Case Review", page_icon="🩺", layout="centered")

st.markdown(
    """
    <style>
    /* Reduce default Streamlit top padding */
    .block-container { padding-top: 0.9rem !important; padding-bottom: 1rem !important; }
    header[data-testid="stHeader"] { display: none; }

    h1, h2, h3, h4 { font-family: Georgia, 'Times New Roman', serif; color: #1f2937; }

    .case-banner {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        flex-wrap: wrap;
        gap: 0.35rem 1.2rem;
        background: #f4f6f8;
        border: 1px solid #d7dde3;
        border-left: 4px solid #2c4a6e;
        border-radius: 6px;
        padding: 0.55rem 1rem;
        margin-bottom: 0.6rem;
        font-size: 0.92rem;
        color: #374151;
    }
    .case-banner .patient-id { font-weight: 700; color: #1f2937; font-size: 1.02rem; }
    .case-banner .sep { color: #b0b8c1; }
    .case-banner .ref-read { font-weight: 600; color: #2c4a6e; }

    div[data-testid="stTextArea"] textarea { min-height: 68px; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ──────────────────────────────────────────────
# Session state defaults
# ──────────────────────────────────────────────

if "clinician" not in st.session_state:
    st.session_state.clinician = ""
if "clinician_first" not in st.session_state:
    st.session_state.clinician_first = ""
if "clinician_last" not in st.session_state:
    st.session_state.clinician_last = ""
if "page" not in st.session_state:
    st.session_state.page = "login"
if "idx" not in st.session_state:
    st.session_state.idx = 0
if "reviews" not in st.session_state:
    st.session_state.reviews = {}
if "session_start" not in st.session_state:
    st.session_state.session_start = ""
if "first_login" not in st.session_state:
    st.session_state.first_login = ""
if "patient_start_time" not in st.session_state:
    st.session_state.patient_start_time = None

patients, clips_by_patient = load_worklist()
n_patients = len(patients)

# Connect to Google Sheets
try:
    gc = get_gspread_client()
    spreadsheet = gc.open_by_key(st.secrets["spreadsheet_id"])
    sheets_ok = True
except Exception as e:
    sheets_ok = False
    sheets_error = str(e)

# ──────────────────────────────────────────────
# LOGIN
# ──────────────────────────────────────────────

if st.session_state.page == "login":
    st.markdown("## DVT Case Review Portal")
    st.markdown(
        "This portal is for quality assurance review of DVT ultrasound cases. "
        "Your progress is saved automatically and you may resume at any time."
    )

    st.markdown("#### How it works")
    st.markdown(
        "1. Enter your name below and select **Start review**.\n"
        "2. For each case, review the patient's clips using the player and clip "
        "selector on the page; a reference read is shown for each case.\n"
        "3. Select the option that best describes your assessment:\n"
        "   - **(a) No action needed.** The exam was performed correctly and "
        "all clips were interpreted correctly.\n"
        "   - **(b) Action required (technique).** The exam was performed "
        "incorrectly and the provider requires education on technique.\n"
        "   - **(c) Action required (interpretation).** Clip(s) were interpreted "
        "incorrectly (DVT-positive read as negative or vice versa) and the "
        "patient needs to be called back.\n"
        "4. Select **Save** or **Next** to record your assessment and continue."
    )

    st.info(
        "**A note about timing:** Per-case review time is recorded for logging purposes only. "
        "This is not a race; please take as long as you need on each case. "
        "To take a break, select **Save** on your current case and then **Log out** "
        "in the sidebar. Your progress will be restored when you return."
    )

    if not sheets_ok:
        st.error(
            f"Could not connect to Google Sheets: {sheets_error}. "
            "Please contact the study coordinator."
        )
        st.stop()

    name_col1, name_col2 = st.columns(2)
    with name_col1:
        first_name = st.text_input("First name", placeholder="e.g. Jane")
    with name_col2:
        last_name = st.text_input("Last name", placeholder="e.g. Smith")

    both_filled = first_name.strip() and last_name.strip()

    if st.button("Start review", type="primary", disabled=not both_filled):
        fn = first_name.strip()
        ln = last_name.strip()
        display_name = f"{fn} {ln}"
        st.session_state.clinician = display_name
        st.session_state.clinician_first = fn.lower()
        st.session_state.clinician_last = ln.lower()
        now = datetime.datetime.now().isoformat()
        st.session_state.session_start = now
        with st.spinner("Loading your saved progress…"):
            existing, saved_first_login = load_existing_reviews(
                spreadsheet, display_name
            )
        # Preserve the original first_login; set it only on first-ever session
        st.session_state.first_login = saved_first_login or now
        if existing:
            st.session_state.reviews = existing
            st.toast(f"Restored {len(existing)} previous review(s).")
        st.session_state.page = "review"
        st.session_state.idx = 0
        st.rerun()

    st.stop()

# ──────────────────────────────────────────────
# REVIEW
# ──────────────────────────────────────────────

if st.session_state.page == "review":
    clinician = st.session_state.clinician
    idx = st.session_state.idx
    row = patients.iloc[idx]
    pid = row["patient"]
    total_clips = int(row["total_positive_clips"]) + int(row["total_negative_clips"])
    fake_interp = str(row.get("fake_user_interpretation", "")).strip().upper()

    # ── per-patient timer ─────────────────────
    # Start timer when a new patient is displayed; only reset on patient change
    if st.session_state.get("_current_pid") != pid:
        # Accumulate time on the previous patient before switching
        prev_pid = st.session_state.get("_current_pid")
        if prev_pid and st.session_state.patient_start_time:
            elapsed = (datetime.datetime.now()
                       - st.session_state.patient_start_time).total_seconds()
            prev_rev = st.session_state.reviews.get(prev_pid, {})
            prev_rev["time_spent_seconds"] = prev_rev.get("time_spent_seconds", 0) + elapsed
            st.session_state.reviews[prev_pid] = prev_rev
        st.session_state.patient_start_time = datetime.datetime.now()
        st.session_state._current_pid = pid

    def _accumulate_time():
        """Add elapsed time on current patient to its running total."""
        if st.session_state.patient_start_time:
            elapsed = (datetime.datetime.now()
                       - st.session_state.patient_start_time).total_seconds()
            cur = st.session_state.reviews.get(pid, {})
            cur["time_spent_seconds"] = cur.get("time_spent_seconds", 0) + elapsed
            st.session_state.reviews[pid] = cur
            # Reset so we don't double-count on the next rerun
            st.session_state.patient_start_time = datetime.datetime.now()

    # ── sidebar ───────────────────────────────
    with st.sidebar:
        st.markdown(f"**Reviewer:** {clinician}")
        reviewed = sum(
            1 for p in patients["patient"] if st.session_state.reviews.get(p, {}).get("decision", "")
        )
        st.progress(reviewed / n_patients)
        st.caption(f"{reviewed} / {n_patients} cases reviewed")
        st.divider()

        st.markdown("**Jump to case**")
        for i, p in enumerate(patients["patient"]):
            label = p if len(p) <= 12 else p[:8] + "…"
            icon = "●" if st.session_state.reviews.get(p, {}).get("decision", "") else "○"
            is_current = (i == idx)
            if st.button(
                f"{icon}  {i + 1}. {label}",
                key=f"jump_{i}",
                use_container_width=True,
                type="primary" if is_current else "secondary",
            ):
                _accumulate_time()
                st.session_state.idx = i
                st.rerun()

        st.divider()
        if st.button("Log out", use_container_width=True):
            _accumulate_time()
            if sheets_ok:
                save_all_reviews(
                    spreadsheet, clinician, patients, st.session_state.reviews,
                    first_login=st.session_state.first_login,
                    latest_login=st.session_state.session_start,
                    first_name=st.session_state.clinician_first,
                    last_name=st.session_state.clinician_last,
                )
            for k in ("clinician", "clinician_first", "clinician_last", "page", "idx", "reviews", "session_start", "first_login",
                       "patient_start_time", "_current_pid"):
                st.session_state.pop(k, None)
            st.rerun()

    # ── compact case banner ────────────────────
    display_name = pid if len(pid) <= 16 else f"{pid[:16]}…"
    st.markdown(
        f"""
        <div class="case-banner">
            <span class="patient-id">{display_name}</span>
            <span><span class="sep">·</span> Case {idx + 1} of {n_patients}</span>
            <span><span class="sep">·</span> {total_clips} clip{"s" if total_clips != 1 else ""}</span>
            <span><span class="sep">·</span> Reference read:
                <span class="ref-read">{fake_interp or "N/A"}</span></span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if len(pid) > 16:
        st.caption(f"Full ID: `{pid}`")

    # ── clip viewer ───────────────────────────
    clips = clips_by_patient.get(pid, [])
    if not clips:
        st.warning("No clips found for this patient. Please contact the study coordinator.")
    else:
        clip_options = list(range(len(clips)))

        def _clip_label(i):
            return f"Clip {i + 1} of {len(clips)}"

        sel = st.selectbox(
            "Clip",
            options=clip_options,
            format_func=_clip_label,
            key=f"clipsel_{pid}",
        )
        # Embedded via Google Drive's own player (iframe), not st.video() --
        # Drive's raw-file download links have no file extension and report
        # a generic octet-stream content-type, which many browsers refuse to
        # play inline in a <video> tag. The /preview endpoint serves an actual
        # HTML page with Drive's hosted player, which handles decoding itself.
        components.iframe(clips[sel]["stream_url"], height=320)

    # ── review form ───────────────────────────
    prev = st.session_state.reviews.get(pid, {})
    prev_decision = prev.get("decision", "")
    prev_comments = prev.get("comments", "")

    default_idx = (
        OPTION_KEYS.index(prev_decision) if prev_decision in OPTION_KEYS else None
    )

    decision = st.radio(
        "Assessment",
        options=OPTION_LABELS,
        index=default_idx,
        key=f"radio_{pid}",
    )

    selected_key = OPTION_KEYS[OPTION_LABELS.index(decision)] if decision else ""

    comments = st.text_area(
        "Additional comments (optional)",
        value=prev_comments,
        placeholder="Any notes on this case…",
        key=f"comments_{pid}",
    )

    # ── navigation ────────────────────────────
    def _save_current():
        _accumulate_time()
        existing = st.session_state.reviews.get(pid, {})
        existing.update({
            "decision": selected_key,
            "comments": comments,
            "reviewed_at": datetime.datetime.now().isoformat(),
        })
        st.session_state.reviews[pid] = existing
        if sheets_ok:
            save_all_reviews(
                spreadsheet, clinician, patients, st.session_state.reviews,
                first_login=st.session_state.first_login,
                    latest_login=st.session_state.session_start,
                    first_name=st.session_state.clinician_first,
                    last_name=st.session_state.clinician_last,
            )

    col_prev, col_save, col_next = st.columns([1, 1, 1])

    with col_prev:
        if idx > 0 and st.button("← Previous", use_container_width=True):
            _accumulate_time()
            st.session_state.idx -= 1
            st.rerun()

    with col_save:
        if st.button(
            "Save",
            type="primary",
            use_container_width=True,
            disabled=decision is None,
        ):
            _save_current()
            st.toast(f"Saved review for {display_name}")

    with col_next:
        if idx < n_patients - 1 and st.button("Next →", use_container_width=True):
            if decision is not None:
                _save_current()
            st.session_state.idx += 1
            st.rerun()

    # ── finish ────────────────────────────────
    reviewed = sum(
        1 for p in patients["patient"] if st.session_state.reviews.get(p, {}).get("decision", "")
    )
    if reviewed == n_patients:
        st.divider()
        st.success("All cases reviewed!")
        if st.button("View summary & finish", type="primary"):
            _accumulate_time()
            if sheets_ok:
                save_all_reviews(
                    spreadsheet, clinician, patients, st.session_state.reviews,
                    first_login=st.session_state.first_login,
                    latest_login=st.session_state.session_start,
                    first_name=st.session_state.clinician_first,
                    last_name=st.session_state.clinician_last,
                )
            st.session_state.page = "done"
            st.rerun()

    st.stop()

# ──────────────────────────────────────────────
# DONE / SUMMARY
# ──────────────────────────────────────────────

if st.session_state.page == "done":
    clinician = st.session_state.clinician
    st.markdown("## Review complete")
    st.markdown(f"**Reviewer:** {clinician}")

    # Show session duration
    if st.session_state.session_start:
        start = datetime.datetime.fromisoformat(st.session_state.session_start)
        elapsed = datetime.datetime.now() - start
        mins = int(elapsed.total_seconds() // 60)
        secs = int(elapsed.total_seconds() % 60)
        st.markdown(f"**Session duration:** {mins} min {secs} sec")

    rows = []
    for _, row in patients.iterrows():
        pid = row["patient"]
        rev = st.session_state.reviews.get(pid, {})
        t = rev.get("time_spent_seconds", 0)
        rows.append({
            "Patient": pid if len(pid) <= 16 else pid[:12] + "…",
            "Total clips": int(row["total_positive_clips"]) + int(row["total_negative_clips"]),
            "Decision": rev.get("decision", "N/A").upper(),
            "Time (sec)": round(t, 1) if t else "N/A",
            "Comments": rev.get("comments", ""),
        })
    summary_df = pd.DataFrame(rows)

    def highlight_decision(val):
        colors = {"A": "#eef3ee", "B": "#f7f1e3", "C": "#f5eaea"}
        bg = colors.get(val.strip(), "")
        return f"background-color: {bg}" if bg else ""

    st.dataframe(
        summary_df.style.map(highlight_decision, subset=["Decision"]),
        use_container_width=True,
        hide_index=True,
    )

    st.info("Your reviews have been saved to the shared Google Sheet. Thank you!")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("← Back to review", use_container_width=True):
            st.session_state.page = "review"
            st.rerun()
    with col2:
        if st.button("Log out", use_container_width=True):
            for k in ("clinician", "clinician_first", "clinician_last", "page", "idx", "reviews", "session_start", "first_login",
                       "patient_start_time", "_current_pid"):
                st.session_state.pop(k, None)
            st.rerun()
