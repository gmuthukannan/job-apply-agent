"""
doc_generator.py
================
Generates a tailored resume and cover letter using Claude, then renders
both to PDF via Playwright's built-in PDF export.

Model choices:
  Resume:       claude-opus-4-6  — quality matters most here; Opus produces the
                                   best tailored bullets and summary language
  Cover letter: claude-sonnet-4-6 — still high quality, but less complex than
                                    resume; Sonnet is sufficient and cheaper

Flow per document:
  1. Build prompt with full profile + JD
  2. Claude returns structured JSON (tailored content)
  3. Jinja2 renders the JSON into HTML using templates/
  4. Playwright opens the HTML file and exports it as PDF
  5. Both HTML and PDF are saved to the run directory

Filenames:
  Resume:       {FirstName}_Resume_{Job_Title}.pdf
  Cover letter: cover_letter.pdf
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

import anthropic
from jinja2 import Environment, FileSystemLoader
from playwright.async_api import Page

from agent.logger import AgentLogger
from agent.profile_loader import profile_summary_for_llm


# ── Model selection ────────────────────────────────────────────────────────────
# Resume uses Opus — we want the best possible tailored language here.
# This is the document a hiring manager will actually read, so it's worth
# the extra cost (~$0.15-0.30 per generation).
RESUME_MODEL = "claude-opus-4-6"

# Cover letter uses Sonnet — still excellent quality, roughly 5x cheaper.
# The cover letter is less structured than the resume so Sonnet handles it well.
COVER_MODEL = "claude-sonnet-4-6"

# Vision resolver (in applicator.py) also uses Sonnet — reading a screenshot
# and identifying options doesn't require Opus-level reasoning.


# ── Resume generation prompt ───────────────────────────────────────────────────
# Returns structured JSON so we can render it into a clean HTML template.
# Using JSON (rather than asking for formatted text) lets us control layout
# precisely and avoids parsing issues.
RESUME_PROMPT = """
You are an expert resume writer specialising in technical and AI engineering roles.
Your output is strictly JSON - no markdown, no explanation.

CANDIDATE PROFILE:
{profile_text}

JOB DESCRIPTION:
Company: {company}
Role: {role}
Location: {location}
Key themes: {key_themes}
Requirements:
{requirements}

TASK: Produce a tailored, ATS-optimised resume for this specific role.
Rules:
- Lead bullets with strong action verbs and concrete metrics wherever possible
- Mirror the language of the JD (use their exact terminology)
- Surface the 2-3 evidence stories most relevant to this role
- Keep bullets concise: <= 2 lines each
- Tagline should be specific to the role (not generic)
- Summary: 3 sentences max, mention the company name naturally

Return ONLY this JSON structure:
{{
  "tagline": "one line positioning statement targeting this specific role",
  "summary": "3-sentence tailored professional summary",
  "skills_rows": [
    [{{"category": "Category Name", "items": "Item1, Item2, Item3"}}, {{"category": "Category Name", "items": "Item1, Item2"}}],
    [{{"category": "Category Name", "items": "Item1, Item2"}}, {{"category": "Category Name", "items": "Item1"}}]
  ],
  "experience": [
    {{
      "title": "Job Title",
      "company": "Company Name",
      "period": "Month YYYY - Month YYYY",
      "location": "City, Country",
      "bullets": [
        "Strong action verb + what you did + measurable outcome",
        "..."
      ]
    }}
  ],
  "tailoring_notes": "Internal note explaining key tailoring decisions made"
}}
"""


# ── Cover letter generation prompt ─────────────────────────────────────────────
COVER_LETTER_PROMPT = """
You are a senior technical writer crafting a cover letter for a competitive AI/data engineering role.

CANDIDATE PROFILE:
{profile_text}

TARGET ROLE:
Company: {company}
Role: {role}
Key themes: {key_themes}
Role summary: {role_summary}

RESUME TAGLINE (use as positioning anchor):
{tagline}

TASK: Write a compelling, concise cover letter. Rules:
- 3 body paragraphs only
- Para 1: Hook - why this company + role excites the candidate (be specific, not generic)
- Para 2: Strongest relevant evidence story with a concrete metric
- Para 3: Second evidence thread + forward-looking close
- Tone: {tone}
- Never use cliches ("I am writing to apply...", "passion for...", "team player")
- Max 280 words total across the 3 paragraphs
- Reference specific JD themes naturally

