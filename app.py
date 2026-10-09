"""PDF Toolkit - Flask app (Vercel compatible, koi file disk par save nahi hoti)."""
import io
import json
import os
import re
import zipfile
from functools import lru_cache

import requests
from flask import Flask, Response, abort, render_template, request, send_file
from PIL import Image, ImageDraw, ImageOps
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

SITE_NAME = 'PDF Toolkit'   # <- apni site ka naam yahan likhein
MAX_MB = 4                  # Vercel ki request limit ~4.5 MB hai

app = Flask(__name__, template_folder='templates', static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = MAX_MB * 1024 * 1024

# Token sirf environment variable se aayega, code mein kabhi nahi likhna.
API_TOKEN = os.environ.get('CONVERT_API_TOKEN')
CONVERT_API = 'https://v2.convertapi.com/convert'


class ToolError(Exception):
    """User ko dikhne wala saaf error."""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def base_name(filename):
    name = os.path.splitext(os.path.basename(filename or ''))[0]
    name = re.sub(r'[^\w\- ]+', '_', name, flags=re.UNICODE).strip(' _')
    return name[:60] or 'file'


def read_bytes(f):
    data = f.read()
    if not data:
        raise ToolError('The uploaded file is empty.')
    return data


def to_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def open_pdf(data):
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(''):
            raise ToolError('This PDF is password-protected. Use the Unlock PDF tool first.')
        len(reader.pages)
    except ToolError:
        raise
    except Exception:
        raise ToolError('This file does not look like a valid PDF.')
    return reader


def parse_pages(spec, total):
    """'1-3, 7, 10-12' -> [0,1,2,6,9,10,11] (0-based, likhe huye order mein)."""
    spec = (spec or '').replace(' ', '')
    pages = []
    for part in spec.split(','):
        if not part:
            continue
        m = re.fullmatch(r'(\d+)(?:-(\d+))?', part)
        if not m:
            raise ToolError(f'Could not understand "{part}". Use a format like 1-3, 5, 8-10.')
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if a < 1 or b < a or b > total:
            raise ToolError(f'Page "{part}" is outside this PDF (it has {total} pages).')
        pages.extend(range(a - 1, b))
    return pages


def pdf_bytes(writer):
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_overlay(w, h, draw):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(w, h))
    draw(c)
    c.save()
    buf.seek(0)
    return PdfReader(buf).pages[0]


def latin_only(text):
    try:
        text.encode('latin-1')
    except UnicodeEncodeError:
        raise ToolError('Text supports English letters, numbers and common symbols only.')


# --------------------------------------------------------------------------
# Tool handlers: handler(files, form) -> (bytes, download_name, extra_headers)
# --------------------------------------------------------------------------
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
    pages = parse_pages(form.get('pages'), total)
    if pages:
        writer = PdfWriter()
        for i in pages:
            writer.add_page(reader.pages[i])
        return pdf_bytes(writer), f'{name}_extracted.pdf', {}
    if total == 1:
        raise ToolError('This PDF has only one page, there is nothing to split.')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for i, page in enumerate(reader.pages, 1):
            w = PdfWriter()
            w.add_page(page)
            z.writestr(f'{name}_page_{i}.pdf', pdf_bytes(w))
    return buf.getvalue(), f'{name}_pages.zip', {}


QUALITY = {'basic': 80, 'recommended': 60, 'extreme': 35}


def do_compress(files, form):
    data = read_bytes(files[0])
    reader = open_pdf(data)
    quality = QUALITY.get(form.get('level'), 60)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    for page in writer.pages:
        try:
            for img in page.images:
                try:
                    img.replace(img.image, quality=quality)
                except Exception:
                    pass
        except Exception:
            pass
        page.compress_content_streams()
    try:
        writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
    except Exception:
        pass
    out = pdf_bytes(writer)
    if len(out) >= len(data):          # bari ho gayi to original hi wapas dein
        out = data
    saved = round((1 - len(out) / len(data)) * 100)
    headers = {
        'X-Saved-Percent': str(saved),
        'X-Original-Size': str(len(data)),
        'X-New-Size': str(len(out)),
    }
    return out, f'{base_name(files[0].filename)}_compressed.pdf', headers


def do_rotate(files, form):
    reader = open_pdf(read_bytes(files[0]))
    angle = to_int(form.get('angle'), 90)
    if angle not in (90, 180, 270):
        angle = 90
    chosen = set(parse_pages(form.get('pages'), len(reader.pages)))
    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        if not chosen or i in chosen:
            page.rotate(angle)
        writer.add_page(page)
    return pdf_bytes(writer), f'{base_name(files[0].filename)}_rotated.pdf', {}


