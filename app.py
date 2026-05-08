import os
import re
import sqlite3
import uuid
from collections import Counter
from datetime import datetime

from docx import Document
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from services.pdf_parser import extract_text

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
GENERATED_FOLDER = os.path.join(BASE_DIR, "generated")
DB_PATH = os.path.join(BASE_DIR, "app.db")

COMMON_SKILLS = [
    "python", "flask", "django", "react", "javascript", "html", "css",
    "mysql", "sql", "langchain", "rag", "chromadb", "vector database",
    "api", "rest", "rest api", "numpy", "pandas", "machine learning",
    "nlp", "docker", "git", "github", "pdf", "linux", "aws", "azure"
]

app = Flask(__name__)
CORS(app)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(GENERATED_FOLDER, exist_ok=True)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS resumes (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS analyses (
            id TEXT PRIMARY KEY,
            resume_id TEXT NOT NULL,
            job_description TEXT NOT NULL,
            match_score INTEGER NOT NULL,
            matched_keywords TEXT NOT NULL,
            missing_keywords TEXT NOT NULL,
            approved_additions TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (resume_id) REFERENCES resumes (id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS generated_files (
            id TEXT PRIMARY KEY,
            resume_id TEXT NOT NULL,
            file_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (resume_id) REFERENCES resumes (id)
        )
        """
    )
    conn.commit()
    conn.close()


def now_iso():
    return datetime.utcnow().isoformat()


def normalize(text):
    return re.sub(r"\s+", " ", text.lower()).strip()


def extract_keywords(text):
    text = normalize(text)
    found = []
    for skill in COMMON_SKILLS:
        if skill in text:
            found.append(skill)
    return sorted(set(found))


def build_resume_sections(raw_text):
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    headings = {
        "summary": ["summary", "profile", "objective"],
        "skills": ["skills", "technical skills", "core skills"],
        "experience": ["experience", "work experience", "professional experience", "internship"],
        "projects": ["projects", "project"],
        "education": ["education", "academics", "academic"]
    }

    sections = {key: [] for key in headings}
    current = None

    for line in lines:
        lowered = line.lower().rstrip(":")
        matched_heading = None
        for section, names in headings.items():
            if lowered in names:
                matched_heading = section
                break

        if matched_heading:
            current = matched_heading
            continue

        if current:
            sections[current].append(line)

    if not any(sections.values()):
        sections["summary"] = lines[:4]
        sections["skills"] = [line for line in lines if any(skill in normalize(line) for skill in COMMON_SKILLS)]
        sections["experience"] = lines[4:12]
        sections["projects"] = lines[12:18]
        sections["education"] = lines[-4:]

    return sections


def make_match_analysis(resume_text, job_description):
    resume_keywords = set(extract_keywords(resume_text))
    job_keywords = set(extract_keywords(job_description))
    matched = sorted(resume_keywords & job_keywords)
    missing = sorted(job_keywords - resume_keywords)

    if not job_keywords:
        score = 0
    else:
        score = round((len(matched) / max(len(job_keywords), 1)) * 100)

    return {
        "match_score": score,
        "matched_keywords": matched,
        "missing_keywords": missing,
    }


def build_tailored_resume(raw_text, job_description, approved_additions):
    sections = build_resume_sections(raw_text)
    resume_keywords = extract_keywords(raw_text)
    job_keywords = extract_keywords(job_description)

    additions = []
    for item in approved_additions or []:
        cleaned = normalize(str(item))
        if cleaned:
            additions.append(cleaned)
    additions = sorted(set(additions))

    skill_items = sorted(set(resume_keywords + additions))
    highlight_keywords = ", ".join(job_keywords[:8]) if job_keywords else "the target job"

    summary_lines = [
        "Tailored Summary",
        f"Motivated developer with experience aligned to {highlight_keywords}.",
        "Focused on building practical, job-ready solutions with Python, web technologies, and data-driven workflows.",
    ]

    skills_lines = ["Skills"]
    if skill_items:
        skills_lines.append(", ".join(skill_items))
    else:
        skills_lines.append("Python, Flask, React, SQL")

    experience_lines = ["Experience and Projects"]
    if sections["experience"]:
        experience_lines.extend(sections["experience"][:8])
    elif sections["projects"]:
        experience_lines.extend(sections["projects"][:8])
    else:
        experience_lines.extend(raw_text.splitlines()[:12])

    if additions:
        experience_lines.append("")
        experience_lines.append("Approved additions used after user permission:")
        experience_lines.append(", ".join(additions))

    education_lines = ["Education"]
    if sections["education"]:
        education_lines.extend(sections["education"][:6])

    final_text = "\n".join([
        *summary_lines,
        "",
        *skills_lines,
        "",
        *experience_lines,
        "",
        *education_lines,
    ]).strip()

    return final_text


def save_docx(text, output_path):
    doc = Document()
    for line in text.split("\n"):
        doc.add_paragraph(line)
    doc.save(output_path)
    return output_path


def save_pdf(text, output_path):
    c = canvas.Canvas(output_path, pagesize=letter)
    width, height = letter
    x = 50
    y = height - 50
    line_height = 14
    max_width = width - 100

    def write_line(line_text, y_pos):
        c.drawString(x, y_pos, line_text)

    for paragraph in text.split("\n"):
        if not paragraph.strip():
            y -= line_height
            if y < 60:
                c.showPage()
                y = height - 50
            continue

        words = paragraph.split()
        current_line = ""
        for word in words:
            candidate = (current_line + " " + word).strip()
            if stringWidth(candidate, "Helvetica", 10) <= max_width:
                current_line = candidate
            else:
                write_line(current_line, y)
                y -= line_height
                if y < 60:
                    c.showPage()
                    y = height - 50
                current_line = word
        if current_line:
            write_line(current_line, y)
            y -= line_height
            if y < 60:
                c.showPage()
                y = height - 50

    c.save()
    return output_path


def get_resume_or_404(resume_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM resumes WHERE id = ?", (resume_id,)).fetchone()
    conn.close()
    return row


@app.route("/")
def home():
    return """
    <html>
      <head>
        <title>Resume Optimizer AI</title>
        <style>
          body { font-family: Arial, sans-serif; margin: 40px; line-height: 1.6; }
          .card { max-width: 840px; padding: 24px; border: 1px solid #ddd; border-radius: 14px; }
          code { background: #f5f5f5; padding: 2px 6px; border-radius: 4px; }
        </style>
      </head>
      <body>
        <div class="card">
          <h1>Resume Optimizer AI</h1>
          <p>Backend is running correctly.</p>
          <p>Try <code>/health</code>, <code>/upload</code>, <code>/analyze</code>, <code>/rewrite</code>, and <code>/download/&lt;session_id&gt;</code>.</p>
        </div>
      </body>
    </html>
    """


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("file")
    if not file or file.filename == "":
        return jsonify({"error": "Please upload a PDF file"}), 400

    session_id = str(uuid.uuid4())
    safe_name = f"{session_id}_{file.filename}"
    path = os.path.join(UPLOAD_FOLDER, safe_name)
    file.save(path)

    raw_text = extract_text(path)

    conn = get_db()
    conn.execute(
        "INSERT INTO resumes (id, filename, raw_text, created_at) VALUES (?, ?, ?, ?)",
        (session_id, file.filename, raw_text, now_iso())
    )
    conn.commit()
    conn.close()

    suggestions = extract_keywords(raw_text)

    return jsonify({
        "session_id": session_id,
        "filename": file.filename,
        "resume_text_preview": raw_text[:1200],
        "detected_keywords": suggestions
    })


@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.get_json(force=True)
    session_id = data.get("session_id", "")
    job_description = data.get("job_description", "")

    resume_row = get_resume_or_404(session_id)
    if not resume_row:
        return jsonify({"error": "Resume not found"}), 404

    result = make_match_analysis(resume_row["raw_text"], job_description)
    approved_additions = result["missing_keywords"][:5]

    conn = get_db()
    conn.execute(
        """
        INSERT INTO analyses (id, resume_id, job_description, match_score, matched_keywords, missing_keywords, approved_additions, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            session_id,
            job_description,
            result["match_score"],
            ",".join(result["matched_keywords"]),
            ",".join(result["missing_keywords"]),
            ",".join(approved_additions),
            now_iso()
        )
    )
    conn.commit()
    conn.close()

    result["suggested_additions"] = approved_additions
    return jsonify(result)


@app.route("/rewrite", methods=["POST"])
def rewrite():
    data = request.get_json(force=True)
    session_id = data.get("session_id", "")
    job_description = data.get("job_description", "")
    approved_additions = data.get("approved_additions", [])
    output_format = (data.get("output_format") or "docx").lower()

    resume_row = get_resume_or_404(session_id)
    if not resume_row:
        return jsonify({"error": "Resume not found"}), 404

    rewritten_text = build_tailored_resume(
        resume_row["raw_text"],
        job_description,
        approved_additions
    )

    base_name = f"{session_id}_optimized_resume"
    docx_path = os.path.join(GENERATED_FOLDER, base_name + ".docx")
    pdf_path = os.path.join(GENERATED_FOLDER, base_name + ".pdf")

    save_docx(rewritten_text, docx_path)
    save_pdf(rewritten_text, pdf_path)

    conn = get_db()
    conn.execute(
        "INSERT INTO generated_files (id, resume_id, file_type, file_path, created_at) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), session_id, "docx", docx_path, now_iso())
    )
    conn.execute(
        "INSERT INTO generated_files (id, resume_id, file_type, file_path, created_at) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), session_id, "pdf", pdf_path, now_iso())
    )
    conn.commit()
    conn.close()

    return jsonify({
        "message": "resume rewritten",
        "rewritten_text": rewritten_text,
        "download_docx": f"/download/{session_id}?format=docx",
        "download_pdf": f"/download/{session_id}?format=pdf"
    })


@app.route("/download/<session_id>")
def download(session_id):
    file_format = request.args.get("format", "docx").lower()
    if file_format not in {"docx", "pdf"}:
        return jsonify({"error": "Invalid format"}), 400

    suffix = ".docx" if file_format == "docx" else ".pdf"
    file_path = os.path.join(GENERATED_FOLDER, f"{session_id}_optimized_resume{suffix}")
    if not os.path.exists(file_path):
        return jsonify({"error": "File not found"}), 404

    mimetype = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if file_format == "docx"
        else "application/pdf"
    )
    return send_file(file_path, as_attachment=True, mimetype=mimetype)


@app.route("/history/<session_id>")
def history(session_id):
    conn = get_db()
    resume = conn.execute("SELECT * FROM resumes WHERE id = ?", (session_id,)).fetchone()
    analysis = conn.execute(
        "SELECT * FROM analyses WHERE resume_id = ? ORDER BY created_at DESC LIMIT 1",
        (session_id,)
    ).fetchone()
    generated = conn.execute(
        "SELECT * FROM generated_files WHERE resume_id = ? ORDER BY created_at DESC",
        (session_id,)
    ).fetchall()
    conn.close()

    return jsonify({
        "resume": dict(resume) if resume else None,
        "analysis": dict(analysis) if analysis else None,
        "generated_files": [dict(row) for row in generated]
    })


init_db()

if __name__ == "__main__":
    app.run(debug=True)
