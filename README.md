# OmniFocus Sync

Sync OmniFocus databases from Omni Sync Server to any machine (including Linux) via WebDAV.

## Overview

This tool downloads and decrypts OmniFocus data from Omni Sync Server, allowing you to access your tasks on machines where OmniFocus isn't available (e.g., Linux servers).

## How It Works

### Omni Sync Server Protocol

- **Endpoint:** `https://sync.omnigroup.com/<username>/` (redirects to `sync5.omnigroup.com`)
- **Protocol:** WebDAV with Digest Authentication
- **Data Format:** `.ofocus` bundle containing encrypted transaction ZIP files

### Encryption Scheme

OmniFocus uses a layered encryption approach:

1. **Key Derivation:** PBKDF2-SHA1 with ~1M iterations derives a Key Encryption Key (KEK) from your passphrase
2. **Key Wrapping:** AES-128-WRAP (RFC 3394) protects the document keys
3. **File Encryption:** AES-128-CTR with HMAC-SHA256 for integrity (per 64KB segment)

The encryption passphrase is typically the same as your Omni Sync account password.

### Data Structure

```
OmniFocus.ofocus/
├── encrypted                    # Encryption metadata (PBKDF2 params, wrapped keys)
├── *.capability                 # Feature compatibility markers (plaintext)
├── *.client                     # Client device registrations (plaintext)
├── 00000000000000=*.zip        # Full database snapshot (encrypted)
└── YYYYMMDDHHMMSS=*.zip        # Incremental transactions (encrypted)
```

Each decrypted ZIP contains a `contents.xml` with OmniFocus data in XML format.

## Installation

```bash
# Clone the repository
git clone <repo-url>
cd omnifocus-sync

# Install dependencies (requires uv)
uv sync
```

## Usage

### Basic Usage

```bash
uv run python sync_omnifocus.py \
  --username YOUR_USERNAME \
  --password YOUR_PASSWORD \
  --output ./omnifocus-data
```

### Using Environment Variables

```bash
export OMNISYNC_USER=your_username
export OMNISYNC_PASS=your_password

uv run python sync_omnifocus.py -o ./omnifocus-data
```

### Options

| Option | Description |
|--------|-------------|
| `-u, --username` | Omni Sync username (or `OMNISYNC_USER` env var) |
| `-p, --password` | Omni Sync password (or `OMNISYNC_PASS` env var) |
| `-o, --output` | Output directory for decrypted data (default: `./omnifocus-data`) |
| `--keep-encrypted` | Keep the encrypted bundle after decryption |

### Viewing Data

```bash
# View the full database snapshot
unzip -p omnifocus-data/00000000000000*.zip contents.xml | less

# View a specific transaction
unzip -p omnifocus-data/20260112*.zip contents.xml
```

## Files

| File | Description |
|------|-------------|
| `sync_omnifocus.py` | Main sync script (download + decrypt) |
| `download_ofocus.py` | WebDAV download utility |
| `OmniDecrypt.py` | Decryption library |

## Third-Party Code

### OmniDecrypt.py

**Source:** [OmniGroup/OmniGroup](https://github.com/omnigroup/OmniGroup/blob/main/Frameworks/OmniFileStore/DecryptionExample.py)

This file is the official OmniGroup decryption example, modified for compatibility with modern Python versions.

**Changes made for Python 3.9+ / cryptography 40+ compatibility:**

1. **Backend handling** (lines 20-25): Added try/except to handle removal of `default_backend()` in newer cryptography versions:
   ```python
   try:
       from cryptography.hazmat.backends import default_backend
       backend = default_backend()
   except ImportError:
       backend = None  # Newer versions don't need it
   ```

2. **plistlib API** (lines 122-137): Added fallback for removed `use_builtin_types` parameter:
   ```python
   try:
       metadata = plistlib.load(fp, use_builtin_types=False)
   except TypeError:
       fp.seek(0)
       metadata = plistlib.load(fp)
   ```

3. **plistlib.Data handling** (lines 161-163, 499-501): Added compatibility for direct bytes instead of `plistlib.Data` objects:
   ```python
   salt_obj = metadata.get('salt')
   salt = salt_obj.data if hasattr(salt_obj, 'data') else salt_obj
   ```

The original file is licensed under the OmniGroup Source License.

## XML Data Format

The decrypted `contents.xml` files use the OmniFocus v2 XML namespace:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">
  <task id="...">
    <name>Task name</name>
    <note>Task notes</note>
    <due>2026-01-15T17:00:00Z</due>
    <!-- ... -->
  </task>
  <folder id="...">
    <name>Folder name</name>
    <!-- ... -->
  </folder>
  <!-- contexts, projects, perspectives, etc. -->
</omnifocus>
```

Incremental transactions include `op="update"` or `op="delete"` attributes to indicate changes.

## Security Notes

- Your Omni Sync password is used for both authentication and decryption
- Consider using environment variables or a secrets manager instead of command-line arguments
- The decrypted data contains your full task database - store it securely

## References

- [OmniGroup Encryption Format Documentation](https://github.com/omnigroup/OmniGroup/blob/main/Frameworks/OmniFileStore/EncryptionFormat.md)
- [OmniGroup DecryptionExample.py](https://github.com/omnigroup/OmniGroup/blob/main/Frameworks/OmniFileStore/DecryptionExample.py)
