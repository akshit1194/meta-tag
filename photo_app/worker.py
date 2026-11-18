"""Private worker protocol; never import image libraries before resource limits."""
import json
import os
import subprocess
import sys
import warnings


TAGS = [
    'EXIF:DateTimeOriginal', 'EXIF:OffsetTimeOriginal', 'EXIF:SubSecTimeOriginal',
    'EXIF:CreateDate', 'EXIF:ModifyDate', 'EXIF:OffsetTime', 'EXIF:OffsetTimeDigitized',
    'EXIF:Make', 'EXIF:Model', 'EXIF:Orientation', 'EXIF:ExifVersion',
    'EXIF:GPSLatitude', 'EXIF:GPSLatitudeRef', 'EXIF:GPSLongitude', 'EXIF:GPSLongitudeRef',
    'EXIF:GPSDateStamp', 'EXIF:GPSTimeStamp',
    'XMP-exif:DateTimeOriginal', 'XMP-tiff:Make', 'XMP-tiff:Model', 'XMP-tiff:Orientation',
    'XMP-exif:GPSLatitude', 'XMP-exif:GPSLongitude', 'XMP-xmp:CreateDate', 'XMP-xmp:ModifyDate',
    'XMP-xmp:MetadataDate', 'PNG:CreationTime', 'QuickTime:CreateDate',
    'QuickTime:ModifyDate', 'QuickTime:CreationDate', 'ImageWidth', 'ImageHeight',
    'File:FileType', 'File:FileTypeExtension', 'ExifTool:Warning', 'ExifTool:Error',
]


def extract(data):
    if not data.get('exiftool'):
        return {'error': 'PARSER_UNAVAILABLE'}
    command = [data['exiftool'], '-config', '', '-json', '-n', '-G1', '-a', '-s', '-charset', 'filename=UTF8']
    command += ['-' + tag for tag in TAGS]
    command += ['--', data['path']]
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, shell=False,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except OSError:
        return {'error': 'PARSER_UNAVAILABLE'}
    try:
        output = process.stdout.read(256 * 1024 + 1)
        if len(output) > 256 * 1024:
            return {'error': 'PARSER_OUTPUT_LIMIT'}
        process.wait()  # Parent enforces the deadline for the entire process tree.
        try:
            records = json.loads(output)
            tags = records[0]
        except (ValueError, IndexError, TypeError):
            return {'error': 'PARSER_INVALID_RESPONSE'}
        if not isinstance(tags, dict):
            return {'error': 'PARSER_INVALID_RESPONSE'}
        if tags.get('ExifTool:Error') or process.returncode != 0:
            return {'error': 'PARSER_REJECTED_METADATA'}
        tags.pop('SourceFile', None)
        return {'tags': tags}
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def display(data):
    from PIL import Image, UnidentifiedImageError
    from pillow_heif import register_heif_opener

    register_heif_opener(thumbnails=False)
    Image.MAX_IMAGE_PIXELS = data['max_pixels']
    warnings.simplefilter('error', Image.DecompressionBombWarning)
    try:
        with Image.open(data['path']) as probe:
            probe.verify()
        with Image.open(data['path']) as image:
            expected = 'HEIF' if data['format'] in ('HEIC', 'HEIF') else data['format']
            # Pillow's JPEG factory identifies JPEG files with an MPF image
            # index as MPO. That is a JPEG container, not a signature mismatch.
            jpeg_mpo = expected == 'JPEG' and image.format == 'MPO'
            if image.format != expected and not jpeg_mpo:
                return {'error': 'SIGNATURE_DECODER_MISMATCH'}
            width, height = image.size
            if width < 1 or height < 1 or width * height > data['max_pixels']:
                return {'error': 'IMAGE_PIXEL_LIMIT'}
            # HEIF may contain multiple photos; only the primary frame is displayed.
            animated = getattr(image, 'n_frames', 1) > 1
            image.load()  # Full decode validates pixels; truncated images are disallowed.
            # Use the already validated orientation from extraction; do not run
            # another EXIF parser during display generation. Bad EXIF must not
            # turn a valid pixel image into a rejected upload. libheif applies
            # its own container transformations when decoding HEIF.
            transforms = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180,
                          4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE,
                          6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
                          8: Image.Transpose.ROTATE_90}
            transform = transforms.get(data.get('orientation')) if expected != 'HEIF' else None
            oriented = image.transpose(transform) if transform is not None else image
            oriented.thumbnail((1800, 1800))
            rgba = oriented.convert('RGBA')
            # New pixel buffer: do not copy EXIF, XMP, ICC, comments, or text chunks.
            clean = Image.new('RGB', rgba.size, 'white')
            clean.paste(rgba, mask=rgba.getchannel('A'))
            clean.save(data['output'], 'JPEG', quality=85, optimize=True)
        return {'width': width, 'height': height, 'multiple_frames': animated}
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        return {'error': 'IMAGE_PIXEL_LIMIT'}
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        return {'error': 'INVALID_IMAGE'}


def main():
    data = json.loads(sys.stdin.buffer.read(32_768))
    if os.name != 'nt':
        import resource
        cap = data['memory_mb'] * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
        resource.setrlimit(resource.RLIMIT_CPU, (data['cpu_seconds'], data['cpu_seconds']))
        resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024, 8 * 1024 * 1024))
    result = extract(data) if data['action'] == 'extract' else display(data)
    sys.stdout.write(json.dumps(result, allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Parser messages may contain user data and paths. Never print them.
        sys.stdout.write(json.dumps({'error': 'WORKER_FAILED'}))
