import math
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .process import WorkerError, run_worker

DISCLAIMER = 'Metadata is user-supplied and may have been modified. Capture date is not independently verified.'
DATE = re.compile(r'^(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?$')
OFFSET = re.compile(r'^([+-])(\d{2}):(\d{2})$')


def server_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def discover_exiftool(configured=''):
    if configured:
        return str(Path(configured).resolve())
    local = Path(__file__).resolve().parent.parent / 'tools' / 'exiftool-path.txt'
    if local.exists():
        candidate = local.read_text(encoding='utf-8').strip()
        if Path(candidate).is_file():
            return candidate
    return shutil.which('exiftool') or ''


def signature_format(path):
    with open(path, 'rb') as file:
        head = file.read(4096)
    if head.startswith(b'\xff\xd8\xff'):
        return 'JPEG'
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'PNG'
    if len(head) >= 12 and head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'WEBP'
    if len(head) >= 16 and head[4:8] == b'ftyp':
        size = int.from_bytes(head[:4], 'big')
        if not 16 <= size <= len(head) or size % 4:
            return None
        brands = {head[8:12]} | {head[i:i+4] for i in range(16, size, 4)}
        if brands & {b'avif', b'avis'}:
            return None
        if brands & {b'heic', b'heix', b'hevc', b'hevx', b'heim', b'heis', b'hevm', b'hevs'}:
            return 'HEIC'
        if head[8:12] in (b'mif1', b'msf1'):
            return 'HEIF'
    return None


def text(value, warnings, limit=512):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    result = str(value)
    if len(result) > limit:
        warnings.append('METADATA_VALUE_TOO_LONG')
        # Do not call a truncated timestamp the raw timestamp.
        return None
    return result


def capture_time(raw, offset_raw=None, subseconds=None):
    result = dict(raw=raw, offset_raw=offset_raw, subseconds_raw=subseconds,
                  timezone_status='unknown', utc=None)
    warnings = []
    match = DATE.fullmatch(raw or '')
    embedded = match[8] if match else None
    offset = embedded or offset_raw
    if embedded and offset_raw and embedded != offset_raw:
        result['timezone_status'] = 'conflicting'
        return result, ['CONFLICTING_CAPTURE_OFFSETS']
    tz = None
    if offset == 'Z':
        tz = timezone.utc
    elif offset:
        parsed = OFFSET.fullmatch(offset)
        if parsed and int(parsed[2]) <= 14 and int(parsed[3]) < 60 and (int(parsed[2]) < 14 or int(parsed[3]) == 0):
            minutes = (int(parsed[2]) * 60 + int(parsed[3])) * (1 if parsed[1] == '+' else -1)
            tz = timezone(timedelta(minutes=minutes))
        else:
            result['timezone_status'] = 'invalid'
            warnings.append('INVALID_CAPTURE_OFFSET')
    if tz is not None:
        result['timezone_status'] = 'embedded'
    if raw and not match:
        warnings.append('INVALID_CAPTURE_TIMESTAMP')
    if match:
        fraction = match[7] or subseconds or ''
        if fraction and not fraction.isdigit():
            warnings.append('INVALID_CAPTURE_SUBSECONDS')
            return result, warnings
        try:
            date = datetime(*(int(match[i]) for i in range(1, 7)),
                            microsecond=int((fraction + '000000')[:6]), tzinfo=tz)
            if tz is not None:
                result['utc'] = date.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
        except (ValueError, OverflowError):
            warnings.append('INVALID_CAPTURE_TIMESTAMP')
    return result, warnings


def coordinate(tags, axis, warnings):
    value = tags.get(f'GPS:GPS{axis}')
    ref = tags.get(f'GPS:GPS{axis}Ref')
    if value is None:
        # XMP coordinate hemisphere is part of the value; ExifTool -n returns
        # its signed decimal value. EXIF coordinates instead require a ref tag.
        value = tags.get(f'XMP-exif:GPS{axis}')
        if value is None:
            return None
        try:
            number = float(value)
            if not math.isfinite(number) or abs(number) > (90 if axis == 'Latitude' else 180):
                raise ValueError
            return number
        except (TypeError, ValueError):
            warnings.append('INVALID_GPS_' + axis.upper())
            return None
    allowed = ('N', 'S') if axis == 'Latitude' else ('E', 'W')
    try:
        number = float(value)
        if ref not in allowed or not math.isfinite(number) or abs(number) > (90 if axis == 'Latitude' else 180):
            raise ValueError
    except (TypeError, ValueError):
        warnings.append('INVALID_GPS_' + axis.upper())
        return None
    return abs(number) * (-1 if ref in ('S', 'W') else 1)


