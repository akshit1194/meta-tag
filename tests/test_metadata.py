import pytest

from photo_app.metadata import capture_time, record_from_tags, signature_format
from photo_app.process import WorkerError, run_worker
from .conftest import image_bytes


@pytest.mark.parametrize(('raw', 'offset', 'status', 'utc'), [
    ('2024:01:02 00:10:00', '-02:00', 'embedded', '2024-01-02T02:10:00Z'),
    ('2024:01:02 00:10:00', '+02:00', 'embedded', '2024-01-01T22:10:00Z'),
    ('2024-01-02T00:10:00Z', None, 'embedded', '2024-01-02T00:10:00Z'),
    ('2024:01:02 00:10:00', None, 'unknown', None),
    ('2024:01:02 00:10:00', '+99:00', 'invalid', None),
    ('2024:01:02 00:10:00+01:00', '+02:00', 'conflicting', None),
    ('2024:02:31 00:10:00', '+00:00', 'embedded', None),
])
def test_capture_offsets_never_assume_timezone(raw, offset, status, utc):
    result, _ = capture_time(raw, offset)
    assert result['raw'] == raw
    assert result['timezone_status'] == status
    assert result['utc'] == utc


def test_other_dates_are_never_capture_fallbacks():
    record = record_from_tags({'IFD0:ModifyDate': '2024:01:01 12:00:00',
                               'ExifIFD:CreateDate': '2024:01:01 12:00:00'}, 'JPEG', 100)
    assert record['fields']['capture']['raw'] is None
    assert len(record['fields']['other_dates']) == 2


def test_xmp_original_time_uses_its_own_offset_only():
    record = record_from_tags({'XMP-exif:DateTimeOriginal': '2024-01-02T12:00:00',
                               'ExifIFD:OffsetTimeOriginal': '+05:00'}, 'JPEG', 100)
    capture = record['fields']['capture']
    assert capture['raw'] == '2024-01-02T12:00:00'
    assert capture['source'] == 'XMP-exif:DateTimeOriginal'
    assert capture['timezone_status'] == 'unknown' and capture['utc'] is None


def test_xmp_offset_is_normalized_and_zero_gps_is_valid():
    record = record_from_tags({'XMP-exif:DateTimeOriginal': '2024-01-02T12:00:00+03:00',
                               'XMP-exif:GPSLatitude': 0, 'XMP-exif:GPSLongitude': -2.5}, 'JPEG', 100)
    assert record['fields']['capture']['utc'] == '2024-01-02T09:00:00Z'
    assert record['fields']['gps'] == {'latitude': 0, 'longitude': -2.5}


def test_invalid_gps_and_long_values_are_not_invented():
    record = record_from_tags({'GPS:GPSLatitude': 91, 'GPS:GPSLatitudeRef': 'N',
                               'GPS:GPSLongitude': 10, 'IFD0:Make': 'x' * 513}, 'JPEG', 100)
    assert record['fields']['gps'] == {'latitude': None, 'longitude': None}
    assert record['fields']['camera']['make'] is None
    assert 'METADATA_VALUE_TOO_LONG' in record['warnings']


def test_avif_is_not_accepted_as_heif(tmp_path):
    path = tmp_path / 'fake.jpg'
    path.write_bytes((24).to_bytes(4, 'big') + b'ftypavif' + b'\0\0\0\0' + b'mif1heic')
    assert signature_format(path) is None


def test_worker_timeout_is_enforced(tmp_path):
    path = tmp_path / 'source.jpg'
    path.write_bytes(image_bytes())
    with pytest.raises(WorkerError, match='WORKER_TIMEOUT'):
        run_worker(dict(action='display', path=str(path), output=str(tmp_path / 'display.jpg'),
                        format='JPEG', max_pixels=100000), timeout=0.001)


def test_worker_still_rejects_actual_format_disagreement(tmp_path):
    path = tmp_path / 'source.jpg'
    path.write_bytes(image_bytes('JPEG'))
    assert signature_format(path) == 'JPEG'
    with pytest.raises(WorkerError, match='SIGNATURE_DECODER_MISMATCH'):
        run_worker(dict(action='display', path=str(path), output=str(tmp_path / 'display.jpg'),
                        format='PNG', max_pixels=100000))
    assert not (tmp_path / 'display.jpg').exists()


def test_migration_is_idempotent(app):
    from photo_app.db import get_db, migrate
    with app.app_context():
        migrate()
        migrate()
        assert get_db().execute('SELECT count(*) FROM schema_migrations').fetchone()[0] == 1
        assert get_db().execute('PRAGMA foreign_keys').fetchone()[0] == 1
