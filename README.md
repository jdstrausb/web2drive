# Web2Drive

Web2Drive is a terminal utility that fetches web pages (HTML, raw Markdown, or plain text), parses and formats the essential content into clean Markdown, and uploads the resulting document directly to a dedicated `Web2Drive` folder in your Google Drive account.

This utility is designed for rapid delivery of clean context files (like reference articles or READMEs) directly into a web interface using its "Add from Drive" capability, without cluttering your local machine.

## Features

- **Content extraction**: Uses `trafilatura`'s advanced semantic extraction algorithms to isolate core page content and discard layout noise (such as navigation bars, sidebars, and footers).
- **Client-Side SPA Support**: Automatically falls back to a headless browser running `Playwright` if standard retrieval processes return empty pages, loading indicators, or JavaScript requirements.
- **Session Authentication**: Supports importing active browser sessions to parse articles behind personal accounts, subscription models, or basic paywalls.
- **Overlay & Cookie Banner Removal**: Injects structural cleanup routines during headless rendering to strip cookie consent banners and intrusive modal overlays.
- **LLM-assisted naming**: Uses `gemini-3.1-flash-lite` to analyze the URL and document content to generate a clean, highly descriptive, and concise filename.
- **Zero local clutter**: Runs completely via temporary system files that are automatically deleted immediately after a successful upload.

## Prerequisites

1. **Python 3.10+**
2. **Google Cloud Project**: A project with the **Google Drive API** enabled and a Desktop OAuth Client credential (`credentials.json`) saved to `~/.config/web2drive/credentials.json`.
3. **Gemini API Key**: A valid Google AI Studio API key.

## Installation

1. Clone or copy this repository to your preferred local script directory:

   ```bash
   cd ~/Documents/scripts/python/web2drive
   ```

2. Setup the isolated Python virtual environment and install the required dependencies:

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. Initialize Playwright system dependencies:

   ```bash
   playwright install chromium
   ```

4. Ensure your Google OAuth credentials are saved in the default configuration path:
   ```bash
   ~/.config/web2drive/credentials.json
   ```

## Session Authentication & Cookie Bypass

To parse paywalled content, subscription-only portals, or bypass restrictive cookie-consent walls, you can provide active session cookies to Web2Drive. The utility will automatically apply these credentials to both static HTTP requests and dynamic Playwright sessions.

### End-to-End Setup Guide

1. **Install a Cookie Export Extension**:
   Install a web browser extension capable of exporting cookies in JSON format.
   - Recommended Chrome/Firefox extensions: _EditThisCookie_ or _Cookie-Editor_.

2. **Log In to the Target Site**:
   Navigate to the website in your browser (e.g., a subscription service or a site requiring a login) and ensure you are logged in with an active session.

3. **Export Cookies to JSON**:
   - Click your cookie extension icon.
   - Select the **Export** option.
   - Ensure the export format is configured as **JSON**.

4. **Save to the Configuration Path**:
   Create or open your local `cookies.json` configuration file:

   ```bash
   mkdir -p ~/.config/web2drive
   nano ~/.config/web2drive/cookies.json
   ```

   Paste the exported JSON data directly into this file and save it. The format should structurally look like a list of browser cookies:

   ```json
   [
     {
       "domain": ".example.com",
       "expirationDate": 1799999999,
       "name": "session_token",
       "path": "/",
       "value": "your-active-session-value-here"
     }
   ]
   ```

5. **Run the Scraper**:
   Execute the `web2drive` command as normal. The script will automatically discover the file, log a message acknowledging the cookie injection (`🔑 Applying authentication cookies...`), and use your authenticated session to fetch the page.

## Terminal Integration

To run `web2drive` natively from anywhere in your terminal, add the following shell function to your `~/.zshrc` (or standard `~/.bashrc`):

```bash
# Extract web page and upload to Google Drive as Markdown
web2drive() {
  if [ -z "$1" ]
  then
    echo "❌ Error: Please provide a URL."
    echo "Usage: web2drive <url>"
    return 1
  fi

  # Call the utility using the virtual environment's isolated Python
  ~/Documents/scripts/python/web2drive/venv/bin/python ~/Documents/scripts/python/web2drive/web2drive.py "$1"
}
```

Also, expose your Gemini API key in your shell configuration profile (e.g., `~/.zshrc` or `~/.zshenv`):

```bash
export WEB2DRIVE_GEMINI_API_KEY="your_api_key_here"
```

Apply the changes to your active terminal session:

```bash
source ~/.zshrc
```

## Usage

Run the utility followed by the URL of the webpage or document you want to send to Google Drive:

```bash
# Uploading an HTML documentation page
web2drive "https://example.com/some-article-to-ingest"

# Uploading a raw Markdown file from GitHub
web2drive "https://raw.githubusercontent.com/googleapis/python-genai/refs/heads/main/README.md"
```

### Initial Run Authentication

On your first execution, the script will open your system's default browser window to authorize access to your Google Drive space. The authorization token will be safely stored in `~/.config/web2drive/token.json` so you do not need to repeat this step on future runs.

## License

This project is licensed under the MIT License.
