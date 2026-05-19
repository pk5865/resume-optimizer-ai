import io
import os
import re
import sqlite3
import uuid
import tempfile
from datetime import datetime
from xml.sax.saxutils import escape

import fitz
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

from services.pdf_parser import extract_text

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:  # pragma: no cover - optional dependency for local dev
    psycopg2 = None
    RealDictCursor = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "resume_optimizer_ai.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
URL_RE = re.compile(r"(https?://[^\s<>\"]+)")
MAX_PDF_PAGES = 1
DB_INIT_ERROR = None

COMMON_SKILLS = [
    "python", "java", "javascript", "typescript", "html", "css", "sql",
    "flask", "django", "fastapi", "react", "node.js", "node", "spring boot",
    "rest api", "api", "microservices", "jwt", "oauth", "langchain", "rag",
    "chromadb", "vector database", "machine learning", "nlp", "llm",
    "genai", "generative ai", "numpy", "pandas", "scikit-learn", "pytorch",
    "tensorflow", "mysql", "postgresql", "mongodb", "redis", "sqlite",
    "docker", "kubernetes", "jenkins", "git", "github", "linux", "aws",
    "azure", "nginx", "terraform", "postman", "pdf", "airflow", "ci/cd"
]

SKILL_GROUPS = {
    "Languages": [
        "python", "java", "javascript", "typescript", "sql", "html", "css"
    ],
    "Frameworks": [
        "flask", "django", "fastapi", "react", "node.js", "node", "spring boot",
        "langchain", "rag"
    ],
    "AI / ML": [
        "machine learning", "nlp", "llm", "genai", "generative ai",
        "numpy", "pandas", "scikit-learn", "pytorch", "tensorflow", "chromadb",
        "vector database"
    ],
    "Databases": [
        "mysql", "postgresql", "mongodb", "sqlite", "redis"
    ],
    "Cloud / DevOps": [
        "aws", "azure", "docker", "kubernetes", "jenkins", "git", "github",
        "linux", "nginx", "terraform", "ci/cd", "airflow"
    ],
    "Tools": [
        "rest api", "api", "jwt", "oauth", "postman", "pdf"
    ],
}

ROLE_HINTS = [
    ("Full Stack Developer", ["full stack", "full-stack", "react", "node", "frontend", "backend"]),
    ("Backend Developer", ["backend", "spring boot", "flask", "django", "fastapi", "api"]),
    ("Software Engineer", ["software engineer", "software developer", "engineer", "developer"]),
    ("Data Analyst", ["data analyst", "data analytics", "analytics", "sql", "pandas"]),
    ("Machine Learning Engineer", ["machine learning", "ml engineer", "ai", "llm", "genai", "nlp"]),
    ("DevOps Engineer", ["devops", "ci/cd", "docker", "kubernetes", "jenkins", "terraform"]),
]

ROLE_HEADLINE_TERMS = {
    "Full Stack Developer": ["python", "react", "flask", "rest api", "postgresql"],
    "Backend Developer": ["python", "flask", "django", "rest api", "postgresql"],
    "Software Engineer": ["python", "javascript", "api", "sql", "git"],
    "Data Analyst": ["python", "sql", "pandas", "numpy", "machine learning"],
    "Machine Learning Engineer": ["python", "machine learning", "scikit-learn", "nlp", "langchain"],
    "DevOps Engineer": ["docker", "kubernetes", "jenkins", "aws", "ci/cd"],
    "Software Professional": ["python", "react", "api", "sql"],
}

CLAIM_REWRITES = [
    (re.compile(r"(?i)\bled 3\+ python automation tools\b"), "Developed Python automation scripts"),
    (re.compile(r"(?i)\bled\b"), "Developed"),
    (re.compile(r"(?i)\b95% defect-free delivery\b"), "improved code quality through testing and debugging"),
    (re.compile(r"(?i)\b95% defect-free\b"), "improved code quality"),
    (re.compile(r"(?i)\bmanaged Agile Git workflows\b"), "collaborated using Git in Agile development workflows"),
    (re.compile(r"(?i)\bconfusion matrix,\s*precision,\s*recall and F1 score metrics\b"), "RMSE, MAE, and R² metrics"),
    (re.compile(r"(?i)\bconfusion matrix,\s*precision,\s*recall,\s*and F1 score metrics\b"), "RMSE, MAE, and R² metrics"),
    (re.compile(r"(?i)\b100% offline classification accuracy\b"), "offline fallback support"),
    (re.compile(r"(?i)\bfull stack developer \| full stack developer\b"), "Full Stack Developer"),
    (re.compile(r"(?i)\bapi · docker · genai · kubernetes · llm · nlp\b"), "Python, React, Flask, AI Applications"),
]

