#!/usr/bin/env python3
"""
Sync OmniFocus database from Omni Sync Server to local machine.

This script:
1. Downloads the encrypted .ofocus bundle via WebDAV
2. Decrypts all transaction files
3. Outputs decrypted XML that can be parsed on Linux

Usage:
    python sync_omnifocus.py --username USER --password PASS --output ./output

Environment variables (alternative to CLI args):
    OMNISYNC_USER - Omni Sync username
    OMNISYNC_PASS - Omni Sync password (also used as encryption passphrase)
"""

import argparse
import getpass
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

import requests
from requests.auth import HTTPDigestAuth

# Import decryption functionality
from OmniDecrypt import DocumentKey, decrypt_directory


def list_html_directory(url: str, auth: HTTPDigestAuth, max_retries: int = 3) -> tuple[list[dict], str]:
    """List contents of an Apache directory listing via HTML GET.

    Fallback when PROPFIND is blocked by server configuration.

    Returns:
        Tuple of (file list, actual URL after redirects)
    """
    for attempt in range(max_retries):
        response = requests.get(url, auth=auth, allow_redirects=True)
        if response.status_code == 200:
            break
        elif response.status_code == 401 and attempt < max_retries - 1:
            wait_time = 2 ** attempt
            print(f"  Auth failed, retrying in {wait_time}s...")
            time.sleep(wait_time)
        else:
            response.raise_for_status()

    response.raise_for_status()
    actual_url = response.url

    # Parse Apache directory listing HTML: <a href="filename">
    hrefs = re.findall(r'<a href="([^"]+)">', response.text)

    files = []
    for href in hrefs:
        # Skip parent directory and sorting links
        if href.startswith('?') or href.startswith('/'):
            continue
        decoded = urllib.parse.unquote(href)
        files.append({
            'href': href,
            'name': decoded.rstrip('/'),
            'is_dir': decoded.endswith('/')
        })
    return files, actual_url


def list_webdav_directory(url: str, auth: HTTPDigestAuth, max_retries: int = 3) -> tuple[list[dict], str]:
    """List contents of a WebDAV directory using PROPFIND.

    Falls back to HTML directory listing if PROPFIND fails (some servers block it).

    Returns:
        Tuple of (file list, actual URL after redirects)
    """
    headers = {"Depth": "1"}

    for attempt in range(max_retries):
        # Don't follow redirects automatically - PROPFIND gets converted to GET on redirect
        response = requests.request("PROPFIND", url, auth=auth, headers=headers, allow_redirects=False)

        # Handle redirect manually to preserve PROPFIND method
        if response.status_code in (301, 302, 303, 307, 308):
            redirect_url = response.headers.get('Location')
            if redirect_url:
                # Make PROPFIND request to redirect target with fresh auth
                response = requests.request("PROPFIND", redirect_url, auth=auth, headers=headers)

        if response.status_code == 207:
            break
        elif response.status_code == 401 and attempt < max_retries - 1:
            wait_time = 2 ** attempt  # exponential backoff: 1, 2, 4 seconds
            print(f"  Auth failed, retrying in {wait_time}s...")
            time.sleep(wait_time)
        else:
            # PROPFIND blocked - fall back to HTML parsing
            print("  PROPFIND blocked, using HTML directory listing...")
            return list_html_directory(url, auth, max_retries)

    response.raise_for_status()

    actual_url = response.url
    hrefs = re.findall(r'<D:href>([^<]+)</D:href>', response.text)

    files = []
    for href in hrefs:
        decoded = urllib.parse.unquote(href)
        if decoded.rstrip('/') == urllib.parse.urlparse(actual_url).path.rstrip('/'):
            continue
        files.append({
            'href': href,
            'name': os.path.basename(decoded.rstrip('/')),
            'is_dir': decoded.endswith('/')
        })
    return files, actual_url


def download_file(url: str, auth: HTTPDigestAuth, output_path: Path, max_retries: int = 3) -> None:
    """Download a single file from WebDAV."""
    for attempt in range(max_retries):
        response = requests.get(url, auth=auth, stream=True)
        if response.status_code == 200:
            break
        elif response.status_code == 401 and attempt < max_retries - 1:
            time.sleep(2 ** attempt)
        else:
            response.raise_for_status()

    response.raise_for_status()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)


def resolve_sync_url(username: str, auth: HTTPDigestAuth) -> str:
    """Resolve the actual sync server URL by following redirects."""
    base_url = f"https://sync.omnigroup.com/{username}/OmniFocus.ofocus/"

    # Follow redirects to find actual sync server (e.g., sync5.omnigroup.com)
    # Note: Don't pass auth here - it won't be sent to redirected host anyway
    response = requests.head(base_url, allow_redirects=True)
    actual_url = response.url
    print(f"  Redirected to: {actual_url}")

    # Now authenticate to the resolved URL directly (fresh auth negotiation)
    response = requests.get(actual_url, auth=auth)
    if response.status_code == 401:
        # Try with a session for persistent auth
        session = requests.Session()
        session.auth = auth
        response = session.get(actual_url)

    response.raise_for_status()
    return actual_url


