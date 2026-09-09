"""
DVT Case Review: Streamlit Cloud application for clinician QA review.

Results are saved to a Google Sheet owned by the study coordinator, one
worksheet tab per clinician. Which worklist a clinician sees is keyed off
the name they type at login (see WORKLIST_BY_CLINICIAN) -- one deployment
serves every clinician, each with their own pre-built worklist (21 shared
model-selected patients + that clinician's own 21 random patients,
pre-shuffled together). Clip video is streamed from Google Drive (uploaded
separately).
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


def _fixed_corner_js(button_text: str, top: str = "0.7rem", right: str = "1.2rem") -> str:
    """
    Pin the (single, currently-rendered) button whose visible text matches
    button_text to a fixed spot in the top-right of the viewport, so it stays
    reachable regardless of scroll position -- in the sidebar's own case
    list (which can run to dozens of entries) or in the main content.

    Styling the button element directly via its computed text (rather than
    walking to some ancestor, as _sticky_panel_js does for the column-based
    patient panel) sidesteps needing to know Streamlit's current DOM nesting
    for a plain st.button call, which isn't part of its public contract.
    position:fixed anchors to the viewport itself, escaping every ancestor's
    scroll container (sidebar included) without special-casing which one.
    """
    return f"""
    <script>
    (function() {{
        function apply() {{
            var doc = window.parent.document;
            var buttons = doc.querySelectorAll('button');
            for (var i = 0; i < buttons.length; i++) {{
                var btn = buttons[i];
                if (btn.textContent.trim() === {button_text!r} && btn.style.position !== 'fixed') {{
                    btn.style.position = 'fixed';
                    btn.style.top = {top!r};
                    btn.style.right = {right!r};
                    btn.style.zIndex = '1000';
                    btn.style.width = 'auto';
                    btn.style.boxShadow = '0 1px 6px rgba(0,0,0,0.25)';
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
    "pos": "**Pos**: vein does not fully compress (thrombus suspected)",
    "neg": "**Neg**: vein fully compresses (no thrombus)",
    "unsure": "**Unsure**: technical error / cannot assess",
}

OPTION_LABELS = list(REVIEW_OPTIONS.values())
OPTION_KEYS = list(REVIEW_OPTIONS.keys())

# Patient-level decision, made once per case (not per clip) in the floating
# panel alongside the clip list.
PATIENT_OPTIONS = {
    "no_action":    "**No action required**: patient diagnosed correctly",
    "feedback":     "**Action required**: provider requires education on technique",
    "misdiagnosed": "**Action required**: patient was misdiagnosed",
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

    arm = worklist_tag(clinician)
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
    Worksheet title from the clinician's normalized name (max 100 chars for
    Sheets). No arm/tag suffix: WORKLIST_BY_CLINICIAN is a fixed 1:1 mapping
    from name to worklist, so there's no scenario where the same name needs
    to land on two different tabs.
    """
    base = clinician.strip().lower().replace(" ", "_")
    return base[:100]


# ──────────────────────────────────────────────
# Data
# ──────────────────────────────────────────────

# Which pre-built worklist (data/worklist_userN.json) a clinician sees is
# keyed off the name they type at login -- normalized (trimmed, lowercased,
# whitespace-collapsed) so "User 1", "user 1", and "USER 1" all match. One
# deployment serves all clinicians this way; no per-clinician URL needed.
# Each worklist is the same 21 model-selected patients plus that clinician's
# own 21 randomly-selected patients, pre-shuffled together.
WORKLIST_BY_CLINICIAN = {
    "user 1": "worklist_user1.json",
    "user 2": "worklist_user2.json",
    "user 3": "worklist_user3.json",
}


def normalize_clinician_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


LOGIN_STATE_KEYS = (
    "clinician", "clinician_first", "clinician_last", "page", "idx", "reviews",
    "session_start", "first_login", "patient_start_time", "_current_pid",
)
WIDGET_KEY_PREFIXES = ("radio_", "open_", "disclosure_", "patient_radio_", "patient_comments_")


def _clear_session_for_logout():
    """
    Reset session state on logout, so a fresh login (even in the same
    browser tab, without a page reload) re-renders every widget from
    whatever load_existing_reviews() just fetched from the sheet, instead
    of showing this session's leftover per-clip widget values. Streamlit's
    st.session_state persists for the life of the browser connection, not
    per "logical" login -- clearing only LOGIN_STATE_KEYS left every
    radio_/open_/disclosure_/patient_radio_/patient_comments_ key from this
    session sitting untouched, so `if key not in st.session_state` in
    _render_clip_row and the patient-decision radio silently skipped
    re-seeding from the newly-loaded reviews and kept showing stale values
    -- this is why a save immediately followed by a fresh login could look
    like the save never happened even though the sheet was written
    correctly.
    """
    for k in LOGIN_STATE_KEYS:
        st.session_state.pop(k, None)
    for k in [k for k in st.session_state.keys() if k.startswith(WIDGET_KEY_PREFIXES)]:
        st.session_state.pop(k, None)


def worklist_tag(clinician: str) -> str:
    """
    Short tag identifying which worklist file this clinician is assigned,
    e.g. "user1". Recorded in the Google Sheet for the study coordinator
    only -- never surfaced in the app UI.
    """
    key = normalize_clinician_name(clinician)
    worklist_file = WORKLIST_BY_CLINICIAN.get(key)
    if not worklist_file:
        return "unknown"
    return Path(worklist_file).stem.replace("worklist_", "")


@st.cache_data
def load_worklist(clinician: str):
    """
    Loads the worklist assigned to this specific clinician, keyed by their
    (normalized) login name via WORKLIST_BY_CLINICIAN. Before login (or for
    an unrecognized name), returns an empty worklist -- the login screen
    itself validates the name against WORKLIST_BY_CLINICIAN and refuses to
    proceed on an unrecognized one, so "review" page code never actually
    runs against this empty fallback.

    Returns:
        patients_df : DataFrame with one row per patient (summary fields only)
        clips_by_patient : dict[patient_id] -> list of {filename, stream_url, label}
    """
    key = normalize_clinician_name(clinician)
    worklist_file = WORKLIST_BY_CLINICIAN.get(key)
    if not worklist_file:
        return pd.DataFrame(columns=["patient", "total_positive_clips",
                                      "total_negative_clips", "fake_user_interpretation"]), {}

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

st.set_page_config(
    page_title="DVT Case Review", page_icon="🩺", layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    /* Reduce default Streamlit top padding. The header element itself is
       kept (not display:none) and only its visible contents are hidden --
       verified live that display:none on the header breaks the sidebar's
       reopen chevron (shown once the sidebar is collapsed), since that
       control's position is computed from the header's measured height;
       collapsing that height to 0 made the chevron impossible to find or
       click. Hiding the header's children individually keeps the same
       clean look without that side effect. padding-top is 3.5rem (rather
       than the smaller value that would otherwise be enough) so the page's
       first element clears the sidebar's reopen chevron, which floats at a
       fixed spot near the top left at all times -- covered by the sidebar
       itself when it's expanded, but sitting right over that first element
       once the sidebar is collapsed and content shifts left to fill the
       space. This keeps that element full-width instead of indenting it
       sideways to dodge the chevron. */
    .block-container {
        padding-top: 3.5rem !important;
        padding-bottom: 1rem !important;
        padding-left: 2rem !important;
        padding-right: 2rem !important;
    }
    header[data-testid="stHeader"] { background: transparent !important; box-shadow: none !important; }
    div[data-testid="stDecoration"] { display: none !important; }
    div[data-testid="stToolbarActions"] { display: none !important; }
    div[data-testid="stAppDeployButton"] { display: none !important; }
    #MainMenu { visibility: hidden !important; }

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

    /* Align each radio option's bubble with the first line of its label
       text (default is vertically centered against the whole, possibly
       multi-line, label). Two selectors layered for robustness: BaseWeb's
       own component-type attribute (the UI library Streamlit's form
       widgets are built on, not a Streamlit-internal name), plus a
       library-agnostic fallback keyed only on the standard HTML radio
       input every such widget must contain for accessibility, regardless
       of which internal wrapper class names are actually in use. */
    div[data-baseweb="radio"],
    label:has(> input[type="radio"]) {
        align-items: flex-start !important;
    }
    div[data-baseweb="radio"] > div:first-child {
        margin-top: 0.15rem;
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

patients, clips_by_patient = load_worklist(st.session_state.clinician)
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

    # Quote the exact same option strings the widgets use below (not a
    # hand-typed paraphrase), so this instructional text can't drift out of
    # sync with the actual choices again.
    clip_options_md = "\n".join(f"   - {label}" for label in OPTION_LABELS)
    patient_options_md = "\n".join(f"   - {label}" for label in PATIENT_OPTION_LABELS)

    st.markdown("#### How it works")
    st.markdown(
        "1. Enter your name below and select **Start review**.\n"
        "2. For **each clip**, select the option that best describes your "
        "assessment of that clip:\n"
        f"{clip_options_md}\n"
        "3. Once you've reviewed every clip, use the **patient-level panel** on "
        "the right to record your overall decision for the case:\n"
        f"{patient_options_md}\n"
        "4. A case is complete once every clip has an assessment **and** the "
        "patient-level decision is selected. Select **Save** or **Next** to "
        "record your progress and continue."
    )

    st.warning(
        "**Taking a break?** Please take breaks **between patients only**: "
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

    both_filled = bool(first_name.strip()) and bool(last_name.strip())

    # Not disabled=not both_filled: a browser-disabled button blocks the
    # click event entirely, and if that state hasn't caught up yet with what
    # was just typed (text_input only commits on blur/enter, not per
    # keystroke), the first click can land while it's still disabled and get
    # swallowed, requiring a second click once it re-renders as enabled.
    # Keeping it always clickable and validating on click avoids that, at
    # the cost of the button not visually greying out beforehand.
    if not both_filled:
        st.caption("Enter your first and last name to continue.")

    if st.button("Start review", type="primary"):
        fn = first_name.strip()
        ln = last_name.strip()
        if not (fn and ln):
            st.warning("Please enter both your first and last name.")
            st.stop()
        display_name = f"{fn} {ln}"
        if normalize_clinician_name(display_name) not in WORKLIST_BY_CLINICIAN:
            st.error(
                f"'{display_name}' isn't a recognized reviewer name for this study. "
                "Please double-check the spelling, or contact the study coordinator."
            )
            st.stop()
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

    # ── compact case banner ────────────────────
    display_name = pid if len(pid) <= 16 else f"{pid[:16]}…"
    st.markdown(
        f"""
        <div class="case-banner">
            <span class="patient-id">{display_name}</span>
            <span>Case {idx + 1} of {n_patients}</span>
            <span>{total_clips} clip{"s" if total_clips != 1 else ""}</span>
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

    main_col, panel_col = st.columns([2.2, 1.2], gap="small")

    prev_clip_reviews = st.session_state.reviews.get(pid, {}).get("clips", {})
    clip_inputs = []  # collected here, read back by _save_current below

    # st.button, not st.expander -- confirmed (twice) that a plain expander's
    # open/close click doesn't message Python at all here, keyed or not, so
    # there's no way to react to it. A button click IS certain to register
    # exactly once, on the same rerun as the click, which lets the one click
    # that opens a clip also fetch and play it immediately -- no separate
    # "Play" step needed.
    #
    # This whole row is an @st.fragment: clicking a clip's own button or
    # radio reruns only THIS clip's row, not the whole page, so opening one
    # clip (or answering it) never re-renders or re-transmits every other
    # already-open clip's video. Within the fragment, st.rerun(scope=
    # "fragment") is used (not a bare st.rerun(), which would rerun the
    # whole page) so the arrow flips to the correct direction on the very
    # click that toggled it, instead of only catching up on some later,
    # unrelated rerun.
    @st.fragment
    def _render_clip_row(pid, i, clip, n_clips, prev, done, clip_inputs):
        radio_key = f"radio_{pid}_{i}"
        open_key = f"open_{pid}_{i}"
        prev_decision = prev.get("decision", "")
        if radio_key not in st.session_state:
            st.session_state[radio_key] = (
                OPTION_LABELS[OPTION_KEYS.index(prev_decision)] if prev_decision in OPTION_KEYS else None
            )
        if open_key not in st.session_state:
            st.session_state[open_key] = (i == 0)

        # Button and body share one bordered container so the clip's name
        # and its video sit in a single connected box, not two separate
        # boxes with a gap between them.
        with st.container(border=True):
            arrow = "▼" if st.session_state[open_key] else "▶"
            label = f"{arrow}  {done}  Clip {i + 1} of {n_clips} ({clip['filename']})"
            if st.button(label, key=f"disclosure_{pid}_{i}", use_container_width=True):
                st.session_state[open_key] = not st.session_state[open_key]
                st.rerun(scope="fragment")

            if st.session_state[open_key]:
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

        decision_label = st.session_state[radio_key]
        selected_key = OPTION_KEYS[OPTION_LABELS.index(decision_label)] if decision_label else ""
        clip_inputs.append((clip["filename"], selected_key, "", prev.get("reviewed_at", "")))

    with main_col:
        for i, clip in enumerate(clips):
            prev = prev_clip_reviews.get(clip["filename"], {})
            done = "●" if prev.get("decision", "") else "○"
            _render_clip_row(pid, i, clip, len(clips), prev, done, clip_inputs)

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
            "Comments",
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

    # ── log out: pinned to the top-right of the viewport (not the sidebar
    # scroll flow) so it stays reachable however far down the case list or
    # the page the reviewer has scrolled. Top-right specifically avoids the
    # sidebar's own reopen chevron, which floats top-left when collapsed.
    # Saves the current case first (same as Save/Next), so an unsaved edit
    # on the case you're currently viewing isn't silently dropped. ──
    if st.button("Log out", use_container_width=True, key="logout_btn"):
        _save_current()
        _clear_session_for_logout()
        st.rerun()
    components.html(_fixed_corner_js("Log out"), height=0)

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
            _clear_session_for_logout()
            st.rerun()
