#!/usr/bin/env python3
import os
import sys
import re
import tempfile
import urllib.parse
import requests
from bs4 import BeautifulSoup
import html2text

# Import the modern Google Gen AI SDK
from google import genai
from google.genai import types

# Restored legacy Google API & OAuth imports for Google Drive API interaction
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.http import MediaFileUpload

# Scopes required to upload files the script creates
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

CONFIG_DIR = os.path.expanduser("~/.config/web2drive")
CREDENTIALS_PATH = os.path.join(CONFIG_DIR, "credentials.json")
TOKEN_PATH = os.path.join(CONFIG_DIR, "token.json")


def print_help():
    """Prints usage documentation for the CLI utility."""
    help_text = """Web2Drive Utility
-----------------
Fetches the content of a URL, cleans up non-essential structural elements,
and uploads the parsed Markdown file directly to a dedicated 'Web2Drive' 
folder in your Google Drive.

Usage:
  web2drive <URL>
  web2drive --help | -h

Options:
  -h, --help    Show this usage documentation.
"""
    print(help_text)


def get_gdrive_service():
    """Authenticates the user and returns the Drive API service."""
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_PATH):
                print(
                    f"Error: Missing credentials file at {CREDENTIALS_PATH}",
                    file=sys.stderr,
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)

        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(TOKEN_PATH, "w") as token:
            token.write(creds.to_json())

    # Note: google-api-python-client is still used for Google Drive API.
    # The unified google-genai library focuses on model inference.
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=creds)


def clean_filename(title):
    """Sanitizes text to make it a safe, readable filename."""
    safe = re.sub(r'[\\/*?:"<>|]', "", title)
    safe = re.sub(r"\s+", " ", safe).strip()
    return safe[:60]


def get_fallback_filename(url):
    """Extracts a filename fallback from the URL path segments."""
    parsed_url = urllib.parse.urlparse(url)
    path_segments = [seg for seg in parsed_url.path.split("/") if seg]
    if path_segments:
        last_seg = path_segments[-1]
        name_root, _ = os.path.splitext(last_seg)
        if name_root.lower() == "readme" and len(path_segments) > 1:
            parent_seg = path_segments[-2]
            name_root = f"{parent_seg}_{name_root}"
        return clean_filename(name_root)
    return "webpage_content"


def generate_llm_filename(url, markdown_content):
    """Uses the modern google-genai SDK to generate a highly descriptive, clean filename."""
    api_key = os.environ.get("WEB2DRIVE_GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        # Initialize client with modern SDK structure
        client = genai.Client(api_key=api_key)

        # Truncate content to 6000 characters to keep context window small and fast
        content_sample = markdown_content[:6000]

        prompt = (
            "You are a file-naming utility. Your job is to analyze a source URL and a "
            "sample of its parsed markdown content to generate a clean, highly descriptive, "
            "and concise filename.\n\n"
            "Requirements:\n"
            "- The filename must end with the '.md' extension.\n"
            "- Use only letters, numbers, underscores, and hyphens. Avoid spaces or other special characters.\n"
            "- It must be concise and descriptive (typically 3 to 6 words).\n"
            "- Do not exceed 60 characters in total.\n"
            "- Output ONLY the final filename. Do not include quotes, markdown formatting, code blocks, or explanations.\n\n"
            f"URL: {url}\n\n"
            f"Content Sample:\n{content_sample}"
        )

        # Invoke modern client generation on gemini-3.1-flash-lite
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite", contents=prompt
        )

        # Safe extraction of text output to satisfy static type checkers
        raw_text = response.text
        if not raw_text:
            return None

        filename = raw_text.strip()

        # Clean up any unexpected markdown formatting returned by the LLM
        filename = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", filename).strip()
        filename = filename.replace('"', "").replace("'", "")

        if not filename.lower().endswith(".md"):
            filename += ".md"

        # Basic sanitization fallback to guarantee file safety
        filename = re.sub(r'[\\/*?:"<>|\s]', "_", filename)

        return filename
    except Exception as e:
        print(
            f"⚠️ LLM naming failed: {e}. Falling back to default naming.",
            file=sys.stderr,
        )
        return None


