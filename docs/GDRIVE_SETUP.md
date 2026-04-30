# Google Drive Setup Guide

To index files from Google Drive, you need to set up OAuth 2.0 credentials with Google Cloud.

## Step 1: Create a Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (click "Select a Project" → "New Project")
3. Name it something like "Magpie"
4. Click "Create"

## Step 2: Enable the Google Drive API

1. In the Cloud Console, search for "Google Drive API"
2. Click on it and press "Enable"
3. Wait for it to be enabled (should be immediate)

## Step 3: Create OAuth 2.0 Credentials

1. Go to **APIs & Services** → **Credentials**
2. Click **+ Create Credentials** → **OAuth client ID**
3. You'll be prompted to create a consent screen first:
   - User Type: Select **External**
   - Click **Create**
4. Fill in the consent screen:
   - App name: "Magpie"
   - User support email: your email
   - Developer contact: your email
   - Click **Save and Continue**
5. Scopes: Click **Add or Remove Scopes**, search for "Google Drive API", select `https://www.googleapis.com/auth/drive` (full read-write access, needed for both indexing and future executor features like moving files to trash), then click **Update**
6. Click **Save and Continue**, then **Back to Dashboard**

## Step 4: Download the Credentials JSON

1. Go back to **Credentials**
2. Click **+ Create Credentials** → **OAuth client ID**
3. Application type: **Desktop app**
4. Click **Create**
5. Click the download icon (or click the client ID name, then **Download JSON**)
6. Save the file as `~/.magpie/gdrive_client_secret.json`:

```bash
mkdir -p ~/.magpie
# Move the downloaded file
mv ~/Downloads/client_secret_*.json ~/.magpie/gdrive_client_secret.json
```

## Step 4.5: Add Yourself as a Test User (if needed)

If you get an "Access blocked" error when running the app, your OAuth app is in Testing mode and you need to add yourself as an authorized test user:

1. In the Cloud Console, go to **APIs & Services** → **OAuth consent screen**
2. Click on **"Audience"** in the left sidebar
3. Scroll down to **"Test users"**
4. Click **"Add users"**
5. Enter your email (the Google account you'll use to authenticate)
6. Click **Save**

Then try running the app again.

## Step 5: Index Your Google Drive

First sync dependencies:

```bash
uv sync
```

Apply the database migration:

```bash
python db/migrate.py
```

Then register and authorize your Google Drive:

```bash
uv run magpie gdrive add --label "My Drive"
```

This will:
- Open a browser to Google's OAuth consent screen
- Ask you to authorize "Magpie" to access your Drive
- Save the access token at `~/.magpie/gdrive_token.json`

Finally, index your files:

```bash
uv run magpie gdrive index "My Drive"
```

Or by drive UUID (shown after `gdrive add`):

```bash
uv run magpie gdrive index <drive-id>
```

## Notes

- **Token storage:** Your access token is cached at `~/.magpie/gdrive_token.json`. It's readable only by you (mode 0600).
- **Refreshing:** The token automatically refreshes when expired on subsequent runs.
- **Multiple drives:** You can register multiple Google Drive accounts by running `gdrive add` multiple times.
- **Google Workspace files:** Native Google Docs, Sheets, and Slides are indexed but have no content hash (stored as `content_hash=NULL`). They can still be labeled and organized.

## Troubleshooting

**"Error: Call to a member function methods() on null"**
- Make sure `~/.magpie/gdrive_client_secret.json` exists and is valid JSON

**"403 Forbidden"**
- The credentials were revoked or the consent was never granted
- Delete `~/.magpie/gdrive_token.json` and run `gdrive add` again

**Rate limited (429 errors)**
- Google Drive API has a limit of ~10 requests/second
- The indexer has exponential backoff built in; just wait and it will retry
