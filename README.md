# Efix Photo Archive

A new local project implementing private photo uploads and admin-only metadata inspection. The starting workspace was empty, so there was no existing framework, backend, database, upload flow, dashboard, or retention policy to extend.

The application uses **Flask 3**, server-rendered **Jinja** templates, **SQLite**, **ExifTool 13.64**, **Pillow 13**, and **pillow-heif 1.8**. It includes account login, role checks, CSRF protection, private storage, and a responsive dashboard. There is no frontend build step and no default account.

## Run locally (Windows PowerShell)

Use Python 3.12 or newer. The implementation has been tested on Windows with Python 3.14.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts\install_exiftool.py
.\.venv\Scripts\python.exe -m flask --app photo_app init-db
.\.venv\Scripts\python.exe -m flask --app photo_app create-user --username admin --admin
.\.venv\Scripts\python.exe -m flask --app photo_app create-user --username uploader
.\.venv\Scripts\python.exe -m waitress --listen=127.0.0.1:5000 --threads=4 --max-request-body-size=21037056 --max-request-header-size=16384 --channel-timeout=30 --inbuf-overflow=22020096 --outbuf-overflow=22020096 --no-log-socket-errors --call photo_app:create_app
```

The account commands prompt for a password and confirmation (minimum 12 characters). Visit **http://127.0.0.1:5000** and sign in. Dependencies and the verified Windows ExifTool distribution have already been installed in this workspace, and the database has been initialized. Create your own accounts before signing in.

ExifTool's Windows download includes its Perl runtime. The setup script downloads the pinned official archive from the project's SourceForge distribution, verifies its SHA-256 against the official HTTPS checksum file, and keeps `exiftool.exe` beside `exiftool_files`. Both are ignored under `tools/`. This verifies download integrity against the publisher's checksum; it is not a code-signing verification. See the [official installation instructions](https://exiftool.org/install.html).

On Linux/macOS, use a current ExifTool package (`libimage-exiftool-perl` on Debian/Ubuntu or `exiftool` with Homebrew), create a virtual environment, and use `.venv/bin/python` in the commands above. ExifTool must be on PATH or configured with `EXIFTOOL_PATH`. The Windows installer script is for x64 Windows only. Native codec support depends on the pillow-heif wheel/libheif build. Linux resource-limit code is included; runtime verification in this workspace was on Windows only.

## Verify the feature

1. Sign in as `uploader`, select a JPEG, PNG, WebP, or HEIC/HEIF photo, and upload it. The original is private; the library displays a separate JPEG copy.
2. Sign in as `admin` and select **View metadata**. Inspect raw capture time, capture offset/timezone status, optional normalized UTC time, separately labelled embedded dates, upload time, camera, image details, GPS, extraction status, warnings, and original-byte SHA-256.
3. Upload a photo with no EXIF. It should still upload, with missing values shown as **Not available**. An EXIF capture time without an offset stays timezone **Unknown**, with no UTC conversion.
4. As an uploader, try the admin page/API URL copied from the admin session: access is denied. Other users cannot access your display images.
5. Delete a photo to remove its original, derivative, and metadata together.

Run focused integration and unit tests, plus dependency checks:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m compileall -q photo_app tests scripts
```

Integration tests use generated photos containing EXIF, including real JPEG, PNG, WebP, and HEIF encoding/extraction. ExifTool must be installed; these tests deliberately fail with setup instructions if it is absent. Other tests cover absent EXIF, unknown/invalid/conflicting timezones, safe parsing failures, invalid/disallowed files, byte and pixel limits, CSRF, login throttling, admin-only GPS/metadata, cross-user access, HTML escaping, hash integrity, extraction ordering, worker timeout, retention, cleanup, deletion, and migration idempotence. Upload regressions include a real PNG larger than the multipart read buffer, files just below and exactly at 20 MiB, rejection one byte over that limit, and rejection of oversized text fields. The multipart memory cap is 512 KiB so its internal 64 KiB read buffer has room for boundary bytes; the photo size cap remains 20 MiB.

## Implementation and data flow