def do_protect(files, form):
    password = form.get('password', '')
    if not password:
        raise ToolError('Please enter a password.')
    reader = open_pdf(read_bytes(files[0]))
    writer = PdfWriter()
    writer.append(reader)
    writer.encrypt(password, algorithm='AES-256')
    return pdf_bytes(writer), f'{base_name(files[0].filename)}_protected.pdf', {}


def do_unlock(files, form):
    data = read_bytes(files[0])
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception:
        raise ToolError('This file does not look like a valid PDF.')
    if not reader.is_encrypted:
        raise ToolError('This PDF is not password-protected.')
    if not reader.decrypt(form.get('password', '')):
        raise ToolError('Wrong password. Please try again.')
    writer = PdfWriter()
    writer.append(reader)
    return pdf_bytes(writer), f'{base_name(files[0].filename)}_unlocked.pdf', {}


def do_organize(files, form):
    reader = open_pdf(read_bytes(files[0]))
    total = len(reader.pages)
    pages = parse_pages(form.get('pages'), total)
    if not pages:
        raise ToolError('Enter the page numbers, for example 3,1,2 or 1-4.')
    if form.get('action') == 'delete':
        remove = set(pages)
        keep = [i for i in range(total) if i not in remove]
        if not keep:
            raise ToolError('You cannot delete every page of the PDF.')
    else:
        keep = pages
    writer = PdfWriter()
    for i in keep:
        writer.add_page(reader.pages[i])
    return pdf_bytes(writer), f'{base_name(files[0].filename)}_organized.pdf', {}


OPACITY = {'light': 0.15, 'medium': 0.3, 'strong': 0.5}


def do_watermark(files, form):
    text = (form.get('text') or '').strip()
    if not text:
        raise ToolError('Please enter the watermark text.')
    latin_only(text)
    opacity = OPACITY.get(form.get('strength'), 0.3)
    reader = open_pdf(read_bytes(files[0]))
    writer = PdfWriter()
    for page in reader.pages:
        w, h = float(page.mediabox.width), float(page.mediabox.height)
        diag = (w * w + h * h) ** 0.5
        size = max(18, min(120, diag * 0.7 / (max(len(text), 1) * 0.6)))

        def draw(c, w=w, h=h, size=size):
            c.setFillColorRGB(0.45, 0.45, 0.45, alpha=opacity)
            c.setFont('Helvetica-Bold', size)
            c.translate(w / 2, h / 2)
            c.rotate(45)
            c.drawCentredString(0, -size / 3, text)

        page.merge_page(make_overlay(w, h, draw))
        writer.add_page(page)
    return pdf_bytes(writer), f'{base_name(files[0].filename)}_watermarked.pdf', {}


def do_page_numbers(files, form):
    reader = open_pdf(read_bytes(files[0]))
    position = form.get('position', 'center')
    start = to_int(form.get('start'), 1)
    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        w, h = float(page.mediabox.width), float(page.mediabox.height)
        label = str(start + i)

        def draw(c, w=w, label=label):
            c.setFillColorRGB(0.2, 0.2, 0.2)
            c.setFont('Helvetica', 10)
            if position == 'left':
                c.drawString(36, 22, label)
            elif position == 'right':
                c.drawRightString(w - 36, 22, label)
            else:
                c.drawCentredString(w / 2, 22, label)

        page.merge_page(make_overlay(w, h, draw))
        writer.add_page(page)
    return pdf_bytes(writer), f'{base_name(files[0].filename)}_numbered.pdf', {}


def do_images_to_pdf(files, form):
    images = []
    for f in files:
        try:
            im = ImageOps.exif_transpose(Image.open(f.stream))
            if im.mode in ('RGBA', 'LA', 'P'):
                rgba = im.convert('RGBA')
                bg = Image.new('RGB', rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[3])
                im = bg
            else:
                im = im.convert('RGB')
        except Exception:
            raise ToolError(f'"{f.filename}" is not a valid image.')
        images.append(im)
    buf = io.BytesIO()
    images[0].save(buf, format='PDF', save_all=True, append_images=images[1:], resolution=100.0)
    name = f'{base_name(files[0].filename)}.pdf' if len(files) == 1 else 'images.pdf'
    return buf.getvalue(), name, {}


