import io

import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational
from werkzeug.security import generate_password_hash

from photo_app import create_app
from photo_app.db import get_db, migrate


@pytest.fixture
def app(tmp_path):
    app = create_app(dict(TESTING=True, SECRET_KEY='testing-only-secret', DATA_ROOT=tmp_path,
                          DATABASE=str(tmp_path / 'test.sqlite3')))
    with app.app_context():
        migrate()
        password = generate_password_hash('a-long-test-password')
        for username, role in (('admin', 'admin'), ('alice', 'user'), ('bob', 'user')):
            get_db().execute('INSERT INTO users(username,password_hash,role,created_at) VALUES (?,?,?,?)',
                             (username, password, role, '2026-01-01T00:00:00Z'))
        get_db().commit()
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def authorize(client, user_id=2):
    with client.session_transaction() as session:
        session['user_id'] = user_id
        session['csrf_token'] = 'test-csrf'


def image_bytes(image_format='JPEG', *, exif=False, offset=True, capture='2024:07:08 12:34:56', make='Example Camera'):
    photo = Image.new('RGB', (96, 64), (63, 122, 100))
    stream = io.BytesIO()
    options = {}
    if exif:
        tags = Image.Exif()
        tags[271], tags[272], tags[274] = make, 'Model One', 6
        tags[306] = '2024:07:09 10:00:00'
        exif_ifd = {36867: capture, 36868: '2024:07:08 12:35:00', 37521: '123', 36864: b'0232'}
        if offset:
            exif_ifd[36881] = '+05:30'
        tags[34665] = exif_ifd
        tags[34853] = {1: 'N', 2: (IFDRational(37), IFDRational(30), IFDRational(0)),
                       3: 'W', 4: (IFDRational(122), IFDRational(15), IFDRational(0))}
        options['exif'] = tags
    if image_format == 'HEIF':
        from pillow_heif import register_heif_opener
        register_heif_opener()
        options['quality'] = 30
    if image_format == 'MPO':
        options.update(save_all=True, append_images=[Image.new('RGB', (96, 64), 'blue')])
    photo.save(stream, format=image_format, **options)
    return stream.getvalue()


def upload(client, data=None, **extra):
    return client.post('/api/photos', data={
        'photo': (io.BytesIO(data if data is not None else image_bytes()), 'untrusted-name.jpg', 'text/plain'),
        'csrf_token': 'test-csrf', **extra,
    }, content_type='multipart/form-data')


def get_record(app, photo_id):
    import json
    with app.app_context():
        row = dict(get_db().execute('SELECT * FROM photo_metadata WHERE photo_id=?', (photo_id,)).fetchone())
    row['fields'] = json.loads(row.pop('fields_json'))
    row['warnings'] = json.loads(row.pop('warnings_json'))
    return row
