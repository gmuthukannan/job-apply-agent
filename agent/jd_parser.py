"""
jd_parser.py
============
Fetches any job posting URL via Playwright (handles JS-heavy ATS pages)
and uses Claude to extract a structured job description JSON.

Model choice: claude-haiku — this is pure text extraction / classification,
no creativity needed, so the cheapest model is fine here.

Flow:
  1. Playwright navigates to URL and waits for the page to settle
  2. JavaScript strips scripts/styles and returns all visible text
  3. Text is truncated to 8000 chars (enough for any JD, avoids token waste)
  4. Claude receives the raw text and returns structured JSON
"""

import json
import os
import re

import anthropic
from playwright.async_api import Page

from agent.logger import AgentLogger


# ── Model selection ────────────────────────────────────────────────────────────
# Haiku is used here because JD extraction is a structured classification task —
# no reasoning or creativity required. Significantly cheaper than Opus/Sonnet.
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# Maximum characters of page text to send to Claude.
# 8000 chars ≈ ~2000 tokens — enough to cover any job description.
MAX_PAGE_CHARS = 8000


# ── Prompt template ────────────────────────────────────────────────────────────
# {page_text} is injected at runtime with the scraped job posting text.
JD_EXTRACTION_PROMPT = """
You are a precise job description analyst. Given raw text scraped from a job posting page,
extract structured information in JSON format.

Raw page text:
---
{page_text}
---

Return ONLY valid JSON (no markdown, no explanation) with this exact structure:
{{
  "company": "company name",
  "role": "exact job title",
  "location": "location string",
  "employment_type": "full-time / contract / etc",
  "requirements": [
    "each required skill or qualification as a plain string"
  ],
  "nice_to_have": [
    "each preferred/bonus skill as a plain string"
  ],
  "responsibilities": [
    "each key responsibility as a plain string"
  ],
  "key_themes": [
    "3-6 overarching themes that define this role (e.g. AI automation, data pipelines, etc)"
  ],
  "tone": "startup / enterprise / technical / etc",
  "apply_url": "the direct URL to submit an application, or empty string if not found",
  "summary": "2-sentence plain-English summary of what this role is really about"
}}
"""


async def fetch_page_text(page: Page, url: str, logger: AgentLogger) -> tuple[str, str]:
    """
    Navigate to a URL and extract all visible text from the page.

    Uses Playwright to handle JavaScript-rendered pages (Greenhouse, Lever,
    Workday all render job content dynamically — requests.get() wouldn't work).

    Returns:
        (page_text, resolved_url) — the visible text and the final URL after
        any redirects (e.g. LinkedIn shortlinks resolve here).
    """
    logger.info(f"Navigating to: {url}")

    # wait_until="networkidle" means we pause until no network requests have
    # fired for 500ms — ensures dynamic content is fully loaded.
    await page.goto(url, wait_until="networkidle", timeout=30000)

    # Extra buffer for slow ATS pages that lazy-load content after network idle
    await page.wait_for_timeout(2000)

    # Detect login walls (LinkedIn, some Workday instances redirect to auth)
    current_url = page.url
    if "linkedin.com/authwall" in current_url or "login" in current_url:
        logger.warning("Hit a login wall — extracting whatever visible text is available")

    # JavaScript executed inside the browser context:
    # - Removes script/style/noscript tags (they add noise, not content)
    # - Returns innerText of the body (respects CSS visibility)
    text = await page.evaluate("""
        () => {
            // Remove non-content elements before extracting text
            const scripts = document.querySelectorAll('script, style, noscript');
            scripts.forEach(s => s.remove());
            return document.body.innerText;
        }
    """)

    logger.info(f"Extracted {len(text):,} characters from page")
    logger.info(f"Resolved URL: {page.url}")
    return text, page.url


async def parse_jd(page: Page, url: str, logger: AgentLogger) -> dict:
    """
    Full pipeline: fetch the job posting page then extract structured JD via Claude.

    Args:
        page: Playwright page object (browser already open)
        url:  URL of the job posting (may be a shortlink)
        logger: AgentLogger instance for console + file output

    Returns:
        dict with keys: company, role, location, requirements, nice_to_have,
        responsibilities, key_themes, tone, apply_url, summary, source_url
    """
    logger.section("📋", "Job Description Analysis")

    # Step 1: Fetch raw page text
    page_text, resolved_url = await fetch_page_text(page, url, logger)

    # Step 2: Truncate to control token usage.
    # Job descriptions are rarely longer than 3000 chars of real content;
    # 8000 gives plenty of buffer for boilerplate/nav text that precedes the JD.
    truncated = page_text[:MAX_PAGE_CHARS]

    # Step 3: Call Claude (Haiku) to extract structured data
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    prompt = JD_EXTRACTION_PROMPT.format(page_text=truncated)

    logger.ai_prompt("JD Extractor", prompt[:500] + "...")
    logger.info(f"Sending {len(truncated):,} chars to Claude ({CLAUDE_MODEL}) for extraction...")

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,  # JD JSON is rarely more than 800 tokens; 2000 is safe ceiling
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    logger.ai_response("JD Extraction", raw)

    # Robustly extract the JSON object — Claude may append text after the closing brace.
    cleaned = re.sub(r"```json|```", "", raw).strip()
    start = cleaned.find("{")
    end   = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in Claude response: {raw[:200]}")
    jd = json.loads(cleaned[start : end + 1])

    # Attach the resolved URL so downstream code can navigate directly to the form
    jd["source_url"] = resolved_url

    # Log key fields for quick review in the terminal
    logger.key_value("Company",            jd.get("company", "?"))
    logger.key_value("Role",               jd.get("role", "?"))
    logger.key_value("Location",           jd.get("location", "?"))
    logger.key_value("Key themes",         ", ".join(jd.get("key_themes", [])))
    logger.key_value("Requirements count", str(len(jd.get("requirements", []))))

    logger.subsection("Requirements")
    for req in jd.get("requirements", []):
        logger.info(req)

    logger.subsection("Nice to Have")
    for nice in jd.get("nice_to_have", []):
        logger.info(nice)

    logger.subsection("Role Summary")
    logger.info(jd.get("summary", ""))

    return jd
