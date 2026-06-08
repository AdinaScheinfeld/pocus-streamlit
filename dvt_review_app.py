"""
DVT Case Review - Streamlit Cloud application for clinician QA review.

Results are saved to a Google Sheet owned by the study coordinator.
Patient data is bundled in the repo (patients_with_dvt.csv).
"""

import datetime
from pathlib import Path

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────

APP_DIR = Path(__file__).parent
PATIENTS_CSV = APP_DIR / "patients_with_dvt.csv"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

REVIEW_OPTIONS = {
    "a": (
        "✅  No action needed: exam performed correctly in all clips "
        "and all clips were interpreted correctly."
    ),
    "b": (
        "⚠️  Action required: exam was performed incorrectly; "
        "provider requires education on technique."
    ),
    "c": (
        "🚨  Action required: clip(s) were interpreted incorrectly "
        "(DVT-positive read as negative OR vice versa); "
        "patient needs to be called back."
    ),
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
        "patient",
        "total_positive_clips",
        "total_negative_clips",
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
    for _, row in patients_df.iterrows():
        pid = row["patient"]
        rev = reviews.get(pid, {})
        rows.append([
            pid,
            int(row["total_positive_clips"]),
            int(row["total_negative_clips"]),
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
def load_patients() -> pd.DataFrame:
    return pd.read_csv(PATIENTS_CSV)


# ──────────────────────────────────────────────
# Page config & CSS
# ──────────────────────────────────────────────

st.set_page_config(page_title="DVT Case Review", page_icon="🩺", layout="centered")

st.markdown(
    """
    <style>
    .patient-card {
        background: linear-gradient(135deg, #f8fafc 0%, #eef2f7 100%);
        border: 1px solid #d0d7de;
        border-radius: 12px;
        padding: 1.5rem 2rem;
        margin-bottom: 1.2rem;
    }
    .patient-card h2 { margin: 0 0 0.3rem 0; font-size: 1.5rem; }
    .clip-badges span {
        display: inline-block; padding: 4px 14px; border-radius: 20px;
        font-weight: 600; font-size: 0.95rem; margin-right: 8px;
    }
    .badge-pos { background: #fee2e2; color: #b91c1c; }
    .badge-neg { background: #dcfce7; color: #166534; }
    .badge-total { background: #e0e7ff; color: #3730a3; }
    .progress-text {
        text-align: center; color: #64748b;
        font-size: 0.9rem; margin-bottom: 0.4rem;
    }
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

patients = load_patients()
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
    st.markdown("## 🩺 DVT Case Review Portal")
    st.markdown(
        "Welcome! This portal is for quality assurance review of DVT ultrasound cases. "
        "Your progress is saved automatically and you can resume at any time."
    )

    st.markdown("#### How it works")
    st.markdown(
        "1. Enter your name below and click **Start review**.\n"
        "2. For each case, open the patient's clips in **QPath** and review all clips.\n"
        "3. After reviewing, select the option that best describes your assessment:\n"
        "   - **(a) No action needed**: the exam was performed correctly and "
        "all clips were interpreted correctly.\n"
        "   - **(b) Action required (technique)**: the exam was performed "
        "incorrectly and the provider requires education on technique.\n"
        "   - **(c) Action required (interpretation)**: clip(s) were interpreted "
        "incorrectly (DVT-positive read as negative or vice versa) and the "
        "patient needs to be called back.\n"
        "4. Click **Save** or **Next** to record your assessment and move on."
    )

    if not sheets_ok:
        st.error(
            f"⚠️ Could not connect to Google Sheets: {sheets_error}. "
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
            st.toast(
                f"Restored {len(existing)} previous review(s).", icon="📂"
            )
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
            icon = "✅" if st.session_state.reviews.get(p, {}).get("decision", "") else "⬜"
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
        if st.button("🔒 Log out", use_container_width=True):
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

    # ── header ────────────────────────────────
    st.markdown(
        f'<p class="progress-text">Case {idx + 1} of {n_patients}</p>',
        unsafe_allow_html=True,
    )

    # ── patient card ──────────────────────────
    display_name = pid if len(pid) <= 12 else f"{pid[:16]}…"
    st.markdown(
        f"""
        <div class="patient-card">
            <h2>Patient: {display_name}</h2>
            <div class="clip-badges">
                <span class="badge-total">Total clips: {total_clips}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if len(pid) > 12:
        st.caption(f"Full ID: `{pid}`")

    # ── review form ───────────────────────────
    st.markdown("#### Your assessment")

    prev = st.session_state.reviews.get(pid, {})
    prev_decision = prev.get("decision", "")
    prev_comments = prev.get("comments", "")

    default_idx = (
        OPTION_KEYS.index(prev_decision) if prev_decision in OPTION_KEYS else None
    )

    decision = st.radio(
        "Select one option:",
        options=OPTION_LABELS,
        index=default_idx,
        key=f"radio_{pid}",
    )

    selected_key = OPTION_KEYS[OPTION_LABELS.index(decision)] if decision else ""

    comments = ""
    if selected_key in ("b", "c"):
        comments = st.text_area(
            "Additional comments (optional)",
            value=prev_comments,
            placeholder="Describe what was incorrect or any follow-up notes…",
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
            "💾 Save",
            type="primary",
            use_container_width=True,
            disabled=decision is None,
        ):
            _save_current()
            st.toast(f"Saved review for {display_name}", icon="✅")

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
        if st.button("📋 View summary & finish", type="primary"):
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
    st.markdown("## ✅ Review complete")
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
            "Decision": rev.get("decision", "—").upper(),
            "Time (sec)": round(t, 1) if t else "—",
            "Comments": rev.get("comments", ""),
        })
    summary_df = pd.DataFrame(rows)

    def highlight_decision(val):
        colors = {"A": "#dcfce7", "B": "#fef9c3", "C": "#fee2e2"}
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
        if st.button("🔒 Log out", use_container_width=True):
            for k in ("clinician", "clinician_first", "clinician_last", "page", "idx", "reviews", "session_start", "first_login",
                       "patient_start_time", "_current_pid"):
                st.session_state.pop(k, None)
            st.rerun()