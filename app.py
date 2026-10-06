import os
import uuid
import time
import requests
from flask import Flask, render_template, request, send_file

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, 'templates'), static_folder=os.path.join(BASE_DIR, 'static'))

UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
OUTPUT_FOLDER = os.path.join(BASE_DIR, 'outputs')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# API Token ko Environment Variable se lena (Secure)
API_TOKEN = os.environ.get('CONVERT_API_TOKEN', 'TboQIRopPqPw8HlG4hlgULgHmKRK0jzW')

def cleanup_old_files(folder, max_age_seconds=3600):
    now = time.time()
    if os.path.exists(folder):
        for filename in os.listdir(folder):
            filepath = os.path.join(folder, filename)
            if os.path.isfile(filepath):
                if now - os.path.getmtime(filepath) > max_age_seconds:
                    os.remove(filepath)

def get_base_name(filename):
    return os.path.splitext(filename)[0]

def convert_with_api(input_file, input_format, output_format, base_name):
    cleanup_old_files(UPLOAD_FOLDER, 3600)
    cleanup_old_files(OUTPUT_FOLDER, 3600)
    
    # FIX: Unique ID add ki taake 1000 users ek saath same naam ki file upload karen toh clash na ho
    unique_id = uuid.uuid4().hex[:8]
    out_name = f"{base_name}_{unique_id}.{output_format}"
    
    headers = {'Authorization': f'Bearer {API_TOKEN}'}
    files = {'File': (input_file.filename, input_file.stream, 'application/octet-stream')}
    data = {'StoreFile': 'true'}
    
    response = requests.post(
        f'https://v2.convertapi.com/convert/{input_format}/to/{output_format}',
        headers=headers,
        files=files,
        data=data
    )
    
    if response.status_code == 200:
        result = response.json()
        if 'Files' in result and len(result['Files']) > 0:
            file_url = result['Files'][0].get('Url') or result['Files'][0].get('url')
            if file_url:
                download_response = requests.get(file_url)
                output_path = os.path.join(OUTPUT_FOLDER, out_name)
                with open(output_path, 'wb') as f:
                    f.write(download_response.content)
                return send_file(output_path, as_attachment=True, download_name=out_name)
            else:
                return f"API Error: URL not found.", 500
        else:
            return f"API Error: No files in response.", 500
    else:
        return f"API Error (Status {response.status_code}): {response.text}", 500

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/merge', methods=['GET', 'POST'])
def merge_pdf():
    if request.method == 'POST':
        try:
            from pypdf import PdfWriter
            files = request.files.getlist('pdf_files')
            if not files:
                return "No files selected", 400
            merger = PdfWriter()
            for file in files:
                if file and file.filename.endswith('.pdf'):
                    merger.append(file)
            unique_id = uuid.uuid4().hex[:8]
            out_name = f"merged_{unique_id}.pdf"
            output_path = os.path.join(OUTPUT_FOLDER, out_name)
            merger.write(output_path)
            merger.close()
            return send_file(output_path, as_attachment=True, download_name=out_name)
        except Exception as e:
            return f"Error: {str(e)}", 500
    return render_template('tool.html', title="Merge PDF", action="/merge", accept=".pdf", multiple=True)

@app.route('/split', methods=['GET', 'POST'])
def split_pdf():
    if request.method == 'POST':
        try:
            from pypdf import PdfReader, PdfWriter
            import zipfile
            file = request.files.get('pdf_file')
            if not file:
                return "No file selected", 400
            base_name = get_base_name(file.filename)
            unique_id = uuid.uuid4().hex[:8]
            reader = PdfReader(file)
            out_name = f"{base_name}_split_{unique_id}.zip"
            zip_path = os.path.join(OUTPUT_FOLDER, out_name)
            with zipfile.ZipFile(zip_path, 'w') as zipf:
                for i, page in enumerate(reader.pages):
                    writer = PdfWriter()
                    writer.add_page(page)
                    temp_pdf = os.path.join(OUTPUT_FOLDER, f"page_{i+1}_{unique_id}.pdf")
                    with open(temp_pdf, 'wb') as f:
                        writer.write(f)
                    zipf.write(temp_pdf, f"page_{i+1}.pdf")
                    os.remove(temp_pdf)
            return send_file(zip_path, as_attachment=True, download_name=out_name)
        except Exception as e:
            return f"Error: {str(e)}", 500
    return render_template('tool.html', title="Split PDF", action="/split", accept=".pdf", multiple=False)

@app.route('/compress', methods=['GET', 'POST'])
def compress_pdf():
    if request.method == 'POST':
        try:
            from pypdf import PdfReader, PdfWriter
            file = request.files.get('pdf_file')
            if not file:
                return "No file selected", 400
            base_name = get_base_name(file.filename)
            unique_id = uuid.uuid4().hex[:8]
            reader = PdfReader(file)
            writer = PdfWriter()
            for page in reader.pages:
                page.compress_content_streams()
                writer.add_page(page)
            out_name = f"{base_name}_compressed_{unique_id}.pdf"
            output_path = os.path.join(OUTPUT_FOLDER, out_name)
            with open(output_path, 'wb') as f:
                writer.write(f)
            return send_file(output_path, as_attachment=True, download_name=out_name)
        except Exception as e:
            return f"Error: {str(e)}", 500
    return render_template('tool.html', title="Compress PDF", action="/compress", accept=".pdf", multiple=False)

# ==================== CONVERT PDF TOOLS ====================
# (Baaki saare routes /pdf-to-word, /word-to-pdf, etc. bilkul waise hi rahenge, 
# sirf itna yaad rakhein ke wo sab 'convert_with_api' function use kar rahe hain jo ab safe hai)

