# Web2Drive

Web2Drive is a terminal utility that fetches web pages (HTML, raw Markdown, or plain text), parses and formats the essential content into clean Markdown, and uploads the resulting document directly to a dedicated `Web2Drive` folder in your Google Drive account.

It's built for one workflow in particular: getting clean reference material — articles, documentation, READMEs — into an LLM chat assistant that offers a Google Drive file picker (an "Add from Drive" button), without saving anything to your local machine first.

## Features

- **Content extraction**: Uses `trafilatura`'s semantic extraction algorithms to isolate core page content and discard layout noise (navigation bars, sidebars, footers), with a BeautifulSoup + `html2text` fallback when extraction comes up empty.
- **Client-side SPA support**: Automatically falls back to a headless `Playwright` browser when standard retrieval returns empty pages, loading indicators, or content gated behind JavaScript.
- **Session authentication**: Supports importing active browser session cookies to parse articles behind personal accounts, subscriptions, or basic paywalls.
- **Clipboard source fallback**: Parses and uploads content directly from the system clipboard — useful for private notes, local editor selections, or text copied from PDFs.
- **Batch processing**: Sequentially parses a list of URLs from a local `.txt` file, uploading them one by one with pacing guards to avoid rate limits.
- **Overlay & cookie-banner removal**: Injects structural cleanup routines during headless rendering to strip cookie-consent banners and intrusive modal overlays.
- **LLM-assisted naming & metadata** _(optional)_: When a Gemini API key is configured, uses `gemini-3.1-flash-lite` to derive a descriptive filename plus a metadata header (a three-sentence executive summary, estimated reading time, and tags) prepended to the document. Without a key, the tool falls back to deterministic filenames derived from the page title or URL.
- **Zero local clutter**: Works through temporary system files that are automatically deleted after each run.

## Prerequisites

