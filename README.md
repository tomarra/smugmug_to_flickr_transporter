# SmugMug → Flickr Transporter Tool

Migrates all your photos and albums from SmugMug to Flickr, preserving titles, descriptions, tags, and album structure.

---

## Prerequisites

- Python 3.8 or newer
- A SmugMug account with API access
- A Flickr account with API access

---

## Step 1 — Install dependencies

```bash
pip install -r requirements.txt
```

---

## Step 2 — Get a SmugMug API key

1. Go to https://api.smugmug.com/api/developer/apply
2. Apply for an API key (select "Browser" as application type)
3. Once approved, note your **API Key** and **API Secret**

---

## Step 3 — Authorize the script with your SmugMug account

Run the authorization helper:

```bash
SMUGMUG_API_KEY=your_key SMUGMUG_API_SECRET=your_secret python migrate.py --auth-smugmug
```

Visit the URL it prints, approve access, enter the PIN, and copy the two tokens it prints.

---

## Step 4 — Get a Flickr API key

1. Go to https://www.flickr.com/services/apps/create/apply/
2. Apply for a **non-commercial** key
3. Note your **API Key** and **Secret**

---

## Step 5 — Set environment variables

Set all six values before running the script. On macOS/Linux:

```bash
export SMUGMUG_API_KEY="..."
export SMUGMUG_API_SECRET="..."
export SMUGMUG_ACCESS_TOKEN="..."    # from Step 3
export SMUGMUG_ACCESS_SECRET="..."  # from Step 3
export SMUGMUG_NICKNAME="..."        # your SmugMug username (shown in your profile URL)
export FLICKR_API_KEY="..."
export FLICKR_API_SECRET="..."
```

On Windows (Command Prompt):

```cmd
set SMUGMUG_API_KEY=...
set SMUGMUG_API_SECRET=...
... (repeat for each variable)
```

Alternatively, you can paste the values directly into the `# CONFIGURATION` section at the top of `migrate.py`.

---

## Step 6 — Run the migration

```bash
python migrate.py
```

The script will:
1. Ask you to authorize Flickr in your browser (first run only)
2. Fetch all your SmugMug albums
3. For each album: download every photo, upload it to Flickr, and create a matching photoset

Progress is saved to `migration_progress.json` after every photo. If the script is interrupted, just run it again — it will skip everything already done.

Detailed logs are written to `migration.log`.

---

## Privacy note

Photos are uploaded to Flickr as **public** by default. To make them private instead, find the line:

```python
is_public=1,
```

in `migrate.py` and set it to `0`.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| "Rate limited" warnings | The script auto-waits and retries. Increase `REQUEST_DELAY` at the top of `migrate.py` if it keeps happening |
| "No download URL" errors | The photo may be a video (not supported) or have download disabled in SmugMug settings |
| Flickr upload fails | Check your Flickr storage quota — free accounts have 1,000 photo limits |
| Script crashes midway | Just re-run it — progress is saved and it will resume |

---

## What is preserved

| Data | Preserved? |
|---|---|
| Original resolution photos | ✅ Yes |
| Photo titles | ✅ Yes |
| Descriptions/captions | ✅ Yes |
| Keywords/tags | ✅ Yes |
| Album names and structure | ✅ Yes |
| Album descriptions | ✅ Yes |
| EXIF/metadata (embedded) | ✅ Yes (embedded in file) |
| Dates (upload date) | ⚠️ Set to migration date on Flickr |
| Comments | ❌ Not supported by Flickr API |
| Videos | ❌ Not currently supported |