Return ONLY this JSON:
{{
  "paragraphs": [
    "paragraph 1 text",
    "paragraph 2 text",
    "paragraph 3 text"
  ],
  "word_count": 270
}}
"""


async def html_to_pdf(page: Page, html_path: Path, pdf_path: Path):
    """
    Convert a local HTML file to PDF using Playwright's PDF export.

    Why Playwright instead of WeasyPrint or similar?
    - Already a project dependency (no extra install)
    - Uses the full Chromium rendering engine — CSS support is excellent
    - page.pdf() handles page breaks, fonts, and print media queries correctly

    Args:
        page:      Playwright page object
        html_path: Absolute path to the HTML file to render
        pdf_path:  Where to save the output PDF
    """
    # file:// protocol lets Playwright load a local file without a web server
    await page.goto(f"file://{html_path.resolve()}", wait_until="load")

    # Small buffer to ensure fonts/CSS are fully applied before export
    await page.wait_for_timeout(500)

    await page.pdf(
        path=str(pdf_path),
        format="A4",
        # Margin is set to 0 here because the HTML template handles its own padding
        margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"},
        print_background=True,  # Ensure background colors/borders render
    )


async def generate_resume(
    page: Page,
    profile: dict,
    jd: dict,
    run_dir: Path,
    logger: AgentLogger,
) -> tuple[dict, Path]:
    """
    Generate a tailored resume via Claude Opus, render to HTML + PDF.

    Steps:
      1. Build prompt with full profile + JD requirements
      2. Claude returns tailored resume content as JSON
      3. Log tailoring decisions (what Claude emphasized and why)
      4. Render JSON into HTML using templates/resume.html (Jinja2)
      5. Convert HTML to PDF via Playwright

    Args:
        page:    Playwright page (reused for PDF rendering)
        profile: Loaded candidate profile
        jd:      Parsed job description
        run_dir: Directory for this run's output files
        logger:  Agent logger

    Returns:
        (resume_data dict, pdf_path Path)
    """
    logger.section("📄", "Resume Generation")
    logger.info(f"Using model: {RESUME_MODEL} (Opus — best quality for resume)")

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # Format requirements as a bulleted list for the prompt
    requirements_text = "\n".join(f"  - {r}" for r in jd.get("requirements", []))

    prompt = RESUME_PROMPT.format(
        profile_text=profile_summary_for_llm(profile),
        company=jd["company"],
        role=jd["role"],
        location=jd.get("location", ""),
        key_themes=", ".join(jd.get("key_themes", [])),
        requirements=requirements_text,
    )

    logger.info("Sending profile + JD to Claude Opus for resume tailoring...")
    logger.ai_prompt("Resume Generator", prompt[:600] + "...")

    response = client.messages.create(
        model=RESUME_MODEL,
        max_tokens=3000,  # Resume JSON with 6 roles + bullets is ~1500-2000 tokens
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    logger.ai_response("Resume Content", raw)

    cleaned = re.sub(r"```json|```", "", raw).strip()
    start = cleaned.find("{")
    end   = cleaned.rfind("}")
    resume_data = json.loads(cleaned[start : end + 1])

    # Log what Claude decided to emphasize
    logger.decision("Tagline",         resume_data.get("tagline", ""))
    logger.decision("Tailoring notes", resume_data.get("tailoring_notes", ""))

    # Build a plain-text version of the resume for the log file
    resume_text_lines = [
        resume_data.get("tagline", ""),
        "",
        "SUMMARY",
        resume_data.get("summary", ""),
        "",
        "EXPERIENCE",
    ]
    for role in resume_data.get("experience", []):
        resume_text_lines.append(f"\n{role['title']} @ {role['company']} | {role['period']}")
        for bullet in role.get("bullets", []):
            resume_text_lines.append(f"  * {bullet}")

    logger.document("Generated Resume Text", "\n".join(resume_text_lines))

    # ── Render HTML from Jinja2 template ──────────────────────────────────
    # templates/resume.html uses Jinja2 {{ }} syntax to insert data
    env = Environment(loader=FileSystemLoader("templates"))
    template = env.get_template("resume.html")
    html_content = template.render(
        personal=profile["personal"],
        tagline=resume_data["tagline"],
        summary=resume_data["summary"],
        skills_rows=resume_data.get("skills_rows", []),
        experience=resume_data.get("experience", []),
        certifications=profile.get("certifications", []),
        education=profile.get("education", {}),
    )

    html_path = run_dir / "resume.html"
    html_path.write_text(html_content, encoding="utf-8")
    logger.info(f"Resume HTML saved -> {html_path}")

    # ── Build output filename ──────────────────────────────────────────────
    # Format: FirstName_Resume_Job_Title.pdf (spaces -> underscores)
    first_name = profile["personal"]["full_name"].split()[0]
    safe_title = re.sub(r"[^\w\s-]", "", jd.get("role", "Role")).strip()
    safe_title = re.sub(r"\s+", "_", safe_title)
    pdf_name = f"{first_name}_Resume_{safe_title}.pdf"

    pdf_path = run_dir / pdf_name
    await html_to_pdf(page, html_path, pdf_path)
    logger.success(f"Resume PDF saved -> {pdf_path}")

    return resume_data, pdf_path


async def generate_cover_letter(
    page: Page,
    profile: dict,
    jd: dict,
    resume_data: dict,
    run_dir: Path,
    logger: AgentLogger,
) -> tuple[str, Path]:
    """
    Generate a tailored cover letter via Claude Sonnet, render to HTML + PDF.

    Uses the resume tagline as a positioning anchor so the cover letter
    and resume tell a consistent story.

    Args:
        page:        Playwright page (reused for PDF rendering)
        profile:     Loaded candidate profile
        jd:          Parsed job description
        resume_data: Output from generate_resume() — provides the tagline
        run_dir:     Directory for this run's output files
        logger:      Agent logger

    Returns:
        (cover_letter_full_text str, pdf_path Path)
    """
    logger.section("✉️", "Cover Letter Generation")
    logger.info(f"Using model: {COVER_MODEL} (Sonnet — good quality, lower cost)")

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    prompt = COVER_LETTER_PROMPT.format(
        profile_text=profile_summary_for_llm(profile),
        company=jd["company"],
        role=jd["role"],
        key_themes=", ".join(jd.get("key_themes", [])),
        role_summary=jd.get("summary", ""),
        tagline=resume_data.get("tagline", ""),
        tone=profile.get("preferred_tone", "professional, concise"),
    )

    logger.ai_prompt("Cover Letter Generator", prompt[:600] + "...")
    logger.info("Generating cover letter via Claude Sonnet...")

    response = client.messages.create(
        model=COVER_MODEL,
        max_tokens=1500,  # Cover letter JSON is ~400-600 tokens
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    logger.ai_response("Cover Letter Content", raw)

    cleaned = re.sub(r"```json|```", "", raw).strip()
    start = cleaned.find("{")
    end   = cleaned.rfind("}")
    cl_data = json.loads(cleaned[start : end + 1])

    paragraphs = cl_data.get("paragraphs", [])
    full_text = "\n\n".join(paragraphs)

    logger.document("Generated Cover Letter", full_text)
    logger.key_value("Word count", str(cl_data.get("word_count", len(full_text.split()))))

    # ── Render HTML from Jinja2 template ──────────────────────────────────
    env = Environment(loader=FileSystemLoader("templates"))
    template = env.get_template("cover_letter.html")
    html_content = template.render(
        personal=profile["personal"],
        date=datetime.now().strftime("%B %d, %Y"),
        company=jd["company"],
        role=jd["role"],
        paragraphs=paragraphs,
    )

    html_path = run_dir / "cover_letter.html"
    html_path.write_text(html_content, encoding="utf-8")
    logger.info(f"Cover letter HTML saved -> {html_path}")

    pdf_path = run_dir / "cover_letter.pdf"
    await html_to_pdf(page, html_path, pdf_path)
    logger.success(f"Cover letter PDF saved -> {pdf_path}")

    return full_text, pdf_path
