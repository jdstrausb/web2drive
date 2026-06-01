#!/usr/bin/env python3
import os
import sys
import re
import json
import tempfile
import urllib.parse
import subprocess
import time

import requests
from bs4 import BeautifulSoup
import html2text
import trafilatura
from pydantic import BaseModel, Field

# Modern Google Gen AI SDK
from google import genai

# Google API & OAuth imports for Google Drive interaction
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# --- Configuration paths -----------------------------------------------------

# Scopes required to upload files the script creates
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

CONFIG_DIR = os.path.expanduser("~/.config/web2drive")
CREDENTIALS_PATH = os.path.join(CONFIG_DIR, "credentials.json")
TOKEN_PATH = os.path.join(CONFIG_DIR, "token.json")
COOKIES_PATH = os.path.join(CONFIG_DIR, "cookies.json")

# --- Behavioural constants ---------------------------------------------------

DRIVE_FOLDER_NAME = "Web2Drive"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# Gemini metadata generation
GEMINI_MODEL = "gemini-3.1-flash-lite"
LLM_TEMPERATURE = 0.2
LLM_CONTENT_SAMPLE_CHARS = 8000

# Fetch / rendering
STATIC_FETCH_TIMEOUT = 15  # seconds, for the requests static fetch
PLAYWRIGHT_TIMEOUT_MS = 25000  # ms, for page.goto
PLAYWRIGHT_SETTLE_MS = 1000  # ms, post-load settle before reading content

# Content-sufficiency heuristics (measured in chars of extracted body)
MIN_CONTENT_CHARS = 400  # below this, treat extraction as failed
SHORT_CONTENT_CHARS = 1500  # below this, inspect for "JS required" hints

# Batch pacing
BATCH_SLEEP_SECONDS = 2

# Document assembly. Kept in one place so the header format can never drift
# between the producer and any consumer.
SOURCE_HEADER_TEMPLATE = "**Source:** {url}\n\n---\n\n"


def format_source_header(url: str) -> str:
    """Returns the minimal source header prepended when no LLM metadata exists."""
    return SOURCE_HEADER_TEMPLATE.format(url=url)


class Web2DriveError(Exception):
    """Raised for expected, user-facing failures in the processing pipeline.

    Library-level functions raise this instead of calling sys.exit, so that
    callers (single-URL, clipboard, batch) can decide whether a failure is
    fatal or merely skippable. Using a normal Exception subclass also means
    these are caught by the per-URL handler in batch mode, unlike SystemExit.
    """


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
    """Prints usage documentation for the CLI utility."""
    help_text = """Web2Drive Utility
-----------------
Fetches the content of a URL, reads from the system clipboard, or batch-processes
a list of URLs from a local text file, cleans and formats the content, and uploads
the resulting Markdown files directly to a dedicated 'Web2Drive' folder in Google Drive.

Usage:
  web2drive <URL>
  web2drive --clip | -c
  web2drive --batch | -b <path_to_file.txt>
  web2drive --help | -h

Options:
  -c, --clip     Parse and upload content directly from the system clipboard.
  -b, --batch    Batch-process a list of URLs from a local text file (one URL per line).
  -h, --help     Show this usage documentation.
"""
    print(help_text)


def get_gdrive_service():
    """Authenticates the user and returns the Drive API service.

    Raises Web2DriveError if the OAuth client credentials are missing.
    """
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_PATH):
                raise Web2DriveError(f"Missing credentials file at {CREDENTIALS_PATH}")
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)

        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(TOKEN_PATH, "w") as token:
            token.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


def clean_filename(title: str) -> str:
    """Sanitizes text to make it a safe, readable filename."""
    safe = re.sub(r'[\\/*?:"<>|]', "", title)
    safe = re.sub(r"\s+", " ", safe).strip()
    return safe[:60]


def get_fallback_filename(url: str) -> str:
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


