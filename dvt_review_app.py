"""
DVT Case Review: Streamlit Cloud application for clinician QA review.

Results are saved to a Google Sheet owned by the study coordinator.
Which worklist (model-generated vs. random) this deployment shows is set via
st.secrets["worklist_file"] -- never shown in the UI, keeping the study
single-blind. Clip video is streamed from Google Drive (uploaded separately).
"""

import datetime
import json
import re
from pathlib import Path

import gspread
import pandas as pd
import requests
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

# ──────────────────────────────────────────────
# Video embedding
# ──────────────────────────────────────────────
#
# Worklist stream_urls are Drive's ".../file/d/<ID>/preview" iframe-embed
# links, which host Drive's own player (no loop, and autoplay isn't
# reliably controllable from a cross-origin parent page). Verified
# 2026-09-06: "https://drive.usercontent.google.com/download?id=<ID>&export=download"
# serves the same file directly with a correct "video/mp4" content-type,
# open CORS ("access-control-allow-origin: *"), and HTTP 206 byte-range
# support -- so a native <video> tag works and actually supports loop.


def _sticky_panel_js(marker_id: str, css_class: str) -> str:
    """
    Find the Streamlit column that contains the element with id=marker_id
    and add css_class to it, so that column can be styled (sticky + boxed)
    via a plain CSS rule on that class.

    Runs via components.html, which renders in its own iframe but (with
    Streamlit's default sandbox, which includes allow-same-origin) can still
    reach the real page through window.parent.document -- the standard way
    community Streamlit components inject page-level DOM tweaks. Walking up
    from the marker to "the ancestor with a previous sibling" finds the
    column flex-item structurally, without hardcoding Streamlit's internal
    data-testid names, which aren't documented and change across versions.
    """
    return f"""
    <script>
    (function() {{
        function apply() {{
            var doc = window.parent.document;
            var marker = doc.getElementById("{marker_id}");
            if (!marker) return false;
            var el = marker;
            for (var i = 0; i < 8 && el; i++) {{
                el = el.parentElement;
                if (el && el.previousElementSibling) {{
                    el.classList.add("{css_class}");
                    return true;
                }}
            }}
            return false;
        }}
        if (!apply()) {{
            var tries = 0;
            var iv = setInterval(function() {{
                tries++;
                if (apply() || tries > 25) clearInterval(iv);
            }}, 200);
        }}
    }})();
    </script>
    """


def _drive_direct_url(preview_url: str) -> str:
    """Convert a Drive '.../file/d/<ID>/preview' link to a direct,
    range-seekable video/mp4 URL."""
    m = re.search(r"/d/([^/]+)/", preview_url)
    file_id = m.group(1) if m else preview_url
    return f"https://drive.usercontent.google.com/download?id={file_id}&export=download"


@st.cache_data(show_spinner=False, ttl=1800, max_entries=200)
def _fetch_clip_bytes(preview_url: str) -> bytes:
    """
    Download one clip's raw bytes server-side and cache them.

    _drive_direct_url serves the right content-type with open CORS, but its
    response also carries "Cross-Origin-Resource-Policy: same-site" -- a
    browser-enforced header (curl doesn't check it, which is why testing with
    curl alone missed this) that silently blocks a page on a different site
    (our *.streamlit.app domain) from loading it directly into a <video src>,
    even though the resource itself looks perfectly loadable. Fetching the
    bytes here, server-side, isn't subject to that browser policy; handing
    st.video() the raw bytes instead of the URL makes it serve them from
    Streamlit's own origin, which the browser has no reason to block.
    """
    url = _drive_direct_url(preview_url)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content


REVIEW_OPTIONS = {
    "pos": "Pos — vein does not fully compress (thrombus suspected)",
    "neg": "Neg — vein fully compresses (no thrombus)",
    "unsure": "Unsure — technical error / cannot assess",
}

OPTION_LABELS = list(REVIEW_OPTIONS.values())
OPTION_KEYS = list(REVIEW_OPTIONS.keys())