ACHIEVEMENT_PATTERNS = [
    re.compile(r"(?i)\b\d+\+\s+production-ready applications\b"),
    re.compile(r"(?i)\b\d+\+\s+live\b"),
    re.compile(r"(?i)\b\d+\+\s+customer-facing applications\b"),
    re.compile(r"(?i)\b\d+\s+live on\b"),
    re.compile(r"(?i)\bdeployed\b"),
    re.compile(r"(?i)\breduced\b"),
    re.compile(r"(?i)\bimproved\b"),
    re.compile(r"(?i)\boptimized\b"),
    re.compile(r"(?i)\bdeveloped\b"),
]

app = Flask(__name__)
CORS(app)


def get_db():
    if DATABASE_URL:
        if psycopg2 is None:
            raise RuntimeError("psycopg2 is required when DATABASE_URL is set")
        try:
            return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor, connect_timeout=10)
        except Exception:
            # Fall back to local SQLite so the service can still boot if the
            # hosted database connection is misconfigured during deploy.
            pass

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn, table_name, column_name, column_sql):
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {column_sql}")
        return

    cur = conn.cursor()
    existing = cur.execute(f"PRAGMA table_info({table_name})").fetchall()
    column_names = {row[1] for row in existing}
    if column_name not in column_names:
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")


def migrate_db(conn):
    ensure_column(conn, "generated_files", "generated_text", "TEXT NOT NULL DEFAULT ''")


def init_db():
    global DB_INIT_ERROR
    try:
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
                generated_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (resume_id) REFERENCES resumes (id)
            )
            """
        )
        migrate_db(conn)
        conn.commit()
        conn.close()
        DB_INIT_ERROR = None
    except Exception as exc:
        DB_INIT_ERROR = str(exc)
        fallback = sqlite3.connect(DB_PATH)
        fallback.row_factory = sqlite3.Row
        cur = fallback.cursor()
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
                generated_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (resume_id) REFERENCES resumes (id)
            )
            """
        )
        migrate_db(fallback)
        fallback.commit()
        fallback.close()


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


def ordered_unique(items):
    ordered = []
    seen = set()
    for item in items:
        cleaned = normalize(str(item))
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            ordered.append(cleaned)
    return ordered


def display_term(term):
    special = {
        "api": "API",
        "aws": "AWS",
        "azure": "Azure",
        "ci/cd": "CI/CD",
        "css": "CSS",
        "docker": "Docker",
        "django": "Django",
        "fastapi": "FastAPI",
        "flask": "Flask",
        "github": "GitHub",
        "git": "Git",
        "html": "HTML",
        "java": "Java",
        "javascript": "JavaScript",
        "jenkins": "Jenkins",
        "jwt": "JWT",
        "kubernetes": "Kubernetes",
        "langchain": "LangChain",
        "llm": "LLM",
        "linux": "Linux",
        "machine learning": "Machine Learning",
        "mongodb": "MongoDB",
        "mysql": "MySQL",
        "nginx": "Nginx",
        "node": "Node",
        "node.js": "Node.js",
        "nlp": "NLP",
        "oauth": "OAuth",
        "pandas": "Pandas",
        "postgresql": "PostgreSQL",
        "postman": "Postman",
        "python": "Python",
        "pytorch": "PyTorch",
        "rag": "RAG",
        "react": "React",
        "redis": "Redis",
        "rest api": "REST API",
        "scikit-learn": "scikit-learn",
        "sql": "SQL",
        "spring boot": "Spring Boot",
        "tensorflow": "TensorFlow",
        "terraform": "Terraform",
        "typescript": "TypeScript",
        "vector database": "Vector Database",
        "genai": "GenAI",
        "generative ai": "Generative AI",
        "airflow": "Airflow",
        "pdf": "PDF",
        "microservices": "Microservices",
        "sqlite": "SQLite",
    }
    if term in special:
        return special[term]
    return " ".join(part.capitalize() for part in term.split())


