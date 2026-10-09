import os
import io
import json
import uuid
import time
import requests
import zipfile
from flask import Flask, render_template, request, send_file, Response, abort
from pypdf import PdfReader, PdfWriter
from PIL import Image, ImageOps

SITE_NAME = 'PDF MASTAR'
MAX_MB = 4

app = Flask(__name__, template_folder='templates', static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = MAX_MB * 1024 * 1024

API_TOKEN = os.environ.get('CONVERT_API_TOKEN', 'TboQIRopPqPw8HlG4hlgULgHmKRK0jzW')
CONVERT_API = 'https://v2.convertapi.com/convert'

class ToolError(Exception):
    pass

def base_name(filename):
    return os.path.splitext(os.path.basename(filename or ''))[0][:60] or 'file'

def read_bytes(f):
    data = f.read()
    if not data: raise ToolError('The uploaded file is empty.')
    return data

def open_pdf(data):
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(''): raise ToolError('Password-protected PDF.')
        return reader
    except Exception:
        raise ToolError('Invalid PDF.')

def pdf_bytes(writer):
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()

def do_merge(files, form):
    if len(files) < 2: raise ToolError('Select at least 2 PDFs.')
    writer = PdfWriter()
    for f in files: writer.append(open_pdf(read_bytes(f)))
    return pdf_bytes(writer), 'merged.pdf', {}

def do_split(files, form):
    reader = open_pdf(read_bytes(files[0]))
    name = base_name(files[0].filename)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for i, page in enumerate(reader.pages, 1):
            w = PdfWriter(); w.add_page(page)
            z.writestr(f'{name}_page_{i}.pdf', pdf_bytes(w))
    return buf.getvalue(), f'{name}_pages.zip', {}

def do_compress(files, form):
    data = read_bytes(files[0])
    reader = open_pdf(data)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
        page.compress_content_streams()
    out = pdf_bytes(writer)
    return (out if len(out) < len(data) else data), f'{base_name(files[0].filename)}_compressed.pdf', {}

def do_images_to_pdf(files, form):
    images = []
    for f in files:
        im = ImageOps.exif_transpose(Image.open(f.stream)).convert('RGB')
        images.append(im)
    buf = io.BytesIO()
    images[0].save(buf, format='PDF', save_all=True, append_images=images[1:], resolution=100.0)
    return buf.getvalue(), 'images.pdf' if len(files) > 1 else f'{base_name(files[0].filename)}.pdf', {}

def make_api_handler(default_src, dst):
    def handler(files, form):
        f = files[0]
        data = read_bytes(f)
        src = os.path.splitext(f.filename)[1].lstrip('.').lower() or default_src
        name = base_name(f.filename)
        r = requests.post(f'{CONVERT_API}/{src}/to/{dst}', headers={'Authorization': f'Bearer {API_TOKEN}'},
                          files={'File': (f.filename, data)}, data={'StoreFile': 'true'}, timeout=100)
        if r.status_code != 200: raise ToolError('Conversion failed.')
        items = r.json().get('Files') or []
        blobs = [requests.get(item.get('Url') or item.get('url'), timeout=60).content for item in items if item.get('Url') or item.get('url')]
        if not blobs: raise ToolError('No file returned.')
        if len(blobs) == 1: return blobs[0], f'{name}.{dst}', {}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            for i, blob in enumerate(blobs, 1): z.writestr(f'{name}_{i}.{dst}', blob)
        return buf.getvalue(), f'{name}_{dst}.zip', {}
    return handler

TOOLS = [
    {'slug': 'merge', 'title': 'Merge PDF', 'desc': 'Combine multiple PDFs into one.', 'icon': '🧩'},
    {'slug': 'split', 'title': 'Split PDF', 'desc': 'Extract pages or split every page.', 'icon': '✂️'},
    {'slug': 'compress', 'title': 'Compress PDF', 'desc': 'Reduce PDF file size.', 'icon': '🗜️'},
    {'slug': 'pdf-to-word', 'title': 'PDF to Word', 'desc': 'Convert PDF to editable Word.', 'icon': '📝'},
    {'slug': 'word-to-pdf', 'title': 'Word to PDF', 'desc': 'Convert Word documents to PDF.', 'icon': '📘'},
    {'slug': 'pdf-to-excel', 'title': 'PDF to Excel', 'desc': 'Extract tables from PDF.', 'icon': '📊'},
    {'slug': 'excel-to-pdf', 'title': 'Excel to PDF', 'desc': 'Convert Excel to PDF.', 'icon': '📈'},
    {'slug': 'pdf-to-jpg', 'title': 'PDF to JPG', 'desc': 'Convert PDF pages to JPG.', 'icon': '🖼️'},
    {'slug': 'jpg-to-pdf', 'title': 'JPG to PDF', 'desc': 'Convert images to PDF.', 'icon': '🖼️'},
]

HANDLERS = {
    'merge': do_merge, 'split': do_split, 'compress': do_compress, 'jpg-to-pdf': do_images_to_pdf,
    'word-to-pdf': make_api_handler('docx', 'pdf'), 'pdf-to-word': make_api_handler('pdf', 'docx'),
    'excel-to-pdf': make_api_handler('xlsx', 'pdf'), 'pdf-to-excel': make_api_handler('pdf', 'xlsx'),
    'pdf-to-jpg': make_api_handler('pdf', 'jpg'),
}

@app.context_processor
def inject_globals():
    return {'SITE_NAME': SITE_NAME}

@app.route('/')
def home():
    return render_template('index.html', tools=TOOLS)

@app.route('/<slug>', methods=['GET', 'POST'])
def tool_page(slug):
    tool = next((t for t in TOOLS if t['slug'] == slug), None)
    if not tool: abort(404)
    if request.method == 'GET':
        return render_template('tool.html', tool=tool, max_mb=MAX_MB)
    
    files = [f for f in request.files.getlist('files') if f and f.filename]
    if not files: return 'Please choose a file first.', 400
    allowed = tuple(tool['slug'].replace('pdf-to-', '').replace('-to-pdf', '').split('-')[0] for _ in [1]) # Simplified check
    # Better check:
    ext_check = tool['slug']
    if 'pdf' in ext_check and 'jpg' in ext_check: allowed = ('.jpg', '.jpeg', '.png', '.pdf')
    elif 'word' in ext_check: allowed = ('.doc', '.docx', '.pdf')
    elif 'excel' in ext_check: allowed = ('.xls', '.xlsx', '.pdf')
    else: allowed = ('.pdf',)
    
    for f in files:
        if not f.filename.lower().endswith(allowed): return f'Not supported. Allowed: {allowed}', 400
    
    try:
        data, name, _ = HANDLERS[slug](files, request.form)
    except ToolError as e:
        return str(e), 400
    except Exception:
        return 'Something went wrong.', 500
        
    resp = send_file(io.BytesIO(data), as_attachment=True, download_name=name)
    resp.headers['Cache-Control'] = 'no-store'
    return resp

handler = app
if __name__ == '__main__': app.run(debug=True)