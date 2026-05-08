import os
import re
import sqlite3
import uuid
from datetime import datetime
from xml.sax.saxutils import escape

import fitz
from docx import Document
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

from services.pdf_parser import extract_text

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
GENERATED_FOLDER = os.path.join(BASE_DIR, "generated")
DB_PATH = os.path.join(BASE_DIR, "app.db")
URL_RE = re.compile(r"(https?://[^\s<>\"]+)")
MAX_PDF_PAGES = 1

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


def linkify_text(text):
    escaped = escape(text)

    def replace(match):
        url = match.group(1)
        safe_url = escape(url)
        return f'<a href="{safe_url}" color="#2563eb">{safe_url}</a>'

    return URL_RE.sub(replace, escaped)


def extract_keywords(text):
    text = normalize(text)
    found = []
    for skill in COMMON_SKILLS:
        if skill in text:
            found.append(skill)
    return sorted(set(found))


def apply_removals(raw_text, remove_terms):
    terms = [normalize(str(term)) for term in (remove_terms or []) if normalize(str(term))]
    if not terms:
        return raw_text.strip(), []

    kept_lines = []
    removed_lines = []

    for line in raw_text.splitlines():
        normalized_line = normalize(line)
        if line.strip() and any(term in normalized_line for term in terms):
            removed_lines.append(line)
            continue
        kept_lines.append(line)

    return "\n".join(kept_lines).strip(), removed_lines


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


def suggest_removal_candidates(raw_text):
    sections = build_resume_sections(raw_text)
    ordered_sections = ["certifications", "projects", "skills", "education"]
    candidates = []

    for section in ordered_sections:
        items = sections.get(section, [])
        if not items:
            continue
        candidates.append(f"Remove {section} section")
        for item in items[:3]:
            cleaned = item.strip()
            if cleaned:
                candidates.append(cleaned)

    if not candidates:
        fallback = [line.strip() for line in raw_text.splitlines() if line.strip()]
        candidates.extend(fallback[-6:])

    deduped = []
    seen = set()
    for item in candidates:
        key = normalize(item)
        if key and key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped[:12]


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
    additions = []
    for item in approved_additions or []:
        cleaned = normalize(str(item))
        if cleaned:
            additions.append(cleaned)
    additions = sorted(set(additions))

    base_lines = [line.rstrip() for line in raw_text.splitlines() if line.strip()]
    if not additions:
        return "\n".join(base_lines).strip()

    # Insert approved additions into the skills area when possible so the resume
    # stays close to the uploaded layout instead of adding a separate appendix.
    skills_heading_idx = None
    for index, line in enumerate(base_lines):
        if normalize(line) in {"skills", "technical skills", "core skills"}:
            skills_heading_idx = index
            break

    if skills_heading_idx is not None:
        insert_at = skills_heading_idx + 1
        while insert_at < len(base_lines) and base_lines[insert_at].strip():
            next_line = normalize(base_lines[insert_at])
            if next_line in {"experience", "projects", "education", "certifications"}:
                break
            insert_at += 1
        base_lines.insert(insert_at, "Currently learning: " + ", ".join(additions))
    else:
        base_lines.append("")
        base_lines.append("Currently learning: " + ", ".join(additions))

    return "\n".join(base_lines).strip()


def save_docx(text, output_path):
    doc = Document()
    for line in text.split("\n"):
        doc.add_paragraph(line)
    doc.save(output_path)
    return output_path


