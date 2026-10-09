import os
import io
import json
import uuid
import time
import requests
import zipfile
from functools import lru_cache
from flask import Flask, render_template, request, send_file, Response, abort
from pypdf import PdfReader, PdfWriter
from PIL import Image, ImageDraw, ImageOps

SITE_NAME = 'PDF MASTAR'
MAX_MB = 4

app = Flask(__name__, template_folder='templates', static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = MAX_MB * 1024 * 1024

API_TOKEN = os.environ.get('CONVERT_API_TOKEN', 'TboQIRopPqPw8HlG4hlgULgHmKRK0jzW')
CONVERT_API = 'https://v2.convertapi.com/convert'

class ToolError(Exception):
    pass

def base_name(filename):
    name = os.path.splitext(os.path.basename(filename or ''))[0]
    return name[:60] or 'file'

def read_bytes(f):
    data = f.read()
    if not data:
        raise ToolError('The uploaded file is empty.')
    return data

def open_pdf(data):
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(''):
            raise ToolError('This PDF is password-protected.')
        len(reader.pages)
    except ToolError:
        raise
    except Exception:
        raise ToolError('This file does not look like a valid PDF.')
    return reader

def pdf_bytes(writer):
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()

def do_merge(files, form):
    if len(files) < 2:
        raise ToolError('Select at least 2 PDF files to merge.')
    writer = PdfWriter()
    for f in files:
        writer.append(open_pdf(read_bytes(f)))
    return pdf_bytes(writer), 'merged.pdf', {}

def do_split(files, form):
    reader = open_pdf(read_bytes(files[0]))
    total = len(reader.pages)
    name = base_name(files[0].filename)
    if total == 1:
        raise ToolError('This PDF has only one page.')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for i, page in enumerate(reader.pages, 1):
            w = PdfWriter()
            w.add_page(page)
            z.writestr(f'{name}_page_{i}.pdf', pdf_bytes(w))
    return buf.getvalue(), f'{name}_pages.zip', {}

def do_compress(files, form):
    data = read_bytes(files[0])
    reader = open_pdf(data)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    for page in writer.pages:
        page.compress_content_streams()
    out = pdf_bytes(writer)
    if len(out) >= len(data):
        out = data
    return out, f'{base_name(files[0].filename)}_compressed.pdf', {}

def do_images_to_pdf(files, form):
    images = []
    for f in files:
        try:
            im = ImageOps.exif_transpose(Image.open(f.stream))
            if im.mode in ('RGBA', 'LA', 'P'):
                bg = Image.new('RGB', im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[3] if len(im.split()) == 4 else None)
                im = bg
            else:
                im = im.convert('RGB')
        except Exception:
            raise ToolError(f'"{f.filename}" is not a valid image.')
        images.append(im)
    buf = io.BytesIO()
    images[0].save(buf, format='PDF', save_all=True, append_images=images[1:], resolution=100.0)
    return buf.getvalue(), 'images.pdf' if len(files) > 1 else f'{base_name(files[0].filename)}.pdf', {}

def make_api_handler(default_src, dst):
    def handler(files, form):
        if not API_TOKEN:
            raise ToolError('Converter not set up (CONVERT_API_TOKEN missing).')
        f = files[0]
        data = read_bytes(f)
        src = os.path.splitext(f.filename)[1].lstrip('.').lower() or default_src
        name = base_name(f.filename)
        try:
            r = requests.post(
                f'{CONVERT_API}/{src}/to/{dst}',
                headers={'Authorization': f'Bearer {API_TOKEN}'},
                files={'File': (f.filename, data)},
                data={'StoreFile': 'true'},
                timeout=100
            )
        except requests.RequestException:
            raise ToolError('Conversion service unreachable.')
        if r.status_code != 200:
            raise ToolError('Conversion failed.')
        try:
            items = r.json().get('Files') or []
            blobs = []
            for item in items:
                url = item.get('Url') or item.get('url')
                if url:
                    d = requests.get(url, timeout=60)
                    d.raise_for_status()
                    blobs.append(d.content)
        except Exception:
            raise ToolError('Could not download converted file.')
        if not blobs:
            raise ToolError('Conversion returned no file.')
        if len(blobs) == 1:
            return blobs[0], f'{name}.{dst}', {}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            for i, blob in enumerate(blobs, 1):
                z.writestr(f'{name}_{i}.{dst}', blob)
        return buf.getvalue(), f'{name}_{dst}.zip', {}
    return handler

def tool(slug, title, desc, icon, cat, accept, multiple=False, min_files=1, fields=None, note=None):
    return dict(slug=slug, title=title, desc=desc, icon=icon, cat=cat, accept=accept,
                multiple=multiple, min_files=min_files, fields=fields or [], note=note)

API_NOTE = 'Powered by ConvertAPI. File is sent to their servers for processing.'

TOOLS = [
    tool('merge', 'Merge PDF', 'Combine several PDFs into one.', '🧩', 'organize', '.pdf', multiple=True, min_files=2),
    tool('split', 'Split PDF', 'Extract pages or split every page.', '✂️', 'organize', '.pdf'),
    tool('compress', 'Compress PDF', 'Make your PDF smaller.', '🗜️', 'organize', '.pdf'),
    tool('jpg-to-pdf', 'Images to PDF', 'Turn JPG/PNG into a single PDF.', '🖼️', 'convert', '.jpg,.jpeg,.png', multiple=True),
    tool('word-to-pdf', 'Word to PDF', 'Convert DOC/DOCX to PDF.', '📘', 'convert', '.docx,.doc', note=API_NOTE),
    tool('pdf-to-word', 'PDF to Word', 'Convert PDF to editable Word.', '📝', 'convert', '.pdf', note=API_NOTE),
    tool('excel-to-pdf', 'Excel to PDF', 'Convert spreadsheets to PDF.', '📗', 'convert', '.xlsx,.xls', note=API_NOTE),
    tool('pdf-to-excel', 'PDF to Excel', 'Pull tables out of a PDF.', '📊', 'convert', '.pdf', note=API_NOTE),
    tool('pdf-to-jpg', 'PDF to JPG', 'Save every PDF page as JPG.', '📷', 'convert', '.pdf', note=API_NOTE),
]

TOOL_MAP = {t['slug']: t for t in TOOLS}

BUTTONS = {
    'merge': ('Merge PDFs', 'merged PDF'),
    'split': ('Split PDF', 'split PDFs'),
    'compress': ('Compress PDF', 'compressed PDF'),
    'jpg-to-pdf': ('Convert to PDF', 'PDF'),
    'word-to-pdf': ('Convert to PDF', 'PDF'),
    'pdf-to-word': ('Convert to Word', 'Word Document'),
    'excel-to-pdf': ('Convert to PDF', 'PDF'),
    'pdf-to-excel': ('Convert to Excel', 'Excel Spreadsheet'),
    'pdf-to-jpg': ('Convert to JPG', 'JPG Images'),
}

for _slug, (_button, _label) in BUTTONS.items():
    TOOL_MAP[_slug]['button'] = _button
    TOOL_MAP[_slug]['label'] = _label

CATS = [('all', 'All tools'), ('organize', 'Organize'), ('convert', 'Convert')]

HANDLERS = {
    'merge': do_merge,
    'split': do_split,
    'compress': do_compress,
    'jpg-to-pdf': do_images_to_pdf,
    'word-to-pdf': make_api_handler('docx', 'pdf'),
    'pdf-to-word': make_api_handler('pdf', 'docx'),
    'excel-to-pdf': make_api_handler('xlsx', 'pdf'),
    'pdf-to-excel': make_api_handler('pdf', 'xlsx'),
    'pdf-to-jpg': make_api_handler('pdf', 'jpg'),
}

@app.context_processor
def inject_globals():
    return {'SITE_NAME': SITE_NAME}

@app.route('/')
def home():
    return render_template('index.html', tools=TOOLS, cats=CATS)

@app.route('/<slug>', methods=['GET', 'POST'])
def tool_page(slug):
    t = TOOL_MAP.get(slug)
    if not t:
        abort(404)
    if request.method == 'GET':
        related = [x for x in TOOLS if x['cat'] == t['cat'] and x['slug'] != slug][:3]
        return render_template('tool.html', tool=t, related=related, max_mb=MAX_MB)
    
    files = [f for f in request.files.getlist('files') if f and f.filename]
    if not files:
        return 'Please choose a file first.', 400
    allowed = tuple(t['accept'].split(','))
    for f in files:
        if not f.filename.lower().endswith(allowed):
            return f'"{f.filename}" not supported. Allowed: {t["accept"]}', 400
    if not t['multiple']:
        files = files[:1]
    
    try:
        data, name, headers = HANDLERS[slug](files, request.form)
    except ToolError as e:
        return str(e), 400
    except Exception:
        return 'Something went wrong. Please try again.', 500
        
    resp = send_file(io.BytesIO(data), as_attachment=True, download_name=name)
    resp.headers.update(headers)
    resp.headers['Cache-Control'] = 'no-store'
    return resp

handler = app

if __name__ == '__main__':
    app.run(debug=True)