- `migrations/001_initial.sql`: transactional initial schema; `flask init-db` records version `001` and is idempotent. `photos` has a one-to-one `photo_metadata` record with cascade deletion, status, fields JSON, warnings JSON, upload UTC timestamp, original SHA-256, and safe error code.
- `photo_app/uploads.py`: authenticated stream receipt → byte cap and SHA-256 → file signature check → original-byte metadata extraction → isolated image validation/decode/optimization → private original/derivative persistence → atomic database insert. Temporary files and uncommitted destination files are cleaned up on failure.
- `photo_app/worker.py`, `process.py`: ExifTool runs without a shell, with a tag allowlist, disabled user config, generated server file paths, and bounded output. Image decoding is a separate worker after extraction. Windows Job Objects cap memory for the worker and its descendants and kill them together. POSIX workers use address-space/CPU/file-size limits and process-group termination. Parser exceptions and messages never reach application logs or public responses.
- `photo_app/metadata.py`: structured allowlisted fields, raw date preservation, offset validation, UTC normalization, GPS validation, and safe warning codes. Other date fields are never capture-time substitutes.
- `photo_app/auth.py`, `routes.py`: server-checked roles, scrypt password hashes, signed sessions, CSRF, a short login-throttling window, private image access, safe photo API serialization, and separate admin metadata endpoints. Jinja autoescapes metadata; the frontend uses `textContent` for filenames.
- `photo_app/templates/`, `static/`: login, upload library, admin details, and responsive styling. No third-party scripts, fonts, image services, or map providers receive metadata.
- `tests/`: focused generated-fixture tests; no personal photos are required.

### Date semantics

`fields.capture.raw` retains ExifTool's decoded EXIF `DateTimeOriginal` string unchanged. If EXIF has no original capture timestamp, an embedded XMP `DateTimeOriginal` can supply it, with its source explicitly labelled. Other kinds of dates never fill this field. The original bytes are also retained by default. `OffsetTimeOriginal` and `SubSecTimeOriginal` have separate raw fields. Capture UTC is computed only from a valid capture timestamp and its own explicit offset (or offset embedded in that timestamp). An EXIF offset is never attached to an XMP capture timestamp. No upload time, filesystem time, GPS location, GPS date, camera setting, or server timezone fills a capture date or timezone gap. Invalid raw capture strings are retained and warned about. Values over 512 characters are omitted with `METADATA_VALUE_TOO_LONG`; they are never silently truncated and described as raw.

Other selected embedded dates include EXIF digitized/modified dates with their own offsets, XMP original/created/modified/metadata dates, PNG creation time, HEIF container creation/modification dates, and GPS date/time stamps. These retain their source labels and are not used to infer EXIF capture time. The selected fields are not an exhaustive metadata dump.

Every admin view includes: **“Metadata is user-supplied and may have been modified. Capture date is not independently verified.”** The hash identifies the uploaded byte sequence; it does not authenticate EXIF or prove capture circumstances. Missing metadata does not identify an originating app or social network.

### Extraction statuses

| Status | Meaning |
| --- | --- |
| `extracted` | Selected embedded metadata was read. Individual fields may still be absent or invalid. |
| `missing` | Parsing succeeded but no selected embedded metadata was found. Basic file details are still stored. |
| `unsupported` | A signature-valid, decoder-valid upload was not recognized as supported by the metadata parser. |
| `failed` | Parsing failed, timed out, hit a resource/output limit, or the parser was unavailable. A safe code is stored. |

A valid image can upload with any of these statuses. An invalid, oversized, or disallowed image is rejected separately and has no photo record. Parser warning text is replaced with the safe `PARSER_WARNING` code. A warning with no readable selected embedded fields is treated as `failed` rather than confidently claiming metadata is absent. Unknown file signatures are rejected with HTTP 415; that rejection is distinct from a saved `unsupported` extraction record.

### Supported formats and limits