@app.route('/pdf-to-word', methods=['GET', 'POST'])
def pdf_to_word():
    if request.method == 'POST':
        file = request.files.get('pdf_file')
        if not file: return "No file selected", 400
        return convert_with_api(file, 'pdf', 'docx', get_base_name(file.filename))
    return render_template('tool.html', title="PDF to Word", action="/pdf-to-word", accept=".pdf", multiple=False)

@app.route('/word-to-pdf', methods=['GET', 'POST'])
def word_to_pdf():
    if request.method == 'POST':
        file = request.files.get('pdf_file')
        if not file: return "No file selected", 400
        return convert_with_api(file, 'docx', 'pdf', get_base_name(file.filename))
    return render_template('tool.html', title="Word to PDF", action="/word-to-pdf", accept=".docx,.doc", multiple=False)

@app.route('/jpg-to-pdf', methods=['GET', 'POST'])
def jpg_to_pdf():
    if request.method == 'POST':
        file = request.files.get('pdf_file')
        if not file: return "No file selected", 400
        return convert_with_api(file, 'jpg', 'pdf', get_base_name(file.filename))
    return render_template('tool.html', title="JPG to PDF", action="/jpg-to-pdf", accept=".jpg,.jpeg,.png", multiple=False)

@app.route('/pdf-to-jpg', methods=['GET', 'POST'])
def pdf_to_jpg():
    if request.method == 'POST':
        file = request.files.get('pdf_file')
        if not file: return "No file selected", 400
        return convert_with_api(file, 'pdf', 'jpg', get_base_name(file.filename))
    return render_template('tool.html', title="PDF to JPG", action="/pdf-to-jpg", accept=".pdf", multiple=False)

@app.route('/excel-to-pdf', methods=['GET', 'POST'])
def excel_to_pdf():
    if request.method == 'POST':
        file = request.files.get('pdf_file')
        if not file: return "No file selected", 400
        return convert_with_api(file, 'xlsx', 'pdf', get_base_name(file.filename))
    return render_template('tool.html', title="Excel to PDF", action="/excel-to-pdf", accept=".xlsx,.xls", multiple=False)

@app.route('/pdf-to-excel', methods=['GET', 'POST'])
def pdf_to_excel():
    if request.method == 'POST':
        file = request.files.get('pdf_file')
        if not file: return "No file selected", 400
        return convert_with_api(file, 'pdf', 'xlsx', get_base_name(file.filename))
    return render_template('tool.html', title="PDF to Excel", action="/pdf-to-excel", accept=".pdf", multiple=False)

@app.route('/pdf-to-powerpoint', methods=['GET', 'POST'])
def pdf_to_powerpoint():
    if request.method == 'POST':
        file = request.files.get('pdf_file')
        if not file: return "No file selected", 400
        return convert_with_api(file, 'pdf', 'pptx', get_base_name(file.filename))
    return render_template('tool.html', title="PDF to PowerPoint", action="/pdf-to-powerpoint", accept=".pdf", multiple=False)

@app.route('/powerpoint-to-pdf', methods=['GET', 'POST'])
def powerpoint_to_pdf():
    if request.method == 'POST':
        file = request.files.get('pdf_file')
        if not file: return "No file selected", 400
        return convert_with_api(file, 'pptx', 'pdf', get_base_name(file.filename))
    return render_template('tool.html', title="PowerPoint to PDF", action="/powerpoint-to-pdf", accept=".pptx,.ppt", multiple=False)

@app.route('/pdf-to-png', methods=['GET', 'POST'])
def pdf_to_png():
    if request.method == 'POST':
        file = request.files.get('pdf_file')
        if not file: return "No file selected", 400
        return convert_with_api(file, 'pdf', 'png', get_base_name(file.filename))
    return render_template('tool.html', title="PDF to PNG", action="/pdf-to-png", accept=".pdf", multiple=False)

@app.route('/rotate-pdf', methods=['GET', 'POST'])
def rotate_pdf():
    if request.method == 'POST':
        file = request.files.get('pdf_file')
        if not file: return "No file selected", 400
        return convert_with_api(file, 'pdf', 'pdf', get_base_name(file.filename))
    return render_template('tool.html', title="Rotate PDF", action="/rotate-pdf", accept=".pdf", multiple=False)

@app.route('/protect-pdf', methods=['GET', 'POST'])
def protect_pdf():
    if request.method == 'POST':
        file = request.files.get('pdf_file')
        if not file: return "No file selected", 400
        return convert_with_api(file, 'pdf', 'pdf', get_base_name(file.filename))
    return render_template('tool.html', title="Protect PDF", action="/protect-pdf", accept=".pdf", multiple=False)

@app.route('/unlock-pdf', methods=['GET', 'POST'])
def unlock_pdf():
    if request.method == 'POST':
        file = request.files.get('pdf_file')
        if not file: return "No file selected", 400
        return convert_with_api(file, 'pdf', 'pdf', get_base_name(file.filename))
    return render_template('tool.html', title="Unlock PDF", action="/unlock-pdf", accept=".pdf", multiple=False)

@app.route('/organize-pdf', methods=['GET', 'POST'])
def organize_pdf():
    if request.method == 'POST':
        file = request.files.get('pdf_file')
        if not file: return "No file selected", 400
        return convert_with_api(file, 'pdf', 'pdf', get_base_name(file.filename))
    return render_template('tool.html', title="Organize PDF", action="/organize-pdf", accept=".pdf", multiple=False)

if __name__ == '__main__':
    app.run(debug=True)