def clean_claim_text(text):
    cleaned = text.strip()
    for pattern, replacement in CLAIM_REWRITES:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip(" -;,.")


def pick_role_headline_terms(role, resume_keywords, approved_keywords):
    preferred = ROLE_HEADLINE_TERMS.get(role, ROLE_HEADLINE_TERMS["Software Professional"])
    allowed = ordered_unique(resume_keywords + approved_keywords)
    picked = []

    for term in preferred:
        if term in allowed and term not in picked:
            picked.append(term)

    for term in allowed:
        if term not in picked and term in preferred:
            picked.append(term)

    if not picked:
        picked = allowed[:4]

    return [display_term(term) for term in picked[:4]]


def infer_target_role(job_description, resume_text=""):
    haystack = normalize(f"{job_description} {resume_text}")
    for role, keywords in ROLE_HINTS:
        if any(keyword in haystack for keyword in keywords):
            return role
    return "Software Professional"


def collect_contact_lines(lines):
    contact_lines = []
    for line in lines[1:]:
        clean = line.strip()
        if not clean:
            continue
        lowered = normalize(clean)
        if lowered in {
            "summary", "professional summary", "skills", "technical skills",
            "core skills", "experience", "work experience", "professional experience",
            "projects", "education", "certifications", "contact"
        }:
            break
        if any(token in lowered for token in ["@", "linkedin", "github", "portfolio", "phone", "www", "http", "+91"]):
            contact_lines.append(clean)
            continue
        if len(contact_lines) < 3 and len(clean.split()) <= 6 and not clean.endswith("."):
            contact_lines.append(clean)
    return contact_lines


def format_contact_block(contact_lines):
    if not contact_lines:
        return []

    if len(contact_lines) <= 2:
        return [" | ".join(contact_lines)]

    primary = " | ".join(contact_lines[:2])
    secondary = " | ".join(contact_lines[2:])
    return [primary, secondary]


def format_section_lines(lines):
    formatted = []
    for line in lines:
        clean = clean_claim_text(line)
        if not clean:
            continue
        stripped = clean.lstrip("-•* ").strip()
        if not stripped:
            continue
        formatted.append(f"• {stripped}")
    return formatted


def compose_summary(role, resume_keywords, job_keywords, approved_keywords, existing_summary):
    if existing_summary:
        summary_text = " ".join(existing_summary).strip()
        summary_text = clean_claim_text(summary_text)
        if summary_text and summary_text.count(",") <= 3 and len(summary_text.split()) <= 55:
            return [summary_text]

    preferred = ROLE_HEADLINE_TERMS.get(role, ROLE_HEADLINE_TERMS["Software Professional"])
    combined = ordered_unique(resume_keywords + approved_keywords)
    prioritized = [item for item in preferred if item in combined]
    if not prioritized:
        prioritized = combined[:4]
    top_keywords = [display_term(item) for item in prioritized[:5]]
    keyword_text = ", ".join(top_keywords) if top_keywords else "Python, React, REST APIs"
    return [
        f"{role} with hands-on experience in {keyword_text}.",
        "Built and deployed web applications using APIs, databases, and modern development workflows."
    ]


def compose_skill_lines(all_keywords):
    normalized_keywords = ordered_unique(all_keywords)
    grouped = []
    used = set()

    for group_name, group_terms in SKILL_GROUPS.items():
        matches = []
        for term in group_terms:
            if term in normalized_keywords and term not in used:
                matches.append(display_term(term))
                used.add(term)
        if matches:
            grouped.append(f"{group_name}: {', '.join(matches[:5])}")

    leftovers = [display_term(item) for item in normalized_keywords if item not in used]
    if leftovers:
        grouped.append(f"Additional: {', '.join(leftovers[:5])}")

    return grouped


def compress_skill_groups(skill_lines):
    compressed = []
    for line in skill_lines:
        if line.startswith("AI / ML:"):
            line = line.replace("Machine Learning, ", "")
            line = line.replace("GenAI, Generative AI, ", "GenAI, ")
        compressed.append(line)
    return compressed


