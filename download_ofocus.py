#!/usr/bin/env python3
"""
Download an OmniFocus.ofocus bundle from Omni Sync Server via WebDAV.

Usage:
    python download_ofocus.py <username> <password> <output_dir>
"""

import os
import sys
import re
import urllib.parse
from pathlib import Path

import requests
from requests.auth import HTTPDigestAuth


def list_webdav_directory(url: str, auth: HTTPDigestAuth) -> list[dict]:
    """List contents of a WebDAV directory using PROPFIND."""
    headers = {"Depth": "1"}
    response = requests.request("PROPFIND", url, auth=auth, headers=headers)
    response.raise_for_status()

    # Parse the XML response to extract hrefs
    # Simple regex parsing (could use xml.etree for more robust parsing)
    hrefs = re.findall(r'<D:href>([^<]+)</D:href>', response.text)

    files = []
    for href in hrefs:
        # Decode URL encoding
        decoded = urllib.parse.unquote(href)
        # Skip the directory itself
        if decoded.rstrip('/') == urllib.parse.urlparse(url).path.rstrip('/'):
            continue
        files.append({
            'href': href,
            'name': os.path.basename(decoded.rstrip('/')),
            'is_dir': decoded.endswith('/')
        })
    return files


def download_file(url: str, auth: HTTPDigestAuth, output_path: Path) -> None:
    """Download a single file from WebDAV."""
    response = requests.get(url, auth=auth, stream=True)
    response.raise_for_status()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)


def download_ofocus_bundle(username: str, password: str, output_dir: Path) -> None:
    """Download the entire OmniFocus.ofocus bundle."""
    base_url = f"https://sync.omnigroup.com/{username}/OmniFocus.ofocus/"
    auth = HTTPDigestAuth(username, password)

    # First, get redirected URL
    response = requests.head(base_url, auth=auth, allow_redirects=True)
    actual_url = response.url
    print(f"Sync URL: {actual_url}")

    # List all files
    print("Listing bundle contents...")
    files = list_webdav_directory(actual_url, auth)
    print(f"Found {len(files)} items")

    # Create output directory
    bundle_dir = output_dir / "OmniFocus.ofocus"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    # Download each file
    for i, file_info in enumerate(files):
        name = file_info['name']
        if file_info['is_dir']:
            print(f"  [{i+1}/{len(files)}] Skipping directory: {name}")
            continue

        print(f"  [{i+1}/{len(files)}] Downloading: {name}")

        # Construct full URL
        file_url = urllib.parse.urljoin(actual_url, file_info['href'])
        output_path = bundle_dir / name

        try:
            download_file(file_url, auth, output_path)
        except Exception as e:
            print(f"    ERROR: {e}")

    print(f"\nBundle downloaded to: {bundle_dir}")


def main():
    if len(sys.argv) < 4:
        print("Usage: python download_ofocus.py <username> <password> <output_dir>")
        sys.exit(1)

    username = sys.argv[1]
    password = sys.argv[2]
    output_dir = Path(sys.argv[3])

    download_ofocus_bundle(username, password, output_dir)


if __name__ == '__main__':
    main()