def get_clipboard_content() -> str | None:
    """Reads content from the system clipboard across platforms using subprocess."""
    # Each candidate is (command, args). The first available utility wins.
    clipboard_commands = [
        ["pbpaste"],  # macOS
        ["xclip", "-selection", "clipboard", "-o"],  # Linux (X11)
        ["xsel", "-b", "-o"],  # Linux (X11)
        ["wl-paste"],  # Linux (Wayland)
    ]

    for command in clipboard_commands:
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            return result.stdout
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue

    print(
        "❌ Error: No supported clipboard utility found (pbpaste, xclip, xsel, wl-paste).",
        file=sys.stderr,
    )
    return None


def is_html_string(text: str) -> bool:
    """Heuristic check to identify if a block of text contains rich HTML code."""
    cleaned_start = text.strip()[:150].lower()
    if cleaned_start.startswith("<!doctype") or cleaned_start.startswith("<html"):
        return True

    # Count standard structural blocks to differentiate from code blocks
    # that merely contain stray tags.
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
    match_count = sum(text_lower.count(tag) for tag in tags)
    return match_count >= 3


def generate_metadata_and_filename(
    url: str, markdown_content: str
) -> tuple[str | None, str | None]:
    """Uses Gemini to derive a descriptive filename and a structured metadata header.

    Returns (filename, metadata_header) on success, or (None, None) if the API
    key is absent or the call fails. This is best-effort: callers fall back to
    deterministic naming when it returns None.
    """
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
        filename = re.sub(r'[\\/*?:"<>|\s]', "_", filename)

        return filename, metadata_header

    except Exception as e:
        print(
            f"⚠️ LLM metadata generation failed: {e}. Falling back to default naming.",
            file=sys.stderr,
        )
        return None, None


def load_session_cookies() -> list | None:
    """Loads saved authentication cookies from the configuration directory."""
    if not os.path.exists(COOKIES_PATH):
        return None
    try:
        with open(COOKIES_PATH, "r") as f:
            cookies = json.load(f)
            if isinstance(cookies, list):
                return cookies
    except Exception as e:
        print(f"⚠️ Warning: Could not parse {COOKIES_PATH}: {e}", file=sys.stderr)
    return None


def apply_cookies_to_requests(session: requests.Session, cookies_list: list) -> None:
    """Binds loaded JSON cookies to a requests.Session object."""
    for cookie in cookies_list:
        name = cookie.get("name")
        value = cookie.get("value")
        if name is not None and value is not None:
            session.cookies.set(
                name=name,
                value=value,
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
                secure=cookie.get("secure", False),
            )


def clean_page_overlays(page) -> None:
    """Injects a client-side JS script to remove banners, modals, and paywalls."""
    js_cleanup_script = """
    () => {
        const selectors = [
            '[id*="consent"]', '[class*="consent"]',
            '[id*="cookie"]', '[class*="cookie"]',
            '[class*="paywall"]', '[id*="paywall"]',
            '[class*="overlay"]', '[class*="modal"]',
            '.tp-modal', '.tp-backdrop'
        ];

        selectors.forEach(selector => {
            try {
                document.querySelectorAll(selector).forEach(el => {
                    const style = window.getComputedStyle(el);
                    if (style.position === 'fixed' || style.position === 'absolute' || parseInt(style.zIndex) > 99) {
                        el.remove();
                    }
                });
            } catch (e) {}
        });

        document.body.style.setProperty('overflow', 'auto', 'important');
        document.documentElement.style.setProperty('overflow', 'auto', 'important');
    }
    """
    try:
        page.evaluate(js_cleanup_script)
    except Exception as e:
        print(f"⚠️ Warning: Overlay cleanup failed: {e}", file=sys.stderr)


