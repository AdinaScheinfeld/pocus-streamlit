"""
DVT Case Review — Streamlit Cloud application for clinician QA review.

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
        "✅  No action needed — exam performed correctly in all clips "
        "and all clips were interpreted correctly."
    ),
    "b": (
        "⚠️  Action required — exam was performed incorrectly; "
        "provider requires education on technique."
    ),
    "c": (
        "🚨  Action required — clip(s) were interpreted incorrectly "
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


def load_existing_reviews(spreadsheet, clinician: str) -> dict:
    """Load any previously saved reviews for this clinician."""
    safe_title = _ws_title(clinician)
    try:
        ws = spreadsheet.worksheet(safe_title)
        rows = ws.get_all_records()
    except gspread.WorksheetNotFound:
        return {}

    out = {}
    for r in rows:
        out[r["patient"]] = {
            "decision": r.get("decision", ""),
            "comments": r.get("comments", ""),
        }
    return out


def save_all_reviews(spreadsheet, clinician: str, patients_df, reviews: dict):
    """Overwrite the clinician's worksheet with current reviews."""
    headers = [
        "patient",
        "total_positive_clips",
        "total_negative_clips",
        "decision",
        "comments",
        "clinician",
        "timestamp",
    ]
    ws = get_or_create_worksheet(spreadsheet, _ws_title(clinician), headers)

    rows = [headers]  # start fresh
    now = datetime.datetime.now().isoformat()
    for _, row in patients_df.iterrows():
        pid = row["patient"]
        rev = reviews.get(pid, {})
        rows.append([
            pid,
            int(row["total_positive_clips"]),
            int(row["total_negative_clips"]),
            rev.get("decision", ""),
            rev.get("comments", ""),
            clinician,
            now,
        ])

    ws.clear()
    ws.update(rows, value_input_option="RAW")


def _ws_title(clinician: str) -> str:
    """Worksheet title from clinician name (max 100 chars for Sheets)."""
    return clinician.strip().replace(" ", "_")[:100]


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
if "page" not in st.session_state:
    st.session_state.page = "login"
if "idx" not in st.session_state:
    st.session_state.idx = 0
if "reviews" not in st.session_state:
    st.session_state.reviews = {}

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
        "Welcome! Please enter your name to begin reviewing cases. "
        "Your progress is saved automatically and you can resume at any time."
    )

    if not sheets_ok:
        st.error(
            f"⚠️ Could not connect to Google Sheets: {sheets_error}. "
            "Please contact the study coordinator."
        )
        st.stop()

    name = st.text_input("Your full name", placeholder="e.g. Dr. Jane Smith")

    if st.button("Start review", type="primary", disabled=not name.strip()):
        st.session_state.clinician = name.strip()
        with st.spinner("Loading your saved progress…"):
            existing = load_existing_reviews(spreadsheet, name.strip())
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
    pos = int(row["total_positive_clips"])
    neg = int(row["total_negative_clips"])

    # ── sidebar ───────────────────────────────
    with st.sidebar:
        st.markdown(f"**Reviewer:** {clinician}")
        reviewed = sum(
            1 for p in patients["patient"] if p in st.session_state.reviews
        )
        st.progress(reviewed / n_patients)
        st.caption(f"{reviewed} / {n_patients} cases reviewed")
        st.divider()

        st.markdown("**Jump to case**")
        for i, p in enumerate(patients["patient"]):
            label = p if len(p) <= 12 else p[:8] + "…"
            icon = "✅" if p in st.session_state.reviews else "⬜"
            if st.button(
                f"{icon}  {i + 1}. {label}",
                key=f"jump_{i}",
                use_container_width=True,
            ):
                st.session_state.idx = i
                st.rerun()

        st.divider()
        if st.button("🔒 Log out", use_container_width=True):
            if sheets_ok:
                save_all_reviews(
                    spreadsheet, clinician, patients, st.session_state.reviews
                )
            for k in ("clinician", "page", "idx", "reviews"):
                del st.session_state[k]
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
                <span class="badge-pos">DVT-positive clips: {pos}</span>
                <span class="badge-neg">DVT-negative clips: {neg}</span>
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
        st.session_state.reviews[pid] = {
            "decision": selected_key,
            "comments": comments,
        }
        if sheets_ok:
            save_all_reviews(
                spreadsheet, clinician, patients, st.session_state.reviews
            )

    col_prev, col_save, col_next = st.columns([1, 1, 1])

    with col_prev:
        if idx > 0 and st.button("← Previous", use_container_width=True):
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
        1 for p in patients["patient"] if p in st.session_state.reviews
    )
    if reviewed == n_patients:
        st.divider()
        st.success("All cases reviewed!")
        if st.button("📋 View summary & finish", type="primary"):
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

    rows = []
    for _, row in patients.iterrows():
        pid = row["patient"]
        rev = st.session_state.reviews.get(pid, {})
        rows.append({
            "Patient": pid if len(pid) <= 16 else pid[:12] + "…",
            "Pos clips": int(row["total_positive_clips"]),
            "Neg clips": int(row["total_negative_clips"]),
            "Decision": rev.get("decision", "—").upper(),
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
            for k in ("clinician", "page", "idx", "reviews"):
                del st.session_state[k]
            st.rerun()