def make_api_handler(default_src, dst):
    def handler(files, form):
        if not API_TOKEN:
            raise ToolError('This converter is not set up yet (CONVERT_API_TOKEN is missing on the server).')
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
                timeout=100,
            )
        except requests.RequestException:
            raise ToolError('The conversion service is not reachable right now. Please try again.')
        if r.status_code != 200:
            raise ToolError('Conversion failed. The file may be damaged or not supported.')
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
            raise ToolError('Could not download the converted file. Please try again.')
        if not blobs:
            raise ToolError('The conversion returned no file.')
        if len(blobs) == 1:
            return blobs[0], f'{name}.{dst}', {}
        buf = io.BytesIO()           # jaise PDF -> JPG mein har page alag image
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            for i, blob in enumerate(blobs, 1):
                z.writestr(f'{name}_{i}.{dst}', blob)
        return buf.getvalue(), f'{name}_{dst}.zip', {}
    return handler


# --------------------------------------------------------------------------
# Tool catalogue
# --------------------------------------------------------------------------
def tool(slug, title, desc, icon, cat, accept, multiple=False, min_files=1, fields=None, note=None):
    return dict(slug=slug, title=title, desc=desc, icon=icon, cat=cat, accept=accept,
                multiple=multiple, min_files=min_files, fields=fields or [], note=note,
                button=title, label='file')


API_NOTE = 'This conversion is powered by ConvertAPI, so your file is sent to their servers for processing.'

TOOLS = [
    tool('merge', 'Merge PDF', 'Combine several PDFs into one. Drag to set the order.',
         '🧩', 'organize', '.pdf', multiple=True, min_files=2),
    tool('split', 'Split PDF', 'Extract pages or split every page into its own file.',
         '✂️', 'organize', '.pdf',
         fields=[dict(name='pages', type='text', label='Pages to extract (optional)',
                      placeholder='1-3, 7, 10-12',
                      hint='Leave empty to get every page as a separate file in a ZIP.')]),
    tool('compress', 'Compress PDF', 'Make your PDF smaller and easier to share.',
         '🗜️', 'organize', '.pdf',
         fields=[dict(name='level', type='select', label='Compression level',
                      options=[('recommended', 'Recommended - good quality, smaller size'),
                               ('basic', 'Basic - best quality'),
                               ('extreme', 'Extreme - smallest size')])]),
    tool('organize-pdf', 'Organize PDF', 'Reorder pages or delete the ones you do not need.',
         '🗂️', 'organize', '.pdf',
         fields=[dict(name='action', type='select', label='What do you want to do?',
                      options=[('reorder', 'Keep only these pages, in this order'),
                               ('delete', 'Delete these pages')]),
                 dict(name='pages', type='text', label='Page numbers', placeholder='3, 1, 2   or   1-4',
                      required=True)]),
    tool('rotate-pdf', 'Rotate PDF', 'Rotate all pages, or only the pages you choose.',
         '🔄', 'organize', '.pdf',
         fields=[dict(name='angle', type='select', label='Rotate by',
                      options=[('90', '90° clockwise'), ('180', '180°'), ('270', '90° counter-clockwise')]),
                 dict(name='pages', type='text', label='Pages (optional)', placeholder='1-3, 5',
                      hint='Leave empty to rotate every page.')]),
    tool('watermark-pdf', 'Add Watermark', 'Stamp text like CONFIDENTIAL or DRAFT across every page.',
         '💧', 'edit', '.pdf',
         fields=[dict(name='text', type='text', label='Watermark text', placeholder='CONFIDENTIAL',
                      required=True),
                 dict(name='strength', type='select', label='Strength',
                      options=[('medium', 'Medium'), ('light', 'Light'), ('strong', 'Strong')])]),
    tool('page-numbers', 'Add Page Numbers', 'Number the pages of your PDF in one click.',
         '🔢', 'edit', '.pdf',
         fields=[dict(name='position', type='select', label='Position',
                      options=[('center', 'Bottom center'), ('right', 'Bottom right'),
                               ('left', 'Bottom left')]),
                 dict(name='start', type='number', label='Start from', placeholder='1')]),
    tool('protect-pdf', 'Protect PDF', 'Lock your PDF with a password (AES-256 encryption).',
         '🔒', 'secure', '.pdf',
         fields=[dict(name='password', type='password', label='Choose a password', required=True,
                      hint='Remember it. We cannot recover a lost password.')]),
    tool('unlock-pdf', 'Unlock PDF', 'Remove the password from a PDF you own.',
         '🔓', 'secure', '.pdf',
         fields=[dict(name='password', type='password', label='Current password', required=True)]),
    tool('jpg-to-pdf', 'Images to PDF', 'Turn JPG and PNG images into a single PDF.',
         '🖼️', 'convert', '.jpg,.jpeg,.png', multiple=True),
    tool('word-to-pdf', 'Word to PDF', 'Convert DOC and DOCX files to PDF.',
         '📘', 'convert', '.docx,.doc', note=API_NOTE),
    tool('excel-to-pdf', 'Excel to PDF', 'Convert spreadsheets to PDF.',
         '📗', 'convert', '.xlsx,.xls', note=API_NOTE),
    tool('powerpoint-to-pdf', 'PowerPoint to PDF', 'Convert slideshows to PDF.',
         '📙', 'convert', '.pptx,.ppt', note=API_NOTE),
    tool('pdf-to-word', 'PDF to Word', 'Convert a PDF into an editable Word document.',
         '📝', 'convert', '.pdf', note=API_NOTE),
    tool('pdf-to-excel', 'PDF to Excel', 'Pull tables out of a PDF into a spreadsheet.',
         '📊', 'convert', '.pdf', note=API_NOTE),
    tool('pdf-to-powerpoint', 'PDF to PowerPoint', 'Turn a PDF into editable slides.',
         '📽️', 'convert', '.pdf', note=API_NOTE),
    tool('pdf-to-jpg', 'PDF to JPG', 'Save every PDF page as a JPG image.',
         '📷', 'convert', '.pdf', note=API_NOTE),
    tool('pdf-to-png', 'PDF to PNG', 'Save every PDF page as a PNG image.',
         '🌄', 'convert', '.pdf', note=API_NOTE),
]
TOOL_MAP = {t['slug']: t for t in TOOLS}

