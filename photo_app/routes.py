import json

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.exceptions import HTTPException

from .auth import admin_required, login_required
from .db import get_db
from .metadata import DISCLAIMER
from .uploads import UploadError, save_upload

bp = Blueprint('photos', __name__)

STATUS_LABELS = {'extracted': 'Extracted', 'missing': 'Missing metadata',
                 'unsupported': 'Unsupported by parser', 'failed': 'Extraction failed'}
WARNING_LABELS = {
    'CAPTURE_TIMEZONE_UNKNOWN': 'The capture timezone is unknown. No UTC capture time was calculated.',
    'INVALID_CAPTURE_TIMESTAMP': 'The embedded capture timestamp is invalid; the raw value is preserved.',
    'INVALID_CAPTURE_OFFSET': 'The embedded capture offset is invalid.',
    'CONFLICTING_CAPTURE_OFFSETS': 'Embedded capture offsets conflict. No UTC capture time was calculated.',
    'INVALID_CAPTURE_SUBSECONDS': 'Embedded capture subseconds are invalid.',
    'METADATA_VALUE_TOO_LONG': 'A metadata value exceeded the storage limit and was omitted.',
    'PARSER_WARNING': 'The parser reported a warning. Available fields were retained.',
    'INVALID_ORIENTATION': 'The embedded orientation is invalid.',
    'INVALID_GPS_LATITUDE': 'The embedded GPS latitude is invalid or lacks a hemisphere.',
    'INVALID_GPS_LONGITUDE': 'The embedded GPS longitude is invalid or lacks a hemisphere.',
    'PRIMARY_FRAME_DISPLAYED': 'This file has multiple frames. The display image uses the primary frame.',
}


def find_photo(photo_id):
    # Every route, including display files, checks current database authorization.
    row = get_db().execute('SELECT * FROM photos WHERE id=?', (photo_id,)).fetchone()
    if not row or (g.user['role'] != 'admin' and row['owner_id'] != g.user['id']):
        abort(404)
    return row


def safe_photo(row):
    # Explicit allowlist. No original names, hashes, dates, GPS, or parser output.
    return dict(id=row['id'], uploaded_at=row['uploaded_at'], format=row['format'],
                width=row['width'], height=row['height'], byte_size=row['byte_size'],
                display_url=url_for('photos.display_image', photo_id=row['id']))


@bp.get('/')
@login_required
def index():
    db = get_db()
    if g.user['role'] == 'admin':
        rows = db.execute('SELECT * FROM photos ORDER BY uploaded_at DESC LIMIT 100').fetchall()
    else:
        rows = db.execute('SELECT * FROM photos WHERE owner_id=? ORDER BY uploaded_at DESC LIMIT 100',
                          (g.user['id'],)).fetchall()
    return render_template('index.html', photos=rows)


@bp.post('/upload')
@bp.post('/api/photos')
@login_required
def upload():
    file = request.files.get('photo')
    if file is None:
        raise UploadError('PHOTO_REQUIRED')
    photo_id = save_upload(file, g.user['id'])
    if request.path.startswith('/api/'):
        return jsonify(safe_photo(find_photo(photo_id))), 201
    flash('Photo uploaded. A private display copy is ready.')
    return redirect(url_for('photos.index'))


@bp.get('/api/photos')
@login_required
def list_photos():
    # An admin dashboard has a separate endpoint; this API always lists own photos.
    rows = get_db().execute('SELECT * FROM photos WHERE owner_id=? ORDER BY uploaded_at DESC LIMIT 100',
                            (g.user['id'],)).fetchall()
    return jsonify(photos=[safe_photo(row) for row in rows])


@bp.get('/photos/<photo_id>/display.jpg')
@login_required
def display_image(photo_id):
    row = find_photo(photo_id)
    return send_file(current_app.config['DATA_ROOT'] / 'display' / (row['id'] + '.jpg'),
                     mimetype='image/jpeg', conditional=False, etag=False)


def metadata_record(photo_id):
    photo = find_photo(photo_id)
    row = get_db().execute('SELECT * FROM photo_metadata WHERE photo_id=?', (photo_id,)).fetchone()
    if not row:
        abort(404)
    data = dict(photo_id=photo_id, status=row['status'], uploaded_at=row['uploaded_at'],
                sha256=row['sha256'], fields=json.loads(row['fields_json']),
                warnings=json.loads(row['warnings_json']), error_code=row['error_code'], disclaimer=DISCLAIMER)
    return photo, data


@bp.get('/admin/photos/<photo_id>/metadata')
@admin_required
def metadata_view(photo_id):
    photo, data = metadata_record(photo_id)
    return render_template('metadata.html', photo=photo, metadata=data,
                           status_labels=STATUS_LABELS, warning_labels=WARNING_LABELS)


@bp.get('/api/admin/photos/<photo_id>/metadata')
@admin_required
def metadata_api(photo_id):
    _, data = metadata_record(photo_id)
    return jsonify(data)


@bp.post('/photos/<photo_id>/delete')
@login_required
def delete_photo(photo_id):
    row = find_photo(photo_id)
    # Remove originals, derivatives, and linked metadata together under the retention policy.
    root = current_app.config['DATA_ROOT']
    (root / 'originals' / row['id']).unlink(missing_ok=True)
    (root / 'display' / (row['id'] + '.jpg')).unlink(missing_ok=True)
    db = get_db()
    db.execute('DELETE FROM photos WHERE id=?', (row['id'],))
    db.commit()
    flash('Photo and its stored metadata deleted.')
    return redirect(url_for('photos.index'))


@bp.app_errorhandler(UploadError)
def upload_error(error):
    if request.path.startswith('/api/'):
        return jsonify(error=error.code), error.status
    return render_template('error.html', code=error.status, message=error.code.replace('_', ' ').capitalize()), error.status


@bp.app_errorhandler(HTTPException)
def http_error(error):
    if request.path.startswith('/api/'):
        return jsonify(error=error.name.upper().replace(' ', '_')), error.code
    return render_template('error.html', code=error.code, message=error.description), error.code
