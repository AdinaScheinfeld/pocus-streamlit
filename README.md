# DVT Case Review Portal

A Streamlit web app for clinician QA review of POCUS cases. Clinicians access it via a URL, so no install or server access needed. Results are saved to a Google Sheet.

---

## How it works

| Who | What |
|---|---|
| **Clinician** | Opens the URL → enters their name → reviews one patient at a time → selects option a / b / c → optional comments for action items → clicks Save / Next |
| **Organizer** | Open your Google Sheet at any time to monitor progress. Each clinician gets their own tab. |

### Review options

- **(a) No action:** exam and interpretation both correct
- **(b) Action required:** exam technique needs provider education
- **(c) Action required:** clips misinterpreted; patient callback needed

### Features

- Progress bar + sidebar jump-to-case list
- Auto-restores previous progress when a clinician logs back in
- Results auto-save on Save and Next clicks
- Summary table at the end with color-coded decisions