JPEG (including JPEG-based MPO/MPF multi-picture containers), PNG, WebP (including extended/lossless containers), and HEIC/HEIF are supported when the installed parser and decoder can read the file. Signatures and decoder agreement determine acceptance; uploaded names and MIME values do not. Pillow's `MPO` decoder label is accepted only for a JPEG signature. GIF, SVG, AVIF, PDF, raw camera files, and other formats are disallowed. Unusual HEIF containers without a recognized `ftyp` box in the first 4096 bytes are rejected. Only the primary image/frame is displayed for multi-frame files; the original is retained unchanged and a warning is recorded. Auxiliary JPEG images and MPF information are not copied into the display image.

Defaults: **20 MiB per original**, **40 million pixels**, **12 seconds per metadata/display worker**, **768 MiB per worker process tree**, **256 KiB parser output**, and **two concurrent upload pipelines per application process**. Normal upload time includes two sequential worker deadlines. The request/form limits also cap multipart overhead/parts. The decoder performs structural verification and a full primary-frame decode; truncated images and decompression bombs are rejected. Display copies are at most 1800 × 1800 pixels, orientation-corrected, flattened onto white where needed, and freshly encoded without EXIF, XMP, ICC, text, comments, or original trailing bytes. Color management/HDR fidelity is not preserved in these display copies.

### API

All endpoints require session authentication. Mutations require the CSRF token from the `csrf-token` meta element in `X-CSRF-Token` or the `csrf_token` form field.

| Endpoint | Access / response |
| --- | --- |
| `POST /api/photos` | Authenticated multipart `photo`; returns allowlisted photo details, never capture/device/GPS/hash metadata. |
| `GET /api/photos` | Own latest 100 photos, using the same safe serializer. |
| `GET /photos/<id>/display.jpg` | Owner or admin; sanitized derivative only. |
| `GET /admin/photos/<id>/metadata` | Admin-only HTML view. |
| `GET /api/admin/photos/<id>/metadata` | Admin-only structured metadata, including GPS. |
| `POST /photos/<id>/delete` | Owner or admin, CSRF protected; deletes stored photo assets and metadata. |

There is no endpoint for original downloads or a public metadata feed. HTML, API, and image responses use `Cache-Control: no-store`; session cookies are HTTP-only and SameSite=Lax.

## Privacy, retention, and operation

By default `instance/` contains the SQLite database, generated session secret, `originals/`, `display/`, and per-upload temporary directories. It is ignored by version control and is outside Flask's static directory. Keep it on an access-controlled disk, and do not configure a web server to serve that directory. POSIX directories are created with mode 0700; supported Windows Python versions apply owner-restricted permissions to newly created 0700 directories. Existing directories and host backups still need appropriate host access controls. SQLite metadata is sensitive and is not encrypted by this application.

Because this is a new project, the implemented retention policy is **retain until explicitly deleted**. Set `RETAIN_ORIGINALS=false` before starting the app to discard source files after extraction and display generation; this applies to new uploads and does not purge existing ones. Deleting a photo removes live original/derivative files and its database metadata; backup media and SQLite deleted-page/WAL remnants follow your host's separate retention policy. The application does not claim forensic secure deletion. Unexpected process termination can leave temporary or orphaned asset files; exceptions during a live request are cleaned up. Run a private data-directory cleanup under your operational retention policy after a crash, with the server stopped.

Configuration environment variables: `PHOTO_DATA_DIR` (absolute private data directory), `EXIFTOOL_PATH` (absolute executable path), `SECRET_KEY` (optional; otherwise generated privately and persisted), `RETAIN_ORIGINALS`, and `COOKIE_SECURE` (set `true` with HTTPS). Resource caps are application config values in `create_app`; deployments can override them in their own application factory. Use the same private database/data directory for the CLI and server. Run `init-db` before the first start. SQLite and a two-slot per-process queue suit a small, single-host deployment; horizontal scaling/object storage/background queues are not included.

Before internet deployment, place a request-limiting HTTPS reverse proxy in front of Waitress and restrict filesystem/service permissions. The provided Waitress command bounds request buffering and header/body sizes. Application-generated image/parser errors contain codes only. Keep ExifTool, Pillow/libheif, and framework dependencies current; parser isolation and resource caps are not a complete OS security sandbox. There is no email, password reset, self-registration, SSO, or automatic expiry workflow in this minimal application.
