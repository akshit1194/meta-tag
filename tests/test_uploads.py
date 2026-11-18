import hashlib
import io
import json
import random
from datetime import datetime, timezone

import pytest
from PIL import Image

from photo_app.db import get_db
from photo_app.metadata import DISCLAIMER, discover_exiftool
from photo_app.process import WorkerError
from .conftest import authorize, get_record, image_bytes, upload


def require_parser():
    # Integration tests intentionally fail with an actionable message if setup is missing.
    assert discover_exiftool(), 'Install ExifTool before running integration tests; see README.md.'


@pytest.mark.parametrize('image_format', ['JPEG', 'PNG', 'WEBP', 'HEIF', 'MPO'])
def test_real_exif_original_bytes_and_private_display(app, client, image_format, caplog):
    require_parser()
    authorize(client)
    original = image_bytes(image_format, exif=True)
    before = datetime.now(timezone.utc)
    response = upload(client, original, DateTimeOriginal='1900:01:01 00:00:00', latitude='0')
    assert response.status_code == 201, response.get_data(as_text=True)
    photo_id = response.json['id']
    record = get_record(app, photo_id)
    if image_format == 'MPO':
        with Image.open(io.BytesIO(original)) as source:
            assert source.format == 'MPO' and source.n_frames == 2
        assert response.json['format'] == 'JPEG'
        assert 'PRIMARY_FRAME_DISPLAYED' in record['warnings']
    assert record['status'] == 'extracted'
    assert record['fields']['capture']['raw'] == '2024:07:08 12:34:56'
    assert record['fields']['capture']['timezone_status'] == 'embedded'
    assert record['fields']['capture']['utc'] == '2024-07-08T07:04:56.123000Z'
    assert record['fields']['camera'] == {'make': 'Example Camera', 'model': 'Model One'}
    assert record['fields']['gps'] == {'latitude': 37.5, 'longitude': -122.25}
    assert record['sha256'] == hashlib.sha256(original).hexdigest()
    assert datetime.fromisoformat(record['uploaded_at']) >= before
    assert record['fields']['image']['byte_size'] == len(original)
    assert (record['fields']['image']['width'], record['fields']['image']['height']) == (96, 64)
    assert record['fields']['image']['orientation'] == 6
    assert app.config['DATA_ROOT'].joinpath('originals', photo_id).read_bytes() == original
    display = client.get(response.json['display_url'])
    assert display.status_code == 200
    with Image.open(io.BytesIO(display.data)) as image:
        assert image.format == 'JPEG'
        assert getattr(image, 'n_frames', 1) == 1
        if image_format == 'MPO':
            # Frame zero is green; the auxiliary frame in this fixture is blue.
            red, green, blue = image.getpixel((image.width // 2, image.height // 2))
            assert green > red and green > blue
            assert not getattr(image, 'mpinfo', None)
        assert not image.getexif()
        assert not any(key in image.info for key in ('exif', 'xmp', 'icc_profile', 'comment'))
        if image_format != 'HEIF':
            assert image.size == (64, 96)  # Display rotation never alters original dimensions.
    assert 'GPS' not in json.dumps(response.json)
    assert set(response.json) == {'id', 'uploaded_at', 'format', 'width', 'height', 'byte_size', 'display_url'}
    assert 'Example Camera' not in caplog.text
    assert '-122.25' not in caplog.text
    assert list(app.config['DATA_ROOT'].joinpath('tmp').iterdir()) == []


def test_missing_exif_has_no_fabricated_capture_or_device(app, client):
    require_parser()
    authorize(client)
    response = upload(client)
    assert response.status_code == 201
    record = get_record(app, response.json['id'])
    assert record['status'] == 'missing'
    assert record['fields']['capture']['raw'] is None
    assert record['fields']['capture']['utc'] is None
    assert record['fields']['capture']['timezone_status'] == 'unknown'
    assert record['fields']['camera'] == {'make': None, 'model': None}
    assert record['fields']['gps'] == {'latitude': None, 'longitude': None}
    assert record['uploaded_at']
    authorize(client, 1)
    page = client.get('/admin/photos/' + response.json['id'] + '/metadata')
    assert b'Not available' in page.data
    assert b'Missing metadata' in page.data
    assert b'WhatsApp' not in page.data and b'Instagram' not in page.data


def test_capture_without_timezone_stays_unknown(app, client):
    authorize(client)
    response = upload(client, image_bytes(exif=True, offset=False))
    assert response.status_code == 201
    record = get_record(app, response.json['id'])
    capture = record['fields']['capture']
    assert capture['raw'] == '2024:07:08 12:34:56'
    assert capture['timezone_status'] == 'unknown'
    assert capture['utc'] is None and capture['offset_raw'] is None
    assert 'CAPTURE_TIMEZONE_UNKNOWN' in record['warnings']
    dates = record['fields']['other_dates']
    assert any(d['label'] == 'Digitized date (EXIF)' and d['raw'] == '2024:07:08 12:35:00' for d in dates)
    assert any(d['label'] == 'Modified date (EXIF)' for d in dates)


@pytest.mark.parametrize(('data', 'expected'), [(b'not an image', 415), (b'GIF89a' + b'\0' * 30, 415),
                                                (b'\x89PNG\r\n\x1a\nmalformed', 400), (b'\xff\xd8\xffmalformed', 400), (b'', 400)])
def test_malformed_and_unsupported_rejected_without_leaks(app, client, data, expected):
    authorize(client)
    response = upload(client, data)
    assert response.status_code == expected, response.json
    root = app.config['DATA_ROOT']
    for directory in ('tmp', 'originals', 'display'):
        assert list((root / directory).iterdir()) == []
    with app.app_context():
        assert get_db().execute('SELECT count(*) FROM photos').fetchone()[0] == 0


@pytest.mark.parametrize('code', ['PARSER_UNAVAILABLE', 'WORKER_TIMEOUT', 'PARSER_REJECTED_METADATA', 'PARSER_OUTPUT_LIMIT'])
def test_extraction_failure_does_not_reject_valid_upload(app, client, monkeypatch, code):
    authorize(client)
    def failed(*args, **kwargs):
        raise WorkerError(code)
    monkeypatch.setattr('photo_app.metadata.run_worker', failed)
    response = upload(client)
    assert response.status_code == 201
    record = get_record(app, response.json['id'])
    assert record['status'] == 'failed' and record['error_code'] == code
    assert record['fields']['capture']['raw'] is None
    assert client.get(response.json['display_url']).status_code == 200


def test_parser_unsupported_is_distinct_from_missing(app, client, monkeypatch):
    authorize(client)
    monkeypatch.setattr('photo_app.metadata.run_worker', lambda *a, **kw: {'tags': {'File:FileType': 'UNKNOWN'}})
    response = upload(client)
    assert response.status_code == 201
    record = get_record(app, response.json['id'])
    assert record['status'] == 'unsupported'
    assert record['error_code'] == 'PARSER_FORMAT_UNSUPPORTED'


def test_corrupt_exif_does_not_reject_valid_pixels(app, client):
    authorize(client)
    jpeg = image_bytes()
    malformed_exif = b'Exif\0\0not-a-tiff-header'
    app1 = b'\xff\xe1' + (len(malformed_exif) + 2).to_bytes(2, 'big') + malformed_exif
    response = upload(client, jpeg[:2] + app1 + jpeg[2:])
    assert response.status_code == 201, response.json
    record = get_record(app, response.json['id'])
    assert record['status'] == 'failed'
    assert record['error_code'] in ('PARSER_WARNING_WITHOUT_METADATA', 'PARSER_REJECTED_METADATA')
    assert client.get(response.json['display_url']).status_code == 200


def test_size_limit_busy_and_pixel_limit(app, client):
    authorize(client)
    app.config['MAX_UPLOAD_BYTES'] = 10
    assert upload(client).status_code == 413
    app.config['MAX_UPLOAD_BYTES'] = 20 * 1024 * 1024
    app.config['MAX_IMAGE_PIXELS'] = 30
    assert upload(client).json['error'] == 'IMAGE_PIXEL_LIMIT'
    slots = app.extensions['photo_slots']
    slots.acquire()
    slots.acquire()
    try:
        assert upload(client).status_code == 503
    finally:
        slots.release()
        slots.release()
    assert list((app.config['DATA_ROOT'] / 'tmp').iterdir()) == []


def test_retention_off_and_deletion(app, client):
    authorize(client)
    app.config['RETAIN_ORIGINALS'] = False
    response = upload(client)
    photo_id = response.json['id']
    assert response.status_code == 201
    assert not (app.config['DATA_ROOT'] / 'originals' / photo_id).exists()
    with app.app_context():
        assert get_db().execute('SELECT original_retained FROM photos WHERE id=?', (photo_id,)).fetchone()[0] == 0
    assert client.post(f'/photos/{photo_id}/delete', data={'csrf_token': 'test-csrf'}).status_code == 302
    assert not (app.config['DATA_ROOT'] / 'display' / (photo_id + '.jpg')).exists()
    with app.app_context():
        assert get_db().execute('SELECT count(*) FROM photo_metadata').fetchone()[0] == 0


def test_delete_removes_retained_original(app, client):
    authorize(client)
    response = upload(client)
    photo_id = response.json['id']
    assert (app.config['DATA_ROOT'] / 'originals' / photo_id).exists()
    assert client.post(f'/photos/{photo_id}/delete', data={'csrf_token': 'test-csrf'}).status_code == 302
    assert not (app.config['DATA_ROOT'] / 'originals' / photo_id).exists()


def test_invalid_capture_is_kept_as_raw_and_never_replaced(app, client):
    authorize(client)
    response = upload(client, image_bytes(exif=True, capture='2024:02:31 12:34:56'))
    assert response.status_code == 201
    record = get_record(app, response.json['id'])
    assert record['fields']['capture']['raw'] == '2024:02:31 12:34:56'
    assert record['fields']['capture']['utc'] is None
    assert 'INVALID_CAPTURE_TIMESTAMP' in record['warnings']


def test_request_body_limit_and_anonymous_upload(app, client):
    authorize(client)
    app.config['MAX_CONTENT_LENGTH'] = 50
    assert upload(client).status_code == 413
    app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024 + 64 * 1024
    with client.session_transaction() as session:
        session.pop('user_id')
    assert upload(client).status_code == 401
    assert list((app.config['DATA_ROOT'] / 'tmp').iterdir()) == []


def test_photo_larger_than_multipart_read_buffer_is_accepted(app, client):
    authorize(client)
    # A real, poorly compressible PNG exceeds the multipart reader's 64 KiB
    # chunk size while remaining far below the advertised 20 MiB file limit.
    photo = Image.frombytes('RGB', (512, 512), random.Random(0).randbytes(512 * 512 * 3))
    stream = io.BytesIO()
    photo.save(stream, 'PNG')
    original = stream.getvalue()
    assert 512 * 1024 < len(original) < app.config['MAX_UPLOAD_BYTES']
    response = upload(client, original)
    assert response.status_code == 201, response.json
    record = get_record(app, response.json['id'])
    assert record['sha256'] == hashlib.sha256(original).hexdigest()
    assert record['fields']['image']['byte_size'] == len(original)
    assert list((app.config['DATA_ROOT'] / 'tmp').iterdir()) == []


@pytest.mark.parametrize('byte_size', [20 * 1024 * 1024 - 1024, 20 * 1024 * 1024, 20 * 1024 * 1024 + 1])
def test_real_photo_byte_limit_includes_multipart_headroom(app, client, byte_size):
    authorize(client)
    # Valid JPEG pixels plus trailing padding exercise the request/file byte
    # boundary without creating a huge decoded image or a slow codec fixture.
    jpeg = image_bytes()
    original = jpeg + b'\0' * (byte_size - len(jpeg))
    response = upload(client, original)
    if byte_size > app.config['MAX_UPLOAD_BYTES']:
        assert response.status_code == 413
        assert response.json['error'] == 'FILE_TOO_LARGE'
        assert list((app.config['DATA_ROOT'] / 'originals').iterdir()) == []
    else:
        assert response.status_code == 201, response.json
        record = get_record(app, response.json['id'])
        assert record['fields']['image']['byte_size'] == byte_size
        assert record['sha256'] == hashlib.sha256(original).hexdigest()
    assert list((app.config['DATA_ROOT'] / 'tmp').iterdir()) == []


def test_oversized_multipart_text_field_is_still_rejected(app, client):
    authorize(client)
    response = upload(client, notes='x' * (app.config['MAX_FORM_MEMORY_SIZE'] + 1))
    assert response.status_code == 413
    assert list((app.config['DATA_ROOT'] / 'originals').iterdir()) == []


def test_admin_only_metadata_escaping_and_public_redaction(app, client):
    authorize(client)
    response = upload(client, image_bytes(exif=True, make='<script>alert(1)</script>'))
    assert response.status_code == 201
    photo_id = response.json['id']
    api = f'/api/admin/photos/{photo_id}/metadata'
    page = f'/admin/photos/{photo_id}/metadata'
    assert client.get(api).status_code == 403
    assert client.get(page).status_code == 403
    assert 'metadata' not in client.get('/api/photos').get_data(as_text=True)
    authorize(client, 3)
    assert client.get(response.json['display_url']).status_code == 404
    assert client.get(api).status_code == 403
    assert client.post(f'/photos/{photo_id}/delete', data={'csrf_token': 'test-csrf'}).status_code == 404
    authorize(client, 1)
    assert client.get(api).json['fields']['gps']['longitude'] == -122.25
    page_response = client.get(page)
    assert page_response.status_code == 200
    assert b'&lt;script&gt;alert(1)&lt;/script&gt;' in page_response.data
    assert b'<script>alert(1)</script>' not in page_response.data
    assert DISCLAIMER in page_response.get_data(as_text=True)
    assert page_response.headers['Cache-Control'] == 'no-store'
    anonymous = app.test_client()
    assert anonymous.get(api).status_code == 401
    assert anonymous.get(response.json['display_url']).status_code == 302
    assert anonymous.get('/static/../instance/originals/' + photo_id).status_code == 404


def test_csrf_and_real_login(client):
    response = client.get('/login')
    assert response.status_code == 200
    assert client.post('/login', data={'username': 'admin', 'password': 'a-long-test-password'}).status_code == 400
    with client.session_transaction() as session:
        csrf = session['csrf_token']
    response = client.post('/login', data={'username': 'admin', 'password': 'a-long-test-password', 'csrf_token': csrf})
    assert response.status_code == 302
    assert client.get('/').status_code == 200
    assert client.post('/api/photos', data={}).status_code == 400


def test_login_throttling(client):
    client.get('/login')
    with client.session_transaction() as session:
        csrf = session['csrf_token']
    for _ in range(5):
        assert client.post('/login', data={'username': 'alice', 'password': 'wrong', 'csrf_token': csrf}).status_code == 200
    assert client.post('/login', data={'username': 'alice', 'password': 'wrong', 'csrf_token': csrf}).status_code == 429


def test_ordering_extraction_precedes_display(app, client, monkeypatch):
    import photo_app.uploads as uploads
    original_extract, original_worker = uploads.extract_metadata, uploads.run_worker
    events = []
    def extract(*args, **kwargs):
        events.append('extract')
        return original_extract(*args, **kwargs)
    def display(*args, **kwargs):
        events.append('display')
        return original_worker(*args, **kwargs)
    monkeypatch.setattr(uploads, 'extract_metadata', extract)
    monkeypatch.setattr(uploads, 'run_worker', display)
    authorize(client)
    assert upload(client).status_code == 201
    assert events == ['extract', 'display']