def fetch_with_playwright(url: str) -> str | None:
    """Fallback fetcher using headless Playwright to handle JavaScript-heavy SPAs."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "⚠️ Playwright package is not installed. To support dynamic SPAs, install it via:\n"
            "  pip install playwright && playwright install chromium",
            file=sys.stderr,
        )
        return None

    print("⏳ Launching headless browser via Playwright to load dynamic content...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=USER_AGENT)

            cookies = load_session_cookies()
            if cookies:
                print("🔑 Injecting authentication cookies into Playwright context...")
                context.add_cookies(cookies)

            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT_MS)
            clean_page_overlays(page)
            page.wait_for_timeout(PLAYWRIGHT_SETTLE_MS)

            html_content = page.content()
            browser.close()
            return html_content
    except Exception as e:
        if "playwright install" in str(e).lower() or "executable" in str(e).lower():
            print(
                "⚠️ Playwright browsers are not installed. Please install them by running:\n"
                "  playwright install chromium",
                file=sys.stderr,
            )
        else:
            print(f"⚠️ Playwright rendering failed: {e}", file=sys.stderr)
        return None


def pre_clean_html(html_text: str) -> str:
    """Strips layout noise (nav, sidebars, comments, widgets) before extraction."""
    soup = BeautifulSoup(html_text, "html.parser")

    noise_selectors = [
        # Layout components
        "header",
        "footer",
        "nav",
        "aside",
        "noscript",
        "iframe",
        "style",
        "script",
        # Comments and community sections
        "#comments",
        ".comments",
        "#disqus_thread",
        ".commentlist",
        ".comment-respond",
        "#respond",
        ".reply",
        # Sidebars and widgets
        ".sidebar",
        "#sidebar",
        ".widget-area",
        ".secondary",
        ".related",
        ".recent-posts",
        ".archives",
        ".categories",
        ".tagcloud",
        # Sharing networks and tracking indicators
        ".social-share",
        ".share-buttons",
        ".author-bio",
        ".post-author",
        ".entry-utility",
        # Custom WordPress elements
        ".sharedaddy",
        "#jp-post-flair",
        ".subscribe-block",
        "#subscribe-blog",
        ".wpcnt",
    ]

    for selector in noise_selectors:
        for element in soup.select(selector):
            element.decompose()

    return str(soup)


def extract_html_content(url: str, html_text: str) -> tuple[str, str]:
    """Processes raw HTML, returning a (filename, markdown_body) pair.

    The returned body never includes a source header; assembly of the final
    document happens in build_and_upload.
    """
    cleaned_html = pre_clean_html(html_text)

    title = ""
    markdown_text = None

    try:
        result = trafilatura.bare_extraction(
            cleaned_html,
            output_format="markdown",
            include_links=True,
            include_images=True,
            include_tables=True,
        )
        if result and isinstance(result, dict):
            title = result.get("title") or ""
            markdown_text = result.get("text")
    except Exception as e:
        print(
            f"⚠️ Trafilatura extraction failed: {e}. Falling back to BeautifulSoup.",
            file=sys.stderr,
        )

    if markdown_text:
        if title:
            filename = f"{clean_filename(title)}.md"
        else:
            soup = BeautifulSoup(cleaned_html, "html.parser")
            title_tag = soup.find("title")
            if title_tag and title_tag.get_text().strip():
                title = title_tag.get_text().strip()
                filename = f"{clean_filename(title)}.md"
            else:
                filename = f"{get_fallback_filename(url)}.md"
    else:
        soup = BeautifulSoup(cleaned_html, "html.parser")

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

        body_content = soup.find("article") or soup.find("main") or soup.body or soup

        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = False
        h.ignore_emphasis = False
        h.body_width = 0

        markdown_text = h.handle(str(body_content))

    return filename, markdown_text


def fetch_and_convert(url: str) -> tuple[str, str]:
    """Fetches the URL, detects content-type, and returns a (filename, body) pair.

    The body is clean Markdown with no source header attached. Raises
    Web2DriveError if neither the static request nor the Playwright fallback
    can retrieve the page.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    cookies = load_session_cookies()
    if cookies:
        print("🔑 Applying authentication cookies to static request...")
        apply_cookies_to_requests(session, cookies)

    html_text = None

    try:
        response = session.get(url, timeout=STATIC_FETCH_TIMEOUT)
        response.raise_for_status()

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

            return filename, text_content

        html_text = response.text

    except Exception as e:
        print(f"⚠️ Static fetch failed: {e}", file=sys.stderr)
        print("⏳ Attempting direct fallback to Playwright...", file=sys.stderr)
        html_text = fetch_with_playwright(url)
        if not html_text:
            raise Web2DriveError(
                f"Could not fetch '{url}': static request and Playwright both failed."
            ) from e

    filename, markdown_text = extract_html_content(url, html_text)

    clean_text = markdown_text.strip()
    insufficient = False
    if len(clean_text) < MIN_CONTENT_CHARS:
        insufficient = True
    elif len(clean_text) < SHORT_CONTENT_CHARS:
        lowered = clean_text.lower()
        if "javascript" in lowered and any(
            x in lowered for x in ("enable", "required", "disabled")
        ):
            insufficient = True

    if insufficient:
        print(
            "⚠️ Extracted content is short or suggests dynamic JS is required. "
            "Trying Playwright fallback..."
        )
        dynamic_html = fetch_with_playwright(url)
        if dynamic_html:
            filename, markdown_text = extract_html_content(url, dynamic_html)

    markdown_text = re.sub(r"\n{3,}", "\n\n", markdown_text)
    return filename, markdown_text


