"""Install the pinned official Windows x64 release into ignored tools/ storage."""
import hashlib
import io
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

VERSION = '13.64'
ROOT = Path(__file__).resolve().parent.parent
BASE = 'https://exiftool.org/'


def download(name):
    url = ('https://downloads.sourceforge.net/project/exiftool/' + name) if name.endswith('.zip') else BASE + name
    request = urllib.request.Request(url, headers={'User-Agent': 'PhotoVault-setup/1.0'})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(30 * 1024 * 1024 + 1)
    if len(data) > 30 * 1024 * 1024:
        raise RuntimeError('Download exceeds the setup limit')
    return data


def main():
    if sys.platform != 'win32':
        raise SystemExit('Install a maintained ExifTool package with your OS package manager; see README.md.')
    name = f'exiftool-{VERSION}_64.zip'
    checksums = download(f'checksums-{VERSION}.txt').decode('utf-8')
    expected = None
    for line in checksums.splitlines():
        if name in line:
            match = re.search(r'\b[0-9a-fA-F]{64}\b', line)
            if match:
                expected = match[0].lower()
    if not expected:
        raise RuntimeError('Official SHA-256 checksum not found; refusing to install')
    archive = download(name)
    if hashlib.sha256(archive).hexdigest() != expected:
        raise RuntimeError('SHA-256 verification failed')
    destination = ROOT / 'tools' / f'exiftool-{VERSION}'
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for member in bundle.infolist():
            target = (destination / member.filename).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise RuntimeError('Unsafe archive path')
        bundle.extractall(destination)
    executable = next(destination.rglob('exiftool(-k).exe'))
    executable = executable.rename(executable.with_name('exiftool.exe'))
    # Predictable ignored location; executable must stay beside exiftool_files.
    (ROOT / 'tools' / 'exiftool-path.txt').write_text(str(executable), encoding='utf-8')
    print(f'Installed ExifTool {VERSION}; archive SHA-256 verified. The app discovers tools/exiftool-path.txt.')


if __name__ == '__main__':
    main()