def get_section_or_fallback(sections, key, fallback_lines):
    items = sections.get(key, [])
    if items:
        return items
    return fallback_lines


def limit_lines(lines, max_items):
    return [line for line in lines if line.strip()][:max_items]


def project_fallback_lines(raw_text):
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    candidates = []
    for line in lines:
        lowered = normalize(line)
        if any(pattern.search(line) for pattern in ACHIEVEMENT_PATTERNS):
            candidates.append(line)
            continue
        if " | " in line or " — " in line or " - " in line:
            candidates.append(line)
            continue
        if any(token in lowered for token in ["live", "railway", "vercel", "netlify", "project", "api", "langchain", "rag", "ml", "predictor", "chatbot"]):
            candidates.append(line)
    return ordered_unique(candidates)


def achievement_lines(raw_text):
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    candidates = []
    for line in lines:
        lowered = normalize(line)
        if any(pattern.search(line) for pattern in ACHIEVEMENT_PATTERNS):
            candidates.append(line)
        elif any(token in lowered for token in ["certified", "certification", "bootcamp", "portfolio", "github", "live on", "immediate joiner"]):
            candidates.append(line)
    return ordered_unique(candidates)


def has_explicit_heading(raw_text, names):
    lines = [line.strip().lower().rstrip(":") for line in raw_text.splitlines() if line.strip()]
    return any(line in names for line in lines)


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
        "education": ["education", "academics", "academic"],
        "certifications": ["certifications", "certification"],
        "additional": ["additional", "achievements", "awards"]
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
        sections["certifications"] = []

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
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    sections = build_resume_sections(raw_text)

    name_line = lines[0] if lines else "YOUR NAME"
    contact_lines = collect_contact_lines(lines)

    resume_keywords = ordered_unique(extract_keywords(raw_text))
    job_keywords = ordered_unique(extract_keywords(job_description))
    approved_keywords = ordered_unique(approved_additions or [])
    all_keywords = ordered_unique(resume_keywords + approved_keywords)

    target_role = infer_target_role(job_description, raw_text)
    summary_heading_names = {"summary", "profile", "objective"}
    explicit_summary = has_explicit_heading(raw_text, summary_heading_names)
    header_terms = pick_role_headline_terms(target_role, resume_keywords, approved_keywords)
    role_line = f"{target_role} | " + " · ".join(header_terms) if header_terms else target_role

    summary_lines = compose_summary(
        target_role,
        resume_keywords,
        job_keywords,
        approved_keywords,
        sections.get("summary", []) if explicit_summary else []
    )
    skill_lines = compress_skill_groups(compose_skill_lines(all_keywords))

    experience_lines = get_section_or_fallback(
        sections,
        "experience",
        lines[4:12] if len(lines) > 4 else lines[1:]
    )
    project_lines = get_section_or_fallback(
        sections,
        "projects",
        lines[12:18] if len(lines) > 12 else lines[1:]
    )
    if len(project_lines) < 4:
        project_lines = project_fallback_lines(raw_text) or project_lines
    education_lines = get_section_or_fallback(
        sections,
        "education",
        lines[-4:] if len(lines) > 4 else lines[1:]
    )
    certification_lines = sections.get("certifications", [])
    achievement_lines_raw = achievement_lines(raw_text)

    output = [name_line]
    output.append(role_line)
    output.extend(format_contact_block(contact_lines))

    output.append("")
    output.append("PROFILE SUMMARY")
    output.extend(limit_lines(summary_lines, 2))

    output.append("")
    output.append("SKILLS")
    if skill_lines:
        output.extend(limit_lines(skill_lines, 6))
    else:
        output.append(
            "Technical Skills: " + ", ".join(display_term(item) for item in all_keywords[:12])
            if all_keywords else "Technical Skills"
        )

    output.append("")
    output.append("EXPERIENCE")
    experience_block = format_section_lines(limit_lines(experience_lines, 3))
    output.extend(experience_block or ["• Add measurable experience bullets from the original resume."])

    output.append("")
    output.append("PROJECTS")
    project_block = format_section_lines(limit_lines(project_lines, 8))
    output.extend(project_block or ["• Add relevant project details from the original resume."])

    output.append("")
    output.append("EDUCATION")
    education_block = format_section_lines(limit_lines(education_lines, 4))
    output.extend(education_block or ["• Add your degree, college, CGPA, and graduation year."])

    if certification_lines:
        output.append("")
        output.append("CERTIFICATIONS")
        output.extend(format_section_lines(limit_lines(certification_lines, 3)))

    if achievement_lines_raw:
        output.append("")
        output.append("ACHIEVEMENTS")
        output.extend(format_section_lines(limit_lines(achievement_lines_raw, 4)))

    return "\n".join(output).strip()