def get_or_create_folder(service, folder_name: str = DRIVE_FOLDER_NAME) -> str:
    """Finds or creates a dedicated directory inside Google Drive.

    Raises Web2DriveError if the Drive API call fails.
    """
    query = (
        "mimeType = 'application/vnd.google-apps.folder' "
        f"and name = '{folder_name}' and trashed = false"
    )
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
        raise Web2DriveError(f"Error resolving Google Drive folder: {e}") from e


def upload_to_drive(local_file_path: str, drive_filename: str) -> None:
    """Uploads a local file to the dedicated Web2Drive folder on Google Drive.

    Raises Web2DriveError if authentication, folder resolution, or upload fails.
    """
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
        print(f"✅ Successfully uploaded '{file.get('name')}' to Google Drive.")
        print(f"File ID: {file.get('id')}")
    except Exception as e:
        raise Web2DriveError(f"Error uploading to Google Drive: {e}") from e


def write_and_upload(document: str, filename: str) -> None:
    """Writes a document to a temp file, uploads it, and always cleans up.

    The temp file is removed in a finally block regardless of upload outcome.
    """
    temp_file = tempfile.NamedTemporaryFile(
        mode="w+", suffix=".md", delete=False, encoding="utf-8"
    )
    try:
        temp_file.write(document)
        temp_file.close()

        print(f"⏳ Uploading to Google Drive as '{filename}'...")
        upload_to_drive(temp_file.name, filename)
    finally:
        if os.path.exists(temp_file.name):
            try:
                os.remove(temp_file.name)
            except OSError as e:
                print(
                    f"Warning: Could not remove temporary file {temp_file.name}: {e}",
                    file=sys.stderr,
                )


def build_and_upload(url: str, fallback_filename: str, body: str) -> None:
    """Assembles the final document from a body and uploads it.

    Asks Gemini for a descriptive filename and metadata header; on failure,
    falls back to the deterministic filename and a minimal source header. This
    is the single shared path used by both single-URL and clipboard processing.
    """
    llm_filename, metadata_header = generate_metadata_and_filename(url, body)

    if llm_filename and metadata_header:
        filename = llm_filename
        document = metadata_header + body
    else:
        filename = fallback_filename
        document = format_source_header(url) + body

    write_and_upload(document, filename)