def download_bundle(username: str, password: str, output_dir: Path) -> Path:
    """Download the OmniFocus.ofocus bundle from Omni Sync Server."""
    auth = HTTPDigestAuth(username, password)

    # Resolve actual sync server URL (handles sync.omnigroup.com -> sync5.omnigroup.com redirect)
    print("Resolving sync server...")
    base_url = resolve_sync_url(username, auth)
    print(f"Using: {base_url}")

    # List files
    print("Listing bundle contents...")
    files, actual_url = list_html_directory(base_url, auth)
    print(f"Sync URL: {actual_url}")
    print(f"Found {len(files)} items")

    # Create bundle directory
    bundle_dir = output_dir / "OmniFocus.ofocus"
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    # Download each file
    for i, file_info in enumerate(files):
        name = file_info['name']
        if file_info['is_dir']:
            continue

        print(f"  [{i+1}/{len(files)}] {name}")
        file_url = urllib.parse.urljoin(actual_url, file_info['href'])
        output_path = bundle_dir / name

        try:
            download_file(file_url, auth, output_path)
        except Exception as e:
            print(f"    ERROR: {e}")

    return bundle_dir


def decrypt_bundle(bundle_dir: Path, output_dir: Path, passphrase: str) -> None:
    """Decrypt the OmniFocus bundle."""
    print(f"\nDecrypting bundle...")

    # Load encryption metadata
    metadata_path = bundle_dir / "encrypted"
    if not metadata_path.exists():
        print("No encryption metadata found - bundle may not be encrypted")
        shutil.copytree(bundle_dir, output_dir)
        return

    with open(metadata_path, 'rb') as f:
        encryption_metadata = DocumentKey.parse_metadata(f)

    # Derive key from passphrase
    metadata_key = DocumentKey.use_passphrase(encryption_metadata, passphrase)

    # Load document key
    key_obj = encryption_metadata.get('key')
    key_data = key_obj.data if hasattr(key_obj, 'data') else key_obj
    doc_key = DocumentKey(key_data, unwrapping_key=metadata_key)

    print("Key slots found:")
    for secret in doc_key.secrets:
        secret.print()

    # Decrypt all files
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    for file_path in bundle_dir.iterdir():
        if file_path.name == "encrypted":
            continue

        output_path = output_dir / file_path.name
        print(f"  Decrypting: {file_path.name}")

        with open(file_path, 'rb') as infp:
            with open(output_path, 'wb') as outfp:
                try:
                    doc_key.decrypt_file(file_path.name, infp, outfp)
                except ValueError as e:
                    # File might be plaintext (e.g., .client, .capability)
                    infp.seek(0)
                    outfp.write(infp.read())


def main():
    parser = argparse.ArgumentParser(description="Sync OmniFocus from Omni Sync Server")
    parser.add_argument('--username', '-u',
                        default=os.environ.get('OMNISYNC_USER'),
                        help='Omni Sync username (or set OMNISYNC_USER)')
    parser.add_argument('--password', '-p',
                        default=os.environ.get('OMNISYNC_PASS'),
                        help='Omni Sync password (or set OMNISYNC_PASS)')
    parser.add_argument('--output', '-o', type=Path, default=Path('./omnifocus-data'),
                        help='Output directory for decrypted data')
    parser.add_argument('--keep-encrypted', action='store_true',
                        help='Keep the encrypted bundle after decryption')

    args = parser.parse_args()

    # Get credentials
    username = args.username
    password = args.password

    if not username:
        username = input("Omni Sync username: ")
    if not password:
        password = getpass.getpass("Omni Sync password: ")

    # Create temp directory for encrypted bundle
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Download
        print("=" * 60)
        print("DOWNLOADING OMNIFOCUS BUNDLE")
        print("=" * 60)
        bundle_dir = download_bundle(username, password, tmpdir)

        # Decrypt
        print("\n" + "=" * 60)
        print("DECRYPTING BUNDLE")
        print("=" * 60)
        decrypt_bundle(bundle_dir, args.output, password)

        if args.keep_encrypted:
            encrypted_dir = args.output.parent / "encrypted-bundle"
            shutil.copytree(bundle_dir, encrypted_dir)
            print(f"\nEncrypted bundle saved to: {encrypted_dir}")

    print("\n" + "=" * 60)
    print("SYNC COMPLETE")
    print("=" * 60)
    print(f"Decrypted data written to: {args.output}")
    print(f"\nTo view task data, extract the .zip files:")
    print(f"  unzip -p {args.output}/00000000000000*.zip contents.xml | less")


if __name__ == '__main__':
    main()