# Patient-level decision, made once per case (not per clip) in the floating
# panel alongside the clip list.
PATIENT_OPTIONS = {
    "no_action":    "No action required - All clips correctly interpreted",
    "feedback":     "Action required - Provider requires feedback",
    "misdiagnosed": "Action required - Patient was misdiagnosed",
}
PATIENT_OPTION_LABELS = list(PATIENT_OPTIONS.values())
PATIENT_OPTION_KEYS = list(PATIENT_OPTIONS.keys())

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
    """
    Load previously saved reviews and first_login for this clinician.
    The sheet has one row per clip, so this reconstructs:
        out[patient] = {"time_spent_seconds": float,
                         "patient_decision": str, "patient_comments": str,
                         "clips": {clip_filename: {decision, comments, reviewed_at}}}
    """
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
        pid = r.get("patient", "")
        if not pid:
            continue
        entry = out.setdefault(
            pid, {"time_spent_seconds": 0.0, "patient_decision": "", "patient_comments": "", "clips": {}}
        )
        t = float(r.get("time_spent_seconds", 0) or 0)
        if t:
            entry["time_spent_seconds"] = t
        if r.get("patient_decision", "").strip():
            entry["patient_decision"] = r["patient_decision"]
        if r.get("patient_comments", "").strip():
            entry["patient_comments"] = r["patient_comments"]
        # Only load clips where a decision was actually made
        if r.get("decision", "").strip():
            entry["clips"][r.get("clip_filename", "")] = {
                "decision": r.get("decision", ""),
                "comments": r.get("comments", ""),
                "reviewed_at": r.get("reviewed_at", ""),
            }
    return out, first_login


def save_all_reviews(spreadsheet, clinician: str, patients_df, clips_by_patient, reviews: dict,
                     first_login: str = "", latest_login: str = "",
                     first_name: str = "", last_name: str = ""):
    """Overwrite the clinician's worksheet with current reviews, one row per clip."""
    headers = [
        "case_number",
        "worklist_arm",
        "patient",
        "clip_filename",
        "ground_truth",
        "fake_user_interpretation",
        "decision",
        "comments",
        "patient_decision",
        "patient_comments",
        "clinician_first_name",
        "clinician_last_name",
        "reviewed_at",
        "time_spent_seconds",
        "first_login",
        "latest_login",
    ]
    ws = get_or_create_worksheet(spreadsheet, _ws_title(clinician), headers)

    arm = worklist_arm()
    rows = [headers]  # start fresh
    for i, (_, row) in enumerate(patients_df.iterrows(), start=1):
        pid = row["patient"]
        rev = reviews.get(pid, {})
        clip_reviews = rev.get("clips", {})
        t = round(rev.get("time_spent_seconds", 0), 1)
        patient_decision = rev.get("patient_decision", "")
        patient_comments = rev.get("patient_comments", "")
        for clip in clips_by_patient.get(pid, []):
            cr = clip_reviews.get(clip["filename"], {})
            rows.append([
                i,
                arm,
                pid,
                clip["filename"],
                clip["label"],
                row.get("fake_user_interpretation", ""),
                cr.get("decision", ""),
                cr.get("comments", ""),
                patient_decision,
                patient_comments,
                first_name,
                last_name,
                cr.get("reviewed_at", ""),
                t,
                first_login,
                latest_login,
            ])

    ws.clear()
    ws.update(rows, value_input_option="RAW")


def _ws_title(clinician: str) -> str:
    """
    Worksheet title from clinician name + worklist arm (max 100 chars for Sheets).
    Including the arm keeps the two deployments from colliding on the same tab
    if the same person ever logs into both (each save overwrites its tab in full,
    so a shared tab would silently wipe out the other arm's results).
    """
    base = clinician.strip().lower().replace(" ", "_")
    return f"{base}_{worklist_arm()}"[:100]


# ──────────────────────────────────────────────
# Data
# ──────────────────────────────────────────────