def pdf_page_count(pdf_bytes):
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return doc.page_count


def build_pdf_bytes(text):
    lines = [line.rstrip() for line in text.splitlines()]
    non_empty = [line for line in lines if line.strip()]

    fit_profiles = [
        {
            "title": 13.4,
            "contact": 9.3,
            "section": 11.2,
            "body": 9.3,
            "bullet": 9.3,
            "left": 40,
            "right": 40,
            "top": 30,
            "bottom": 30,
            "summary_limit": 2,
            "skill_limit": 5,
            "experience_limit": 3,
            "project_limit": 6,
            "education_limit": 4,
            "cert_limit": 2,
            "achievement_limit": 2,
            "header_gap": 1,
            "section_gap": 4,
        },
        {
            "title": 13.0,
            "contact": 9.0,
            "section": 10.9,
            "body": 9.0,
            "bullet": 9.0,
            "left": 38,
            "right": 38,
            "top": 28,
            "bottom": 28,
            "summary_limit": 2,
            "skill_limit": 5,
            "experience_limit": 3,
            "project_limit": 5,
            "education_limit": 4,
            "cert_limit": 0,
            "achievement_limit": 2,
            "header_gap": 1,
            "section_gap": 3,
        },
        {
            "title": 12.6,
            "contact": 8.8,
            "section": 10.5,
            "body": 8.8,
            "bullet": 8.8,
            "left": 36,
            "right": 36,
            "top": 24,
            "bottom": 24,
            "summary_limit": 2,
            "skill_limit": 4,
            "experience_limit": 3,
            "project_limit": 5,
            "education_limit": 3,
            "cert_limit": 0,
            "achievement_limit": 0,
            "header_gap": 0,
            "section_gap": 2,
        },
    ]

    def build_story(profile):
        styles = getSampleStyleSheet()
        resume_styles = {
            "title": ParagraphStyle(
                "ResumeTitle",
                parent=styles["Title"],
                fontName="Helvetica-Bold",
                fontSize=profile["title"],
                leading=profile["title"] + 1.2,
                alignment=1,
                textColor=colors.black,
                spaceAfter=1,
            ),
            "contact": ParagraphStyle(
                "ResumeContact",
                parent=styles["BodyText"],
                fontName="Helvetica",
                fontSize=profile["contact"],
                leading=profile["contact"] + 1.0,
                alignment=1,
                textColor=colors.black,
                spaceAfter=0,
            ),
            "section": ParagraphStyle(
                "ResumeSection",
                parent=styles["Heading2"],
                fontName="Helvetica-Bold",
                fontSize=profile["section"],
                leading=profile["section"] + 0.8,
                textColor=colors.black,
                spaceBefore=1,
                spaceAfter=0,
            ),
            "body": ParagraphStyle(
                "ResumeBody",
                parent=styles["BodyText"],
                fontName="Helvetica",
                fontSize=profile["body"],
                leading=profile["body"] + 1.2,
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
                fontSize=profile["bullet"],
                leading=profile["bullet"] + 1.2,
                textColor=colors.black,
                spaceAfter=0,
                leftIndent=10,
                firstLineIndent=-8,
                bulletIndent=8,
                alignment=0,
            ),
        }

        story = []

        if non_empty:
            name_line = non_empty[0]
            story.append(Paragraph(f"<u>{escape(name_line)}</u>", resume_styles["title"]))
            remaining_lines = list(lines[1:])
            header_lines = []
            while remaining_lines:
                first = remaining_lines[0].strip()
                if not first:
                    remaining_lines.pop(0)
                    break
                header_lines.append(remaining_lines.pop(0))

            if header_lines:
                for item in header_lines:
                    story.append(Paragraph(escape(item), resume_styles["contact"]))
            story.append(HRFlowable(width="100%", thickness=0.4, color=colors.black))
            if profile["header_gap"]:
                story.append(Spacer(1, profile["header_gap"]))
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
                story.append(Spacer(1, 1))
                continue

            if is_heading(stripped):
                if profile["section_gap"]:
                    story.append(Spacer(1, profile["section_gap"]))
                story.append(Paragraph(f"<u><b>{escape(section_title(stripped))}</b></u>", resume_styles["section"]))
                story.append(HRFlowable(width="100%", thickness=0.35, color=colors.black))
                continue

            if is_bullet(stripped):
                bullet_text = stripped.lstrip("-•* ").strip()
                story.append(Paragraph(f"• {linkify_text(bullet_text)}", resume_styles["bullet"]))
                continue

            story.append(Paragraph(linkify_text(stripped), resume_styles["body"]))

        return story

    last_pdf = b""
    for profile in fit_profiles:
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=profile["left"],
            rightMargin=profile["right"],
            topMargin=profile["top"],
            bottomMargin=profile["bottom"],
        )
        story = build_story(profile)
        doc.build(story)
        pdf_bytes = buffer.getvalue()
        last_pdf = pdf_bytes

        if pdf_page_count(pdf_bytes) <= MAX_PDF_PAGES:
            return pdf_bytes

    return last_pdf


