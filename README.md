# DVT Case Review Portal

A Streamlit web app for clinician QA review of DVT-positive cases. Clinicians access it via a URL — no install or server access needed. Results are saved to a Google Sheet you own.

---

## Setup (one-time, ~15 minutes)

### Step 1 — Create a Google Cloud service account

This lets the app write to your Google Sheet without requiring each clinician to log in.

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or use an existing one)
3. Enable the **Google Sheets API** and **Google Drive API**:
   - Navigate to **APIs & Services → Library**
   - Search for "Google Sheets API" → click **Enable**
   - Search for "Google Drive API" → click **Enable**
4. Create a service account:
   - Go to **APIs & Services → Credentials**
   - Click **Create Credentials → Service account**
   - Name it something like `dvt-review-bot`
   - Click **Done** (no extra permissions needed)
5. Create a key:
   - Click on the service account you just created
   - Go to the **Keys** tab
   - Click **Add Key → Create new key → JSON**
   - Download the JSON file — you'll need it in Step 3

### Step 2 — Create the Google Sheet

1. Create a new Google Sheet (any name, e.g. "DVT Review Results")
2. **Share** the sheet with the service account email
   (the `client_email` from your JSON key file, looks like `xxx@your-project.iam.gserviceaccount.com`).
   Give it **Editor** access.
3. Copy the **spreadsheet ID** from the URL:
   ```
   https://docs.google.com/spreadsheets/d/THIS_PART_IS_THE_ID/edit
   ```

### Step 3 — Deploy to Streamlit Community Cloud

1. Push this repository to GitHub (the setup script handles this — see below)
2. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub
3. Click **New app** and select:
   - **Repository:** `your-username/pocus-streamlit`
   - **Branch:** main
   - **Main file path:** `dvt_review_app.py`
4. Before deploying, click **Advanced settings → Secrets** and paste the
   contents of `.streamlit/secrets.toml.template`, replacing all placeholders
   with your actual values from the JSON key file:
   - `spreadsheet_id` = the ID from Step 2
   - Each field under `[gcp_service_account]` = matching field from the JSON key
5. Click **Deploy**

You'll get a public URL like `https://your-app.streamlit.app` that you can
share with your clinicians.

---

## How it works

| Who | What |
|---|---|
| **Clinician** | Opens the URL → enters their name → reviews one patient at a time → selects option a / b / c → optional comments for action items → clicks Save / Next |
| **You** | Open your Google Sheet at any time to monitor progress. Each clinician gets their own tab. |

### Review options

- **(a) No action** — exam and interpretation both correct
- **(b) Action required** — exam technique needs provider education
- **(c) Action required** — clips misinterpreted; patient callback needed

### Features

- Progress bar + sidebar jump-to-case list
- Auto-restores previous progress when a clinician logs back in
- Results auto-save on Save and Next clicks
- Summary table at the end with color-coded decisions

---

## Updating patient data

Replace `patients_with_dvt.csv` and re-deploy (Streamlit Cloud re-deploys
automatically on push to GitHub).

---

## Local testing

```bash
cd /home/ads4015/pocus_streamlit

# Install dependencies
pip install -r requirements.txt

# Create .streamlit/secrets.toml from the template and fill in your values
cp .streamlit/secrets.toml.template .streamlit/secrets.toml
# Edit .streamlit/secrets.toml with your actual values

# Run locally
streamlit run dvt_review_app.py
```
