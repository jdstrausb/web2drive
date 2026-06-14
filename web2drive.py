#!/usr/bin/env python3
import os
import sys
import re
import json
import tempfile
import subprocess
import time
import requests

from pydantic import BaseModel, Field
from google import genai
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Import from your dedicated standalone package layout
import html_content_extractor as extractor

# --- Configuration paths -----------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
CONFIG_DIR = os.path.expanduser("~/.config/web2drive")
CREDENTIALS_PATH = os.path.join(CONFIG_DIR, "credentials.json")
TOKEN_PATH = os.path.join(CONFIG_DIR, "token.json")
COOKIES_PATH = os.path.join(CONFIG_DIR, "cookies.json")

DRIVE_FOLDER_NAME = "Web2Drive"
GEMINI_MODEL = "gemini-3.1-flash-lite"
LLM_TEMPERATURE = 0.2
LLM_CONTENT_SAMPLE_CHARS = 8000
BATCH_SLEEP_SECONDS = 2
SOURCE_HEADER_TEMPLATE = "**Source:** {url}\n\n---\n\n"


def format_source_header(url: str) -> str:
    return SOURCE_HEADER_TEMPLATE.format(url=url)


class Web2DriveError(Exception):
    """Raised for expected, user-facing failures in the processing pipeline."""


class PageMetadata(BaseModel):
    filename: str = Field(
        description="A clean, highly descriptive, and concise filename ending in '.md'. "
        "Use only letters, numbers, underscores, and hyphens (no spaces). "
        "Do not exceed 60 characters in total."
    )
    summary: str = Field(
        description="A concise, 3-sentence executive summary of the core content."
    )
    tags: list[str] = Field(
        description="A list of 3 to 5 highly relevant keywords or category tags."
    )
    reading_time: int = Field(
        description="Estimated reading time in minutes based on document length and complexity."
    )


def print_help() -> None:
    help_text = """Web2Drive Utility
-----------------
Fetches URL content, cleans the context structure, and uploads Markdown documents to Google Drive.

Usage:
  web2drive <URL>
  web2drive --clip | -c
  web2drive --batch | -b <path_to_file.txt>
  web2drive --help | -h
"""
    print(help_text)


def get_gdrive_service():
    creds = None
    if os.path.exists(TOKEN_PATH):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        except Exception as e:
            print(
                f"⚠️ Warning: Could not read cached token: {e}. Re-authenticating...",
                file=sys.stderr,
            )
            creds = None

    if creds and not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                print(
                    f"⏳ Cached token expired or revoked ({e}). Re-authenticating...",
                    file=sys.stderr,
                )
                creds = None

    if not creds:
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

    return build("drive", "v3", credentials=creds)


def get_clipboard_content() -> str | None:
    clipboard_commands = [
        ["pbpaste"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "-b", "-o"],
        ["wl-paste"],
    ]
    for command in clipboard_commands:
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            return result.stdout
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    print("❌ Error: No supported clipboard utility found.", file=sys.stderr)
    return None


def is_html_string(text: str) -> bool:
    cleaned_start = text.strip()[:150].lower()
    if cleaned_start.startswith("<!doctype") or cleaned_start.startswith("<html"):
        return True
    tags = [
        "<div",
        "<p>",
        "<p ",
        "<span",
        "<section",
        "<h1",
        "<h2",
        "<h3",
        "</a>",
        "</div>",
    ]
    text_lower = text.lower()
    return sum(text_lower.count(tag) for tag in tags) >= 3


def generate_metadata_and_filename(
    url: str, markdown_content: str
) -> tuple[str | None, str | None]:
    api_key = os.environ.get("WEB2DRIVE_GEMINI_API_KEY")
    if not api_key:
        return None, None

    try:
        client = genai.Client(api_key=api_key)
        content_sample = markdown_content[:LLM_CONTENT_SAMPLE_CHARS]
        prompt = (
            "You are an expert document archivist. Analyze the following URL and content sample "
            "to extract a structured metadata profile and determine a clean, professional filename."
        )
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[prompt, f"URL: {url}", f"Content Sample:\n{content_sample}"],
            config={
                "response_mime_type": "application/json",
                "response_json_schema": PageMetadata.model_json_schema(),
                "temperature": LLM_TEMPERATURE,
            },
        )
        if not response.text:
            return None, None

        data = json.loads(response.text)
        tags_line = ", ".join(
            f"`#{tag.strip().replace(' ', '')}`" for tag in data.get("tags", [])
        )

        metadata_header = (
            "---\n"
            f"**Source:** {url}\n"
            f"**Estimated Reading Time:** {data.get('reading_time', 1)} mins\n"
            f"**Tags:** {tags_line}\n"
            "---\n\n"
            "### Executive Summary\n"
            f"{data.get('summary', '')}\n\n"
            "---\n\n"
        )
        filename = data.get("filename", "")
        if not filename.lower().endswith(".md"):
            filename += ".md"
        return re.sub(r'[\\/*?:"<>|\s]', "_", filename), metadata_header
    except Exception as e:
        print(
            f"⚠️ LLM metadata generation failed: {e}. Falling back to default naming.",
            file=sys.stderr,
        )
        return None, None