def record_from_tags(tags, image_format, byte_size):
    warnings = []
    value = lambda key: text(tags.get(key), warnings)
    raw = value('ExifIFD:DateTimeOriginal')
    source = 'ExifIFD:DateTimeOriginal' if raw is not None else None
    offset, subseconds = value('ExifIFD:OffsetTimeOriginal'), value('ExifIFD:SubSecTimeOriginal')
    if raw is None:
        raw = value('XMP-exif:DateTimeOriginal')
        if raw is not None:
            source = 'XMP-exif:DateTimeOriginal'
            # Never attach an EXIF offset to a different XMP timestamp.
            offset, subseconds = None, None
    capture, date_warnings = capture_time(raw, offset, subseconds)
    capture['source'] = source
    warnings.extend(date_warnings)
    if raw and capture['timezone_status'] == 'unknown':
        warnings.append('CAPTURE_TIMEZONE_UNKNOWN')
    other_dates = []
    for key, label, offset_key in (
        ('ExifIFD:CreateDate', 'Digitized date (EXIF)', 'ExifIFD:OffsetTimeDigitized'),
        ('IFD0:ModifyDate', 'Modified date (EXIF)', 'ExifIFD:OffsetTime'),
        ('XMP-exif:DateTimeOriginal', 'Original date (XMP)', None),
        ('XMP-xmp:CreateDate', 'Created date (XMP)', None),
        ('XMP-xmp:ModifyDate', 'Modified date (XMP)', None),
        ('XMP-xmp:MetadataDate', 'Metadata date (XMP)', None),
        ('PNG:CreationTime', 'Creation time (PNG)', None),
        ('QuickTime:CreateDate', 'Container created date (HEIF; timezone unspecified)', None),
        ('QuickTime:ModifyDate', 'Container modified date (HEIF; timezone unspecified)', None),
        ('Keys:CreationDate', 'Creation date (HEIF)', None),
        ('GPS:GPSDateStamp', 'GPS date stamp', None),
        ('GPS:GPSTimeStamp', 'GPS time stamp', None),
    ):
        date_value = value(key)
        if date_value is not None:
            other_dates.append(dict(source=key, label=label, raw=date_value,
                                    offset_raw=value(offset_key) if offset_key else None))
    orientation = tags.get('IFD0:Orientation', tags.get('XMP-tiff:Orientation'))
    if orientation is not None and (type(orientation) is not int or not 1 <= orientation <= 8):
        warnings.append('INVALID_ORIENTATION')
        orientation = None
    fields = dict(
        capture=capture, other_dates=other_dates,
        camera=dict(make=value('IFD0:Make') or value('XMP-tiff:Make'),
                    model=value('IFD0:Model') or value('XMP-tiff:Model')),
        gps=dict(latitude=coordinate(tags, 'Latitude', warnings), longitude=coordinate(tags, 'Longitude', warnings)),
        image=dict(format=image_format, byte_size=byte_size, width=None, height=None, orientation=orientation),
        parser='ExifTool',
    )
    if tags.get('ExifTool:Warning'):
        warnings.append('PARSER_WARNING')  # Never store the parser's arbitrary text.
    embedded = any(k.startswith(('ExifIFD:', 'IFD0:', 'GPS:', 'XMP-', 'PNG:', 'QuickTime:', 'Keys:'))
                   and not k.endswith((':ImageWidth', ':ImageHeight')) for k in tags)
    status = 'extracted' if embedded else 'missing'
    error_code = None
    if not embedded and tags.get('ExifTool:Warning'):
        status, error_code = 'failed', 'PARSER_WARNING_WITHOUT_METADATA'
    return dict(status=status, fields=fields, warnings=sorted(set(warnings)), error_code=error_code)


def extract_metadata(path, image_format, byte_size, config):
    record = record_from_tags({}, image_format, byte_size)
    try:
        response = run_worker(
            dict(action='extract', path=str(path), exiftool=discover_exiftool(config['EXIFTOOL_PATH'])),
            timeout=config['WORKER_TIMEOUT'], memory_mb=config['WORKER_MEMORY_MB'],
        )
        tags = response['tags']
        # This extension is derived by ExifTool from bytes, never the upload name.
        parser_extension = tags.get('File:FileTypeExtension')
        parser_format = tags.get('File:FileType')
        supported = (parser_extension.lower() in ('jpg', 'jpeg', 'png', 'webp', 'heic', 'heif')
                     if isinstance(parser_extension, str) else
                     parser_format in (None, 'JPEG', 'PNG', 'WEBP', 'HEIC', 'HEIF'))
        # ExifTool can also name JPEG containers with an MPF image index MPO.
        # Accept that alias only when the original signature identifies JPEG.
        if image_format == 'JPEG' and (
            (isinstance(parser_extension, str) and parser_extension.lower() == 'mpo') or
            (parser_extension is None and parser_format == 'MPO')
        ):
            supported = True
        if not supported:
            record.update(status='unsupported', error_code='PARSER_FORMAT_UNSUPPORTED')
            return record
        return record_from_tags(tags, image_format, byte_size)
    except WorkerError as error:
        record.update(status='failed', error_code=error.code)
        return record