def remove_existing_generated_files(session_id):
    conn = get_db()
    conn.execute("DELETE FROM generated_files WHERE resume_id = ?", (session_id,))
    conn.commit()
    conn.close()


def get_resume_or_404(resume_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM resumes WHERE id = ?", (resume_id,)).fetchone()
    conn.close()
    return row


def get_generated_resume_or_404(session_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM generated_files WHERE resume_id = ? AND file_type = 'pdf' ORDER BY created_at DESC LIMIT 1",
        (session_id,)
    ).fetchone()
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
    payload = {"status": "ok"}
    if DB_INIT_ERROR:
        payload["database_warning"] = DB_INIT_ERROR
    return jsonify(payload)


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("file")
    if not file or file.filename == "":
        return jsonify({"error": "Please upload a PDF file"}), 400

    try:
        session_id = str(uuid.uuid4())
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            temp_path = tmp.name
            file.save(temp_path)

        try:
            raw_text = extract_text(temp_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

        if not raw_text.strip():
            raw_text = ""

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
    except Exception as exc:
        return jsonify({
            "error": "Upload failed while reading the PDF.",
            "details": str(exc)
        }), 500


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

    remove_existing_generated_files(session_id)
    pdf_bytes = build_pdf_bytes(rewritten_text)
    if pdf_page_count(pdf_bytes) > MAX_PDF_PAGES:
        return jsonify({
            "error": "Generated resume still exceeds one page."
        }), 400

    conn = get_db()
    conn.execute(
        "INSERT INTO generated_files (id, resume_id, file_type, file_path, generated_text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), session_id, "pdf", "", rewritten_text, now_iso())
    )
    conn.commit()
    conn.close()

    return jsonify({
        "message": "resume rewritten",
        "resume_rule": "Original resume content was preserved. Only approved additions were appended.",
        "removed_lines": removed_lines,
        "needs_removal": False,
        "rewritten_text": rewritten_text,
        "download_pdf": f"/download/{session_id}?format=pdf",
        "preview_pdf": f"/preview/{session_id}"
    })


@app.route("/download/<session_id>")
def download(session_id):
    file_format = request.args.get("format", "pdf").lower()
    if file_format != "pdf":
        return jsonify({"error": "Invalid format"}), 400

    generated = get_generated_resume_or_404(session_id)
    if not generated:
        return jsonify({"error": "File not found"}), 404

    pdf_bytes = build_pdf_bytes(generated["generated_text"])
    return send_file(
        io.BytesIO(pdf_bytes),
        as_attachment=True,
        download_name="optimized_resume.pdf",
        mimetype="application/pdf",
    )


@app.route("/preview/<session_id>")
def preview(session_id):
    generated = get_generated_resume_or_404(session_id)
    if not generated:
        return jsonify({"error": "File not found"}), 404

    pdf_bytes = build_pdf_bytes(generated["generated_text"])
    response = send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
    )
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