def save_pdf(text, output_path):
    lines = [line.rstrip() for line in text.splitlines()]
    non_empty = [line for line in lines if line.strip()]

    def make_doc(scale):
        return SimpleDocTemplate(
            output_path,
            pagesize=A4,
            leftMargin=43.2,
            rightMargin=43.2,
            topMargin=36,
            bottomMargin=36,
        ), scale

    def scaled_styles(scale):
        styles = getSampleStyleSheet()
        return {
            "title": ParagraphStyle(
                "ResumeTitle",
                parent=styles["Title"],
                fontName="Helvetica-Bold",
                fontSize=15 * scale,
                leading=16 * scale,
                alignment=1,
                textColor=colors.black,
                spaceAfter=1,
            ),
            "contact": ParagraphStyle(
                "ResumeContact",
                parent=styles["BodyText"],
                fontName="Helvetica",
                fontSize=13.8 * 0.85 * scale,
                leading=15.2 * 0.85 * scale,
                alignment=1,
                textColor=colors.black,
                spaceAfter=1,
            ),
            "section": ParagraphStyle(
                "ResumeSection",
                parent=styles["Heading2"],
                fontName="Helvetica-Bold",
                fontSize=17.2 * 0.85 * scale,
                leading=18.4 * 0.85 * scale,
                textColor=colors.black,
                spaceBefore=1,
                spaceAfter=0.5,
            ),
            "body": ParagraphStyle(
                "ResumeBody",
                parent=styles["BodyText"],
                fontName="Helvetica",
                fontSize=14.8 * 0.85 * scale,
                leading=16.4 * 0.85 * scale,
                textColor=colors.black,
                spaceAfter=0,
                alignment=0,
                leftIndent=0,
                rightIndent=0,
                firstLineIndent=0,
            ),
            "bullet": ParagraphStyle(
                "ResumeBullet",
                parent=styles["BodyText"],
                fontName="Helvetica",
                fontSize=14.4 * 0.85 * scale,
                leading=16.0 * 0.85 * scale,
                textColor=colors.black,
                spaceAfter=0,
                leftIndent=28.8 * scale,
                firstLineIndent=-14.4 * scale,
                bulletIndent=14.4 * scale,
                alignment=0,
            ),
        }

    def build_story(scale):
        styles = scaled_styles(scale)
        story = []

        if non_empty:
            name_line = non_empty[0]
            story.append(Paragraph(f"<u>{escape(name_line)}</u>", styles["title"]))
            header_lines = []
            remaining_lines = []
            collecting_header = True
            for line in lines[1:]:
                clean = line.strip()
                if collecting_header and clean and not clean.isupper():
                    if any(token in normalize(line) for token in ["@", "linkedin", "github", "phone", "www", "http", "mailto", "+91"]):
                        header_lines.append(clean)
                        continue
                    if len(header_lines) < 2:
                        header_lines.append(clean)
                        continue
                collecting_header = False
                remaining_lines.append(line)

            if header_lines:
                header_text = " | ".join(escape(item) for item in header_lines)
                story.append(Paragraph(header_text, styles["contact"]))
            story.append(HRFlowable(width="100%", thickness=0.4, color=colors.black))
            story.append(Spacer(1, 2 * scale))
        else:
            remaining_lines = lines

        heading_names = {
            "summary", "professional summary", "skills", "technical skills", "core skills",
            "experience", "work experience", "professional experience", "projects",
            "education", "certifications", "internship", "internships", "contact"
        }

        def is_heading(line):
            cleaned = line.strip().rstrip(":")
            lowered = cleaned.lower()
            return lowered in heading_names or (cleaned.isupper() and len(cleaned.split()) <= 5)

        def is_bullet(line):
            stripped = line.lstrip()
            return stripped.startswith(("-", "•", "*"))

        def section_title(line):
            return line.strip().rstrip(":").upper()

        for line in remaining_lines:
            stripped = line.strip()
            if not stripped:
                story.append(Spacer(1, 0.2 * scale))
                continue

            if is_heading(stripped):
                story.append(Spacer(1, 10 * scale))
                story.append(Paragraph(f"<u><b>{escape(section_title(stripped))}</b></u>", styles["section"]))
                story.append(Spacer(1, 5 * scale))
                story.append(HRFlowable(width="100%", thickness=0.35, color=colors.black))
                continue

            if is_bullet(stripped):
                bullet_text = stripped.lstrip("-•* ").strip()
                story.append(Paragraph(f"• {linkify_text(bullet_text)}", styles["bullet"]))
                continue

            story.append(Paragraph(linkify_text(stripped), styles["body"]))

        return story

    for scale in [1.0, 0.92, 0.86, 0.8, 0.74, 0.68]:
        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            leftMargin=54,
            rightMargin=54,
            topMargin=54,
            bottomMargin=54,
        )
        story = build_story(scale)
        doc.build(story)
        if pdf_page_count(output_path) <= MAX_PDF_PAGES:
            return output_path

    return output_path


def pdf_page_count(pdf_path):
    with fitz.open(pdf_path) as doc:
        return doc.page_count


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
    remove_terms = data.get("remove_terms", [])
    confirm_removals = bool(data.get("confirm_removals", False))
    output_format = (data.get("output_format") or "docx").lower()

    resume_row = get_resume_or_404(session_id)
    if not resume_row:
        return jsonify({"error": "Resume not found"}), 404

    if remove_terms and not confirm_removals:
        return jsonify({
            "error": "Removal confirmation required. Please confirm before removing any content."
        }), 400

    base_text = resume_row["raw_text"]
    removed_lines = []
    if remove_terms and confirm_removals:
        base_text, removed_lines = apply_removals(base_text, remove_terms)

    rewritten_text = build_tailored_resume(
        base_text,
        job_description,
        approved_additions
    )

    base_name = f"{session_id}_optimized_resume"
    docx_path = os.path.join(GENERATED_FOLDER, base_name + ".docx")
    pdf_path = os.path.join(GENERATED_FOLDER, base_name + ".pdf")

    save_docx(rewritten_text, docx_path)
    save_pdf(rewritten_text, pdf_path)

    if pdf_page_count(pdf_path) > MAX_PDF_PAGES:
        if os.path.exists(docx_path):
            os.remove(docx_path)
        if os.path.exists(pdf_path):
            os.remove(pdf_path)
        return jsonify({
            "needs_removal": True,
            "message": "The resume is still longer than one page. Please remove some content and try again.",
            "page_limit": MAX_PDF_PAGES,
            "suggested_removals": suggest_removal_candidates(base_text),
            "resume_rule": "Original content was preserved. Please confirm what to remove before regenerating."
        }), 200

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
        "resume_rule": "Original resume content was preserved. Only approved additions were appended.",
        "removed_lines": removed_lines,
        "needs_removal": False,
        "rewritten_text": rewritten_text,
        "download_docx": f"/download/{session_id}?format=docx",
        "download_pdf": f"/download/{session_id}?format=pdf",
        "preview_pdf": f"/preview/{session_id}"
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


@app.route("/preview/<session_id>")
def preview(session_id):
    file_path = os.path.join(GENERATED_FOLDER, f"{session_id}_optimized_resume.pdf")
    if not os.path.exists(file_path):
        return jsonify({"error": "File not found"}), 404

    response = send_file(file_path, mimetype="application/pdf", as_attachment=False)
    response.headers["Content-Disposition"] = "inline"
    return response


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
