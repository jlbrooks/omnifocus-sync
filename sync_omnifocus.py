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
import urllib.parse
from pathlib import Path

import requests
from requests.auth import HTTPDigestAuth

# Import decryption functionality
from OmniDecrypt import DocumentKey, decrypt_directory


def list_webdav_directory(url: str, auth: HTTPDigestAuth) -> list[dict]:
    """List contents of a WebDAV directory using PROPFIND."""
    headers = {"Depth": "1"}
    response = requests.request("PROPFIND", url, auth=auth, headers=headers)
    response.raise_for_status()

    hrefs = re.findall(r'<D:href>([^<]+)</D:href>', response.text)

    files = []
    for href in hrefs:
        decoded = urllib.parse.unquote(href)
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


def download_bundle(username: str, password: str, output_dir: Path) -> Path:
    """Download the OmniFocus.ofocus bundle from Omni Sync Server."""
    base_url = f"https://sync.omnigroup.com/{username}/OmniFocus.ofocus/"
    auth = HTTPDigestAuth(username, password)

    # Get redirected URL
    response = requests.head(base_url, auth=auth, allow_redirects=True)
    actual_url = response.url
    print(f"Sync URL: {actual_url}")

    # List files
    print("Listing bundle contents...")
    files = list_webdav_directory(actual_url, auth)
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