1. **Python 3.10+**
2. **Google Cloud project**: A project with the **Google Drive API** enabled and a Desktop OAuth Client credential (`credentials.json`), saved to `~/.config/web2drive/credentials.json`.
3. **Gemini API key** _(optional)_: A Google AI Studio API key. This is **not required** — without it the tool still fetches, cleans, and uploads, but skips LLM-generated filenames and the metadata header. See the optional step in [Installation](#installation).

## Installation

1. Clone (or copy) this repository into a directory of your choice. Remember the path you pick — the [Terminal Integration](#terminal-integration) step below references it, and the two must match.

   ```bash
   git clone <repo-url> ~/Documents/scripts/python/web2drive
   cd ~/Documents/scripts/python/web2drive
   ```

2. Create an isolated virtual environment and install the dependencies:

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. Install the Playwright browser used for the dynamic-content fallback:

   ```bash
   playwright install chromium
   ```

4. Place your Google OAuth credentials at the default configuration path:

   ```bash
   mkdir -p ~/.config/web2drive
   cp /path/to/downloaded/credentials.json ~/.config/web2drive/credentials.json
   ```

5. **(Optional) Configure a Gemini API key.** To enable LLM-generated filenames and the metadata header, export your key as `WEB2DRIVE_GEMINI_API_KEY`:

   ```bash
   export WEB2DRIVE_GEMINI_API_KEY="your_api_key_here"
   ```

   For persistence, add that line to your shell profile (`~/.zshrc` or `~/.zshenv`) — the same file you'll use for the shell function below — and reload it with `source ~/.zshrc`. Skip this step entirely to run without LLM features.

## Initial Run Authentication

On your first execution, the script opens your system's default browser to authorize access to your Google Drive. The authorization token is stored at `~/.config/web2drive/token.json`, so you won't need to repeat this step on later runs.

## Session Authentication & Cookie Bypass

To parse paywalled content, subscription-only portals, or sites behind restrictive cookie-consent walls, you can provide active session cookies. The utility applies these to both static HTTP requests and dynamic Playwright sessions.

### End-to-End Setup Guide

1. **Install a cookie-export extension**:
   Install a browser extension capable of exporting cookies in JSON format.
   - Recommended Chrome/Firefox extensions: _EditThisCookie_ or _Cookie-Editor_.

2. **Log in to the target site**:
   Navigate to the website in your browser and ensure you have an active, logged-in session.

3. **Export cookies to JSON**:
   - Click your cookie extension icon.
   - Select the **Export** option.
   - Ensure the export format is set to **JSON**.

4. **Save to the configuration path**:
   Create or open your local `cookies.json` configuration file:

   ```bash
   mkdir -p ~/.config/web2drive
   nano ~/.config/web2drive/cookies.json
   ```

   Paste the exported JSON directly into this file and save. The format should structurally look like a list of browser cookies:

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

5. **Run the scraper**:
   Execute the `web2drive` command as normal. The script discovers the file automatically, logs a message acknowledging the injection (`🔑 Applying authentication cookies to static request...`), and uses your authenticated session to fetch the page.

## Terminal Integration

To run `web2drive` from anywhere in your terminal, add the following shell function to your `~/.zshrc` (or `~/.bashrc`).

> **Note:** We use `"$@"` instead of `"$1"` so that all shell arguments (such as files or flags) are forwarded to the tool.

```bash
# Extract web page, clipboard, or batch file, and upload to Google Drive
web2drive() {
  if [ -z "$1" ]
  then
    echo "❌ Error: Please provide a URL, clipboard flag, or batch file."
    echo "Usage:"
    echo "  web2drive <url>"
    echo "  web2drive --clip"
    echo "  web2drive --batch <path_to_urls.txt>"
    return 1
  fi

  # Call the utility using the virtual environment's isolated Python, forwarding all arguments
  ~/Documents/scripts/python/web2drive/venv/bin/python ~/Documents/scripts/python/web2drive/web2drive.py "$@"
}
```

> The two hardcoded paths above must match the directory you chose in [Installation](#installation) step 1. If you configured a Gemini API key, keep its `export` line in this same file.

Apply the changes to your active terminal session:

```bash
source ~/.zshrc
```

---

## Usage

### 1. Extracting standard URLs

Run the utility followed by the URL of the webpage or document you want to send to Google Drive:

```bash
# Uploading an HTML documentation page
web2drive "https://example.com/some-article-to-ingest"

# Uploading a raw Markdown file from GitHub
web2drive "https://raw.githubusercontent.com/googleapis/python-genai/refs/heads/main/README.md"
```

### 2. Clipboard Fallback

To upload text or code currently on your system clipboard, use the `--clip` or `-c` flag. This is useful for paywalled articles, local editor snippets, Slack discussions, or text copied from PDFs.

```bash
# Processes current system clipboard content
web2drive --clip
# or
web2drive -c
```

The tool reads the clipboard via native system subprocess calls, automatically detects whether the content is structured HTML or plain text/Markdown, and processes it accordingly before submitting it for metadata generation.

### 3. Batch URL Processing

To process several links sequentially, list the URLs in a plain `.txt` file (one per line). Empty lines and comment lines starting with `#` are ignored.

```bash
# Run batch processing explicitly
web2drive --batch ~/Documents/reading_list.txt
# or
web2drive -b ~/Documents/reading_list.txt
```

#### Implicit Auto-Detection

For convenience, you can omit the flag entirely; if the target argument points to a local file ending in `.txt`, batch mode triggers automatically:

```bash
web2drive ~/Documents/reading_list.txt
```

#### Example Batch File (`reading_list.txt`)

```text
# Articles on LLM architectures
https://example.com/transformers-explained
https://example.com/rlhf-overview

# Technical documentation links
https://example.com/api-reference
```

A single unreachable or failed URL is reported and skipped; the batch continues, and a summary of successful uploads is printed at the end.

## License

This project is licensed under the MIT License.