BUTTONS = {
    'merge': ('Merge PDFs', 'merged PDF'),
    'split': ('Split PDF', 'split result'),
    'compress': ('Compress PDF', 'compressed PDF'),
    'organize-pdf': ('Organize PDF', 'organized PDF'),
    'rotate-pdf': ('Rotate PDF', 'rotated PDF'),
    'watermark-pdf': ('Add Watermark', 'watermarked PDF'),
    'page-numbers': ('Add Page Numbers', 'numbered PDF'),
    'protect-pdf': ('Protect PDF', 'protected PDF'),
    'unlock-pdf': ('Unlock PDF', 'unlocked PDF'),
    'jpg-to-pdf': ('Convert to PDF', 'PDF'),
    'word-to-pdf': ('Convert to PDF', 'PDF'),
    'excel-to-pdf': ('Convert to PDF', 'PDF'),
    'powerpoint-to-pdf': ('Convert to PDF', 'PDF'),
    'pdf-to-word': ('Convert to Word', 'Word document'),
    'pdf-to-excel': ('Convert to Excel', 'Excel spreadsheet'),
    'pdf-to-powerpoint': ('Convert to PowerPoint', 'PowerPoint presentation'),
    'pdf-to-jpg': ('Convert to JPG', 'JPG images'),
    'pdf-to-png': ('Convert to PNG', 'PNG images'),
}
for _slug, (_button, _label) in BUTTONS.items():
    TOOL_MAP[_slug]['button'] = _button
    TOOL_MAP[_slug]['label'] = _label

CATS = [('all', 'All tools'), ('organize', 'Organize'), ('edit', 'Edit'),
        ('secure', 'Security'), ('convert', 'Convert')]

HANDLERS = {
    'merge': do_merge,
    'split': do_split,
    'compress': do_compress,
    'organize-pdf': do_organize,
    'rotate-pdf': do_rotate,
    'watermark-pdf': do_watermark,
    'page-numbers': do_page_numbers,
    'protect-pdf': do_protect,
    'unlock-pdf': do_unlock,
    'jpg-to-pdf': do_images_to_pdf,
    'word-to-pdf': make_api_handler('docx', 'pdf'),
    'excel-to-pdf': make_api_handler('xlsx', 'pdf'),
    'powerpoint-to-pdf': make_api_handler('pptx', 'pdf'),
    'pdf-to-word': make_api_handler('pdf', 'docx'),
    'pdf-to-excel': make_api_handler('pdf', 'xlsx'),
    'pdf-to-powerpoint': make_api_handler('pdf', 'pptx'),
    'pdf-to-jpg': make_api_handler('pdf', 'jpg'),
    'pdf-to-png': make_api_handler('pdf', 'png'),
}


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.context_processor
def inject_globals():
    return {'SITE_NAME': SITE_NAME}


@app.errorhandler(413)
def too_large(_e):
    return f'This file is too large. The limit on this site is {MAX_MB} MB.', 413


@app.after_request
def security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    return resp


@app.route('/')
def home():
    return render_template('index.html', tools=TOOLS, cats=CATS)


