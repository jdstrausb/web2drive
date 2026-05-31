#!/usr/bin/env python3
import os
import sys
import re
import json
import tempfile
import urllib.parse
import subprocess
import requests
from bs4 import BeautifulSoup
import html2text
import trafilatura

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
COOKIES_PATH = os.path.join(CONFIG_DIR, "cookies.json")

# Import Pydantic for structured validation
from pydantic import BaseModel, Field
from typing import List


class PageMetadata(BaseModel):
    filename: str = Field(
        description="A clean, highly descriptive, and concise filename ending in '.md'. "
        "Use only letters, numbers, underscores, and hyphens (no spaces). "
        "Do not exceed 60 characters in total."
    )
    summary: str = Field(
        description="A concise, 3-sentence executive summary of the core content."
    )
    tags: List[str] = Field(
        description="A list of 3 to 5 highly relevant keywords or category tags."
    )
    reading_time: int = Field(
        description="Estimated reading time in minutes based on document length and complexity."
    )


def print_help():
    """Prints usage documentation for the CLI utility."""
    help_text = """Web2Drive Utility
-----------------
Fetches the content of a URL or reads from the system clipboard, 
cleans up non-essential structural elements, and uploads the parsed 
Markdown file directly to a dedicated 'Web2Drive' folder in Google Drive.

Usage:
  web2drive <URL>
  web2drive --clip | -c
  web2drive --help | -h

Options:
  -c, --clip    Parse and upload content directly from the system clipboard.
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


def get_clipboard_content():
    """Reads content from the system clipboard across platforms using subprocess."""
    # Try macOS (pbpaste)
    try:
        result = subprocess.run(["pbpaste"], capture_output=True, text=True, check=True)
        return result.stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    # Try Linux (xclip)
    try:
        result = subprocess.run(
            ["xclip", "-selection", "clipboard", "-o"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    # Try Linux (xsel)
    try:
        result = subprocess.run(
            ["xsel", "-b", "-o"], capture_output=True, text=True, check=True
        )
        return result.stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    # Try Wayland (wl-paste)
    try:
        result = subprocess.run(
            ["wl-paste"], capture_output=True, text=True, check=True
        )
        return result.stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    print(
        "❌ Error: No supported clipboard utility found (pbpaste, xclip, xsel, wl-paste).",
        file=sys.stderr,
    )
    return None


def is_html_string(text):
    """Heuristic check to identify if a block of text contains rich HTML code."""
    cleaned_start = text.strip()[:150].lower()
    if cleaned_start.startswith("<!doctype") or cleaned_start.startswith("<html"):
        return True

    # Track standard structural blocks to differentiate from code blocks containing stray tags
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


def generate_metadata_and_filename(url, markdown_content):
    """
    Uses gemini-3.1-flash-lite to analyze the content and return a structured
    metadata block and a descriptive filename in a single API call.
    """
    api_key = os.environ.get("WEB2DRIVE_GEMINI_API_KEY")
    if not api_key:
        return None, None

    try:
        client = genai.Client(api_key=api_key)
        content_sample = markdown_content[:8000]

        prompt = (
            "You are an expert document archivist. Analyze the following URL and content sample "
            "to extract a structured metadata profile and determine a clean, professional filename."
        )

        response = client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=[prompt, f"URL: {url}", f"Content Sample:\n{content_sample}"],
            config={
                "response_mime_type": "application/json",
                "response_json_schema": PageMetadata.model_json_schema(),
                "temperature": 0.2,
            },
        )

        if not response.text:
            return None, None

        data = json.loads(response.text)

        tags_line = ", ".join(
            [f"`#{tag.strip().replace(' ', '')}`" for tag in data.get("tags", [])]
        )

        metadata_header = (
            f"---\n"
            f"**Source:** {url}\n"
            f"**Estimated Reading Time:** {data.get('reading_time', 1)} mins\n"
            f"**Tags:** {tags_line}\n"
            f"---\n\n"
            f"### Executive Summary\n"
            f"{data.get('summary', '')}\n\n"
            f"---\n\n"
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


def load_session_cookies():
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


def apply_cookies_to_requests(session, cookies_list):
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


def clean_page_overlays(page):
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


def fetch_with_playwright(url):
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
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            )

            cookies = load_session_cookies()
            if cookies:
                print("🔑 Injecting authentication cookies into Playwright context...")
                context.add_cookies(cookies)

            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=25000)
            clean_page_overlays(page)
            page.wait_for_timeout(1000)

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


def pre_clean_html(html_text):
    """
    Decomposes comment lists, sidebars, related items, headers, footers,
    and general layout noise to ensure only the core text is processed.
    """
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


def extract_html_content(url, html_text):
    """Processes raw HTML text, returning filename suggestions and parsed markdown body."""
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


def fetch_and_convert(url):
    """Fetches the target URL, detects content-type, processes text, and returns metadata + body."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        }
    )

    cookies = load_session_cookies()
    if cookies:
        print("🔑 Applying authentication cookies to static request...")
        apply_cookies_to_requests(session, cookies)

    html_text = None
    is_markdown = False
    is_plain_text = False

    try:
        response = session.get(url, timeout=15)
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

            markdown_text = f"**Source:** {url}\n\n---\n\n{text_content}"
            return filename, markdown_text

        html_text = response.text

    except Exception as e:
        print(f"⚠️ Static fetch failed: {e}", file=sys.stderr)
        print("⏳ Attempting direct fallback to Playwright...", file=sys.stderr)
        html_text = fetch_with_playwright(url)
        if not html_text:
            sys.exit(1)

    filename, markdown_text = extract_html_content(url, html_text)

    clean_text_only = markdown_text.replace(f"**Source:** {url}\n\n---\n\n", "").strip()

    insufficient = False
    if len(clean_text_only) < 400:
        insufficient = True
    elif len(clean_text_only) < 1500:
        if "javascript" in clean_text_only.lower() and any(
            x in clean_text_only.lower() for x in ["enable", "required", "disabled"]
        ):
            insufficient = True

    if insufficient:
        print(
            "⚠️ Extracted content is short or suggests dynamic JS is required. Trying Playwright fallback..."
        )
        dynamic_html = fetch_with_playwright(url)
        if dynamic_html:
            filename, markdown_text = extract_html_content(url, dynamic_html)

    markdown_text = re.sub(r"\n{3,}", "\n\n", markdown_text)
    markdown_text = f"**Source:** {url}\n\n---\n\n" + markdown_text.replace(
        f"**Source:** {url}\n\n---\n\n", ""
    )
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

    # Determine execution source
    use_clipboard = sys.argv[1] in ("-c", "--clip")

    if use_clipboard:
        print("⏳ Reading content from system clipboard...")
        raw_content = get_clipboard_content()
        if not raw_content or not raw_content.strip():
            print("❌ Error: Clipboard is empty or could not be read.", file=sys.stderr)
            sys.exit(1)

        url = "Clipboard"

        if is_html_string(raw_content):
            print(
                "⏳ Detected HTML structure in clipboard. Extracting main content body..."
            )
            filename, markdown_content = extract_html_content(url, raw_content)
        else:
            print("⏳ Detected plain text/Markdown structure in clipboard.")
            filename = "clipboard_content.md"
            h1_match = re.search(r"^#\s+(.+)$", raw_content, re.MULTILINE)
            if h1_match:
                filename = f"{clean_filename(h1_match.group(1).strip())}.md"
            markdown_content = raw_content
    else:
        url = sys.argv[1]
        print(f"⏳ Extracting content from: {url}...")
        filename, markdown_content = fetch_and_convert(url)

    # Clean standard fallback source line to prevent duplication
    clean_content_body = markdown_content.replace(f"**Source:** {url}\n\n---\n\n", "")

    # Retrieve filename and structured metadata from Gemini
    llm_filename, metadata_header = generate_metadata_and_filename(
        url, clean_content_body
    )

    if llm_filename and metadata_header:
        filename = llm_filename
        final_document = metadata_header + clean_content_body
    else:
        final_document = markdown_content

    temp_file = tempfile.NamedTemporaryFile(
        mode="w+", suffix=".md", delete=False, encoding="utf-8"
    )
    try:
        temp_file.write(final_document)
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