def fetch_and_convert(url):
    """Fetches the target URL, detects content-type, processes text, and returns metadata + body."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
    except Exception as e:
        print(f"Error fetching the URL: {e}", file=sys.stderr)
        sys.exit(1)

    content_type = response.headers.get("Content-Type", "").lower()

    is_markdown = "text/markdown" in content_type or url.lower().endswith(
        (".md", ".markdown")
    )
    is_plain_text = "text/plain" in content_type or url.lower().endswith(
        (".txt", ".text")
    )

    if is_markdown or is_plain_text:
        text_content = response.text
        filename = f"{get_fallback_filename(url)}.md"

        h1_match = re.search(r"^#\s+(.+)$", text_content, re.MULTILINE)
        if h1_match:
            title = h1_match.group(1).strip()
            filename = f"{clean_filename(title)}.md"

        markdown_text = f"**Source:** {url}\n\n---\n\n{text_content}"
        return filename, markdown_text

    # HTML flow
    soup = BeautifulSoup(response.text, "html.parser")

    title = ""
    title_tag = soup.find("title")
    if title_tag and title_tag.get_text().strip():
        title = title_tag.get_text().strip()
    else:
        h1_tag = soup.find("h1")
        if h1_tag and h1_tag.get_text().strip():
            title = h1_tag.get_text().strip()

    if title:
        filename = f"{clean_filename(title)}.md"
    else:
        filename = f"{get_fallback_filename(url)}.md"

    for element in soup(
        ["script", "style", "nav", "footer", "header", "noscript", "iframe"]
    ):
        element.decompose()

    body_content = soup.find("article") or soup.find("main") or soup.body or soup

    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = False
    h.ignore_emphasis = False
    h.body_width = 0

    markdown_text = h.handle(str(body_content))
    markdown_text = re.sub(r"\n{3,}", "\n\n", markdown_text)

    markdown_text = f"**Source:** {url}\n\n---\n\n" + markdown_text
    return filename, markdown_text


def get_or_create_folder(service, folder_name="Web2Drive"):
    """Finds or creates a dedicated directory inside Google Drive."""
    query = f"mimeType = 'application/vnd.google-apps.folder' and name = '{folder_name}' and trashed = false"
    try:
        results = service.files().list(q=query, fields="files(id, name)").execute()
        files = results.get("files", [])

        if files:
            return files[0]["id"]

        print(f"⏳ Creating dedicated folder '{folder_name}' in Google Drive...")
        folder_metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        folder = service.files().create(body=folder_metadata, fields="id").execute()
        return folder["id"]
    except Exception as e:
        print(f"Error resolving Google Drive folder: {e}", file=sys.stderr)
        sys.exit(1)


def upload_to_drive(local_file_path, drive_filename):
    """Uploads local file to the dedicated Web2Drive folder on Google Drive."""
    service = get_gdrive_service()
    folder_id = get_or_create_folder(service, "Web2Drive")

    file_metadata = {
        "name": drive_filename,
        "mimeType": "text/markdown",
        "parents": [folder_id],
    }

    media = MediaFileUpload(local_file_path, mimetype="text/markdown", resumable=True)

    try:
        file = (
            service.files()
            .create(body=file_metadata, media_body=media, fields="id, name")
            .execute()
        )
        print(f"✅ Successfully uploaded '{file.get('name')}' to Google Drive.")
        print(f"File ID: {file.get('id')}")
    except Exception as e:
        print(f"Error uploading to Google Drive: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print_help()
        sys.exit(0)

    url = sys.argv[1]
    print(f"⏳ Extracting content from: {url}...")
    filename, markdown_content = fetch_and_convert(url)

    # Attempt to improve the filename using Gemini with the modern SDK
    llm_filename = generate_llm_filename(url, markdown_content)
    if llm_filename:
        filename = llm_filename

    temp_file = tempfile.NamedTemporaryFile(
        mode="w+", suffix=".md", delete=False, encoding="utf-8"
    )
    try:
        temp_file.write(markdown_content)
        temp_file.close()

        print(f"⏳ Uploading to Google Drive as '{filename}'...")
        upload_to_drive(temp_file.name, filename)
    finally:
        if os.path.exists(temp_file.name):
            try:
                os.remove(temp_file.name)
            except Exception as e:
                print(
                    f"Warning: Could not remove temporary file {temp_file.name}: {e}",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    main()