@lru_cache(maxsize=4)
def make_icon(size):
    img = Image.new('RGB', (size, size))
    px = img.load()
    c1, c2 = (91, 91, 240), (139, 92, 246)
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size)
            px[x, y] = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))
    d = ImageDraw.Draw(img)
    w, h = size * 0.46, size * 0.58
    x0, y0 = (size - w) / 2, (size - h) / 2
    fold = w * 0.3
    d.polygon([(x0, y0), (x0 + w - fold, y0), (x0 + w, y0 + fold), (x0 + w, y0 + h), (x0, y0 + h)],
              fill=(255, 255, 255))
    d.polygon([(x0 + w - fold, y0), (x0 + w - fold, y0 + fold), (x0 + w, y0 + fold)], fill=(210, 212, 245))
    for i in range(3):
        ly = y0 + h * (0.45 + i * 0.16)
        d.rounded_rectangle([x0 + w * 0.16, ly, x0 + w * 0.84, ly + size * 0.032], radius=size * 0.016,
                            fill=(139, 92, 246))
    buf = io.BytesIO()
    img.save(buf, 'PNG', optimize=True)
    return buf.getvalue()


@app.route('/icon-<int:size>.png')
def icon(size):
    if size not in (192, 512):
        abort(404)
    return send_file(io.BytesIO(make_icon(size)), mimetype='image/png', max_age=86400)


@app.route('/manifest.webmanifest')
def manifest():
    shortcuts = [{'name': TOOL_MAP[s]['title'], 'url': '/' + s,
                  'icons': [{'src': '/icon-192.png', 'sizes': '192x192', 'type': 'image/png'}]}
                 for s in ('merge', 'compress', 'pdf-to-word', 'jpg-to-pdf')]
    data = {
        'name': SITE_NAME, 'short_name': SITE_NAME[:12], 'start_url': '/', 'scope': '/',
        'display': 'standalone', 'background_color': '#f6f7fb', 'theme_color': '#5b5bf0',
        'description': 'Merge, split, compress, convert and protect PDFs.',
        'icons': [{'src': '/icon-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any maskable'},
                  {'src': '/icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any maskable'}],
        'shortcuts': shortcuts,
    }
    return Response(json.dumps(data), mimetype='application/manifest+json')


SERVICE_WORKER = """
const CACHE = 'shell-v1';
self.addEventListener('install', e => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then(c => c.add('/')).catch(() => {}));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const r = e.request;
  if (r.method !== 'GET') return;
  e.respondWith(
    fetch(r).then(res => {
      if (r.mode === 'navigate' && res.ok) {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(r, copy));
      }
      return res;
    }).catch(() => caches.match(r).then(m => m || caches.match('/')))
  );
});
"""


@app.route('/sw.js')
def service_worker():
    resp = Response(SERVICE_WORKER, mimetype='application/javascript')
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.route('/<slug>', methods=['GET', 'POST'])
def tool_page(slug):
    t = TOOL_MAP.get(slug)
    if not t:
        abort(404)

    if request.method == 'GET':
        # WHAT NEXT? section mein 'Word to PDF' aur 'PDF to Word' ko top priority par lane ka logic
        priority_slugs = ['word-to-pdf', 'pdf-to-word']
        
        # 1. Jo priority tools hain aur current page ka tool NAHI hain, unko sabse pehle add karein
        related = [x for x in TOOLS if x['slug'] in priority_slugs and x['slug'] != slug]
        
        # 2. Category ke baqi tools add karein
        related += [x for x in TOOLS if x['cat'] == t['cat'] and x['slug'] != slug and x['slug'] not in priority_slugs]
        
        # 3. Baqi bache hue tools add karein
        related += [x for x in TOOLS if x['slug'] != slug and x['slug'] not in priority_slugs and x['cat'] != t['cat']]
        
        return render_template('tool.html', tool=t, related=related[:3], max_mb=MAX_MB)

    files = [f for f in request.files.getlist('files') if f and f.filename]
    if not files:
        return 'Please choose a file first.', 400
    allowed = tuple(t['accept'].split(','))
    for f in files:
        if not f.filename.lower().endswith(allowed):
            return f'"{f.filename}" is not supported here. Allowed: {t["accept"]}', 400
    if not t['multiple']:
        files = files[:1]

    try:
        data, name, headers = HANDLERS[slug](files, request.form)
    except ToolError as e:
        return str(e), 400
    except Exception:
        app.logger.exception('Tool %s failed', slug)
        return 'Something went wrong while processing your file. Please try again.', 500

    resp = send_file(io.BytesIO(data), as_attachment=True, download_name=name)
    resp.headers.update(headers)
    resp.headers['Cache-Control'] = 'no-store'
    return resp


handler = app

if __name__ == '__main__':
    app.run(debug=True)