def load_session_cookies() -> list | None:
    if not os.path.exists(COOKIES_PATH):
        return None
    try:
        with open(COOKIES_PATH, "r") as f:
            cookies = json.load(f)
            return cookies if isinstance(cookies, list) else None
    except Exception as e:
        print(f"⚠️ Warning: Could not parse {COOKIES_PATH}: {e}", file=sys.stderr)
    return None


def fetch_and_convert(url: str) -> tuple[str, str]:
    """Delegates content fetching and conversion down to the core package engine."""
    cookies = load_session_cookies()
    try:
        return extractor.extract_from_url(url, cookies=cookies)
    except Exception as e:
        raise Web2DriveError(f"Extraction Pipeline Failure: {e}") from e


def get_or_create_folder(service, folder_name: str = DRIVE_FOLDER_NAME) -> str:
    query = f"mimeType = 'application/vnd.google-apps.folder' and name = '{folder_name}' and trashed = false"
    try:
        results = service.files().list(q=query, fields="files(id, name)").execute()
        files = results.get("files", [])
        if files:
            return files[0]["id"]

        folder_metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        folder = service.files().create(body=folder_metadata, fields="id").execute()
        return folder["id"]
    except Exception as e:
        raise Web2DriveError(f"Error resolving Google Drive folder: {e}") from e


def upload_to_drive(local_file_path: str, drive_filename: str) -> None:
    service = get_gdrive_service()
    folder_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)
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
        print(
            f"✅ Successfully uploaded '{file.get('name')}' to Google Drive.\nFile ID: {file.get('id')}"
        )
    except Exception as e:
        raise Web2DriveError(f"Error uploading to Google Drive: {e}") from e


def write_and_upload(document: str, filename: str) -> None:
    temp_file = tempfile.NamedTemporaryFile(
        mode="w+", suffix=".md", delete=False, encoding="utf-8"
    )
    try:
        temp_file.write(document)
        temp_file.close()
        upload_to_drive(temp_file.name, filename)
    finally:
        if os.path.exists(temp_file.name):
            os.remove(temp_file.name)


def build_and_upload(url: str, fallback_filename: str, body: str) -> None:
    llm_filename, metadata_header = generate_metadata_and_filename(url, body)
    if llm_filename and metadata_header:
        filename = llm_filename
        document = metadata_header + body
    else:
        filename = fallback_filename
        document = format_source_header(url) + body
    write_and_upload(document, filename)


def process_single_url(url: str) -> bool:
    try:
        fallback_filename, body = fetch_and_convert(url)
        build_and_upload(url, fallback_filename, body)
        return True
    except Exception as e:
        print(f"❌ Error processing {url}: {e}", file=sys.stderr)
        return False


def process_clipboard() -> None:
    print("⏳ Reading content from system clipboard...")
    raw_content = get_clipboard_content()
    if not raw_content or not raw_content.strip():
        raise Web2DriveError("Clipboard is empty or could not be read.")

    url = "Clipboard"
    if is_html_string(raw_content):
        fallback_filename, body = extractor.extract_html_content(url, raw_content)
    else:
        fallback_filename = "clipboard_content.md"
        h1_match = re.search(r"^#\s+(.+)$", raw_content, re.MULTILINE)
        if h1_match:
            fallback_filename = (
                f"{extractor.clean_filename(h1_match.group(1).strip())}.md"
            )
        body = raw_content

    build_and_upload(url, fallback_filename, body)


def run_batch(file_path: str) -> None:
    if not os.path.exists(file_path):
        print(f"❌ Error: File not found at '{file_path}'", file=sys.stderr)
        sys.exit(1)

    urls = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if (
                    line
                    and not line.startswith("#")
                    and line.startswith(("http://", "https://"))
                ):
                    urls.append(line)
    except Exception as e:
        print(f"❌ Error reading batch file: {e}", file=sys.stderr)
        sys.exit(1)

    total_urls = len(urls)
    successful_uploads = 0
    for index, url in enumerate(urls, 1):
        print(f"\n📦 [{index}/{total_urls}] Processing: {url}")
        if process_single_url(url):
            successful_uploads += 1
        if index < total_urls:
            time.sleep(BATCH_SLEEP_SECONDS)
    print(
        f"\n✨ Batch processing completed. Successfully uploaded {successful_uploads}/{total_urls} files."
    )


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print_help()
        sys.exit(0)

    first_arg = sys.argv[1]
    try:
        if first_arg in ("-c", "--clip"):
            process_clipboard()
        elif first_arg in ("-b", "--batch"):
            if len(sys.argv) < 3:
                sys.exit(1)
            run_batch(sys.argv[2])
        else:
            url = first_arg
            if (
                not url.startswith(("http://", "https://"))
                and os.path.exists(url)
                and url.endswith(".txt")
            ):
                run_batch(url)
            elif url.startswith(("http://", "https://")):
                if not process_single_url(url):
                    sys.exit(1)
            else:
                sys.exit(1)
    except Web2DriveError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