def process_single_url(url: str) -> bool:
    """Fetches, converts, summarizes, and uploads a single URL.

    Returns True on success, False on any handled failure. Errors are caught
    here (rather than propagated) so that batch runs continue past a bad URL.
    """
    try:
        fallback_filename, body = fetch_and_convert(url)
        build_and_upload(url, fallback_filename, body)
        return True
    except Web2DriveError as e:
        print(f"❌ Error processing {url}: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"❌ Unexpected error processing {url}: {e}", file=sys.stderr)
        return False


def process_clipboard() -> None:
    """Parses system clipboard content and uploads it directly to Google Drive.

    Raises Web2DriveError if the clipboard is empty/unreadable or the upload
    fails; the caller (main) translates that into an exit code.
    """
    print("⏳ Reading content from system clipboard...")
    raw_content = get_clipboard_content()
    if not raw_content or not raw_content.strip():
        raise Web2DriveError("Clipboard is empty or could not be read.")

    url = "Clipboard"

    if is_html_string(raw_content):
        print(
            "⏳ Detected HTML structure in clipboard. Extracting main content body..."
        )
        fallback_filename, body = extract_html_content(url, raw_content)
    else:
        print("⏳ Detected plain text/Markdown structure in clipboard.")
        fallback_filename = "clipboard_content.md"
        h1_match = re.search(r"^#\s+(.+)$", raw_content, re.MULTILINE)
        if h1_match:
            fallback_filename = f"{clean_filename(h1_match.group(1).strip())}.md"
        body = raw_content

    build_and_upload(url, fallback_filename, body)


def run_batch(file_path: str) -> None:
    """Reads a file of URLs and sequentially processes each one."""
    if not os.path.exists(file_path):
        print(f"❌ Error: File not found at '{file_path}'", file=sys.stderr)
        sys.exit(1)

    urls = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith(("http://", "https://")):
                    urls.append(line)
                else:
                    print(
                        f"⚠️ Warning: Skipping invalid URL line: '{line}'",
                        file=sys.stderr,
                    )
    except Exception as e:
        print(f"❌ Error reading batch file: {e}", file=sys.stderr)
        sys.exit(1)

    if not urls:
        print("⚠️ Warning: No valid URLs found in the batch file.", file=sys.stderr)
        sys.exit(0)

    total_urls = len(urls)
    print(f"🚀 Found {total_urls} valid URLs to process in batch mode.")

    successful_uploads = 0
    for index, url in enumerate(urls, 1):
        print("\n────────────────────────────────────────")
        print(f"📦 [{index}/{total_urls}] Processing: {url}")
        print("────────────────────────────────────────")
        if process_single_url(url):
            successful_uploads += 1

        # Defensive pacing sleep to avoid rate limiting
        if index < total_urls:
            time.sleep(BATCH_SLEEP_SECONDS)

    print(
        f"\n✨ Batch processing completed. "
        f"Successfully uploaded {successful_uploads}/{total_urls} files."
    )


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print_help()
        sys.exit(0)

    first_arg = sys.argv[1]

    try:
        # Mode dispatching
        if first_arg in ("-c", "--clip"):
            process_clipboard()

        elif first_arg in ("-b", "--batch"):
            if len(sys.argv) < 3:
                print(
                    "❌ Error: Please provide the path to a text file containing URLs.",
                    file=sys.stderr,
                )
                sys.exit(1)
            run_batch(sys.argv[2])

        else:
            # Standard input processing
            url = first_arg
            if not url.startswith(("http://", "https://")):
                # Auto-detect a local .txt file as an implicit batch option
                if os.path.exists(url) and url.endswith(".txt"):
                    print(
                        f"💡 Detected a local text file. Processing '{url}' in batch mode..."
                    )
                    run_batch(url)
                else:
                    print(
                        f"❌ Error: Invalid URL, text file path, or flag: '{url}'",
                        file=sys.stderr,
                    )
                    print_help()
                    sys.exit(1)
            else:
                print(f"⏳ Extracting content from: {url}...")
                if not process_single_url(url):
                    sys.exit(1)

    except Web2DriveError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
