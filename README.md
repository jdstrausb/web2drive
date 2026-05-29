# Web2Drive

Web2Drive is a terminal utility that fetches web pages (HTML, raw Markdown, or plain text), parses and formats the essential content into clean Markdown, and uploads the resulting document directly to a dedicated `Web2Drive` folder in your Google Drive account.

This utility is designed for rapid delivery of clean context files (like reference articles or READMEs) directly into a web interface using its "Add from Drive" capability, without cluttering your local machine.

## Features

- **Content extraction**: Automatically parses HTML to strip boilerplate elements (like navigation headers, scripts, styles, and footers), returning only the core content.
- **Format-aware parsing**: Automatically detects raw Markdown or plain text URLs (like raw GitHub sources) and passes them through without double-parsing.
- **LLM-assisted naming**: Uses `gemini-3.1-flash-lite` to analyze the URL and document content to generate a clean, highly descriptive, and concise filename.
- **Google Drive isolation**: Organizes documents inside a dedicated `Web2Drive` directory in Google Drive.
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

2. Setup the isolated Python virtual environment and install the required dependencies using `uv` (or standard `pip`):

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   uv pip install -r requirements.txt
   ```

3. Ensure your Google OAuth credentials are saved in the default configuration path:
   ```bash
   ~/.config/web2drive/credentials.json
   ```

## macOS Terminal Integration

To run `web2drive` natively from anywhere in your terminal, add the following shell function to your `~/.foorc` file (`~/.zshrc`, `~/.bashrc`):

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

On your first execution, the script will open your system's default browser window to authorize access to your Google Drive space. The authorization token will be safely stored in `~/.config/web2drive/token.json` so you don't need to repeat this step on future runs.

## License

This project is licensed under the MIT License.