def worklist_arm() -> str:
    """
    'model' or 'random' depending on which worklist this deployment serves.
    Recorded in the Google Sheet for the study coordinator only -- never
    surfaced in the app UI, so clinicians stay blind to their assigned arm.
    """
    worklist_file = st.secrets.get("worklist_file", "worklist_model.json")
    return Path(worklist_file).stem.replace("worklist_", "")


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

    div[data-testid="stTextArea"] textarea { min-height: 68px; }

    /* Floating patient-level panel. The class itself is defined here, but
       it's ADDED to the real column element by a small JS snippet (see
       _sticky_panel_js below) that walks up from #patient-panel-marker,
       rather than by guessing Streamlit's internal data-testid names in a
       CSS selector -- those are undocumented and change across versions. */
    .patient-panel-box {
        position: sticky !important;
        top: 4.5rem;
        align-self: flex-start;
        background: #ffffff;
        border: 1px solid #d7dde3;
        border-radius: 8px;
        padding: 0.9rem 1rem 1.1rem;
    }

    .ref-read-badge {
        display: block;
        text-align: center;
        font-size: 1.3rem;
        font-weight: 800;
        letter-spacing: 0.02em;
        text-transform: uppercase;
        padding: 0.6rem 0.5rem;
        border-radius: 6px;
        margin-bottom: 0.9rem;
        border: 2px solid #2c4a6e;
        background: #eaf0f7;
        color: #16324f;
    }

    /* Cap clip video height so the video and its assessment options are
       both visible on screen at once without scrolling within the clip.
       Plain "video" tag selector (not a data-testid guess) so this doesn't
       depend on Streamlit's internal markup for st.video's wrapper --
       !important because Streamlit sets width/height inline on the <video>
       element itself, which otherwise wins over an ordinary CSS rule. */
    video {
        width: 100% !important;
        height: 230px !important;
        object-fit: contain !important;
        background: #000;
        display: block;
        margin: 0 auto;
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
        "2. Each case lists all of that patient's clips; opening a clip's "
        "section plays it automatically in a loop — no need to keep "
        "clicking play.\n"
        "3. For **each clip**, select the option that best describes your "
        "assessment of that clip:\n"
        "   - **Pos** — vein does not fully compress (thrombus suspected).\n"
        "   - **Neg** — vein fully compresses (no thrombus).\n"
        "   - **Unsure** — technical error / cannot assess.\n"
        "4. Once you've reviewed every clip, use the **patient-level panel** on "
        "the right to record your overall decision for the case:\n"
        "   - **No action required** — all clips correctly interpreted.\n"
        "   - **Action required — provider requires feedback.**\n"
        "   - **Action required — patient was misdiagnosed.**\n"
        "5. A case is complete once every clip has an assessment **and** the "
        "patient-level decision is selected. Select **Save** or **Next** to "
        "record your progress and continue."
    )

    st.warning(
        "**Taking a break?** Please take breaks **between patients only** — "
        "finish your current patient, select its patient-level decision, then "
        "select **Save** on that case and **Log out** in the sidebar before "
        "stepping away. Your progress is saved per patient and will be "
        "restored exactly where you left off when you log back in."
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

    def _patient_complete(p) -> bool:
        """A patient is complete once every clip has a decision AND the
        patient-level decision has been selected."""
        clips = clips_by_patient.get(p, [])
        if not clips:
            return False
        rev = st.session_state.reviews.get(p, {})
        clip_reviews = rev.get("clips", {})
        all_clips_done = all(clip_reviews.get(c["filename"], {}).get("decision", "") for c in clips)
        return all_clips_done and bool(rev.get("patient_decision", ""))

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
        reviewed = sum(1 for p in patients["patient"] if _patient_complete(p))
        st.progress(reviewed / n_patients)
        st.caption(f"{reviewed} / {n_patients} cases reviewed")
        st.divider()

        st.markdown("**Jump to case**")
        for i, p in enumerate(patients["patient"]):
            label = p if len(p) <= 12 else p[:8] + "…"
            icon = "●" if _patient_complete(p) else "○"
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
                    spreadsheet, clinician, patients, clips_by_patient, st.session_state.reviews,
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
        </div>
        """,
        unsafe_allow_html=True,
    )
    if len(pid) > 16:
        st.caption(f"Full ID: `{pid}`")

    # ── main clip column + floating patient-level panel ───────────
    clips = clips_by_patient.get(pid, [])
    if not clips:
        st.warning("No clips found for this patient. Please contact the study coordinator.")

    main_col, panel_col = st.columns([2.4, 1], gap="large")

    prev_clip_reviews = st.session_state.reviews.get(pid, {}).get("clips", {})
    clip_inputs = []  # collected here, read back by _save_current below

    with main_col:
        for i, clip in enumerate(clips):
            prev = prev_clip_reviews.get(clip["filename"], {})
            prev_decision = prev.get("decision", "")
            done = "●" if prev_decision else "○"

            radio_key = f"radio_{pid}_{i}"
            comments_key = f"comments_{pid}_{i}"
            # Pre-seed session_state from the last saved answer, once, so the
            # widgets below need no "index="/"value=" (which Streamlit would
            # otherwise fight with the key on every later rerun) and so a
            # previously-made choice survives the clip being collapsed again.
            if radio_key not in st.session_state:
                st.session_state[radio_key] = (
                    OPTION_LABELS[OPTION_KEYS.index(prev_decision)] if prev_decision in OPTION_KEYS else None
                )
            if comments_key not in st.session_state:
                st.session_state[comments_key] = prev.get("comments", "")

            # st.toggle (not st.expander) -- its return value is a plain,
            # always-current bool with no ambiguity about when it updates,
            # unlike st.expander's key-tracked open/closed state, which
            # turned out not to behave the way its docs implied and left the
            # clip un-rendered entirely. One click both reveals the clip and
            # starts it playing.
            label = f"{done}  Clip {i + 1} of {len(clips)} ({clip['filename']})"
            is_shown = st.toggle(label, key=f"show_{pid}_{i}")

            if is_shown:
                with st.container(border=True):
                    # Bytes (via _fetch_clip_bytes), not the bare URL -- Drive's
                    # direct-download response is browser-blocked cross-site
                    # (see that function's docstring), so st.video must be
                    # given the actual bytes to serve from Streamlit's own
                    # origin. No ground-truth label is shown here: the
                    # reviewer's assessment must be independent of it.
                    try:
                        clip_bytes = _fetch_clip_bytes(clip["stream_url"])
                        st.video(clip_bytes, autoplay=True, loop=True, muted=True)
                    except Exception as e:
                        st.error(f"Could not load this clip's video: {e}")

                    st.radio("Assessment", options=OPTION_LABELS, key=radio_key)
                    st.text_area(
                        "Additional comments (optional)",
                        placeholder="Any notes on this clip…",
                        key=comments_key,
                    )

            decision_label = st.session_state[radio_key]
            comments = st.session_state[comments_key]
            selected_key = OPTION_KEYS[OPTION_LABELS.index(decision_label)] if decision_label else ""
            clip_inputs.append((clip["filename"], selected_key, comments, prev.get("reviewed_at", "")))

    with panel_col:
        st.markdown('<div id="patient-panel-marker"></div>', unsafe_allow_html=True)
        components.html(_sticky_panel_js("patient-panel-marker", "patient-panel-box"), height=0)
        st.markdown("#### Case decision")
        st.markdown(
            f'<span class="ref-read-badge">Reference read: {fake_interp or "N/A"}</span>',
            unsafe_allow_html=True,
        )

        prev_patient_rev = st.session_state.reviews.get(pid, {})
        prev_patient_decision = prev_patient_rev.get("patient_decision", "")
        patient_default_idx = (
            PATIENT_OPTION_KEYS.index(prev_patient_decision)
            if prev_patient_decision in PATIENT_OPTION_KEYS else None
        )
        patient_decision_label = st.radio(
            "Overall decision for this patient",
            options=PATIENT_OPTION_LABELS,
            index=patient_default_idx,
            key=f"patient_radio_{pid}",
        )
        patient_comments = st.text_area(
            "Patient-level comments (optional)",
            value=prev_patient_rev.get("patient_comments", ""),
            placeholder="Any notes on this case…",
            key=f"patient_comments_{pid}",
        )
        patient_decision_key = (
            PATIENT_OPTION_KEYS[PATIENT_OPTION_LABELS.index(patient_decision_label)]
            if patient_decision_label else ""
        )

    # ── navigation ────────────────────────────
    def _save_current():
        _accumulate_time()
        entry = st.session_state.reviews.setdefault(
            pid, {"time_spent_seconds": 0.0, "patient_decision": "", "patient_comments": "", "clips": {}}
        )
        entry.setdefault("clips", {})
        now = datetime.datetime.now().isoformat()
        for filename, selected_key, comments, prev_reviewed_at in clip_inputs:
            entry["clips"][filename] = {
                "decision": selected_key,
                "comments": comments,
                "reviewed_at": now if selected_key else prev_reviewed_at,
            }
        entry["patient_decision"] = patient_decision_key
        entry["patient_comments"] = patient_comments
        if sheets_ok:
            save_all_reviews(
                spreadsheet, clinician, patients, clips_by_patient, st.session_state.reviews,
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
        if st.button("Save", type="primary", use_container_width=True):
            _save_current()
            st.toast(f"Saved review for {display_name}")

    with col_next:
        if idx < n_patients - 1 and st.button("Next →", use_container_width=True):
            _save_current()
            st.session_state.idx += 1
            st.rerun()

    # ── finish ────────────────────────────────
    reviewed = sum(1 for p in patients["patient"] if _patient_complete(p))
    if reviewed == n_patients:
        st.divider()
        st.success("All cases reviewed!")
        if st.button("View summary & finish", type="primary"):
            _save_current()
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
        clips = clips_by_patient.get(pid, [])
        rev = st.session_state.reviews.get(pid, {})
        clip_reviews = rev.get("clips", {})
        decisions = [clip_reviews.get(c["filename"], {}).get("decision", "") for c in clips]
        n_done = sum(1 for d in decisions if d)
        patient_decision_key = rev.get("patient_decision", "")
        action_needed = "Yes" if patient_decision_key in ("feedback", "misdiagnosed") else "No"
        t = rev.get("time_spent_seconds", 0)
        rows.append({
            "Patient": pid if len(pid) <= 16 else pid[:12] + "…",
            "Clips reviewed": f"{n_done}/{len(clips)}",
            "Patient decision": PATIENT_OPTIONS.get(patient_decision_key, "N/A"),
            "Action needed": action_needed,
            "Time (sec)": round(t, 1) if t else "N/A",
        })
    summary_df = pd.DataFrame(rows)

    def highlight_action(val):
        colors = {"Yes": "#f5eaea", "No": "#eef3ee"}
        bg = colors.get(val, "")
        return f"background-color: {bg}" if bg else ""

    st.dataframe(
        summary_df.style.map(highlight_action, subset=["Action needed"]),
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
