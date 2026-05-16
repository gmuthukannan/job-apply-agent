"""
form_detector.py
================
Detects the ATS system, extracts all interactive form fields from the page,
and maps each field to the correct answer from the candidate profile.

Two-stage mapping strategy:
  Stage 1 — Rule-based mapper (deterministic, zero API cost):
    Known fields (name, email, pronouns, gender, location, etc.) are matched
    by label keyword and answered directly from the profile. This eliminates
    the most common mismatches (e.g. pronouns getting a location value).

  Stage 2 — Claude Haiku (for unknown/custom fields only):
    Only fields that the rule mapper couldn't handle are sent to Claude.
    Haiku is used because field mapping is a lookup/classification task,
    not a creative or reasoning task.

Model choice: claude-haiku — cheapest model, sufficient for structured mapping.
"""

import json
import os
import re

import anthropic
from playwright.async_api import Page

from agent.logger import AgentLogger
from agent.profile_loader import profile_summary_for_llm


# ── Model selection ────────────────────────────────────────────────────────────
# Haiku is used for field mapping — it's fast, cheap, and accurate enough
# for the lookup-style task of matching form labels to profile values.
CLAUDE_MODEL = "claude-haiku-4-5-20251001"


# ── Rule-based field mapper ────────────────────────────────────────────────────
# Each entry is (label_keyword, answer_function).
# The keyword is checked as a substring of the field label (case-insensitive).
# Entries are checked IN ORDER — first match wins.
#
# Why rule-based instead of Claude for these?
# - 100% reliable (no hallucination risk)
# - Zero API cost
# - Instant (no network round-trip)
# - Fixes the "Surrey in pronouns" class of bugs permanently
RULE_MAP = [
    # ── Demographic / EEO fields ──────────────────────────────────────────
    ("pronoun",             lambda p: p["personal"].get("Pronouns", "")),
    ("gender",              lambda p: p["personal"].get("Gender", "")),
    ("race",                lambda p: p["personal"].get("Race", "")),
    ("ethnicity",           lambda p: p["personal"].get("Race", "")),
    ("veteran",             lambda p: p["personal"].get("Veteran Status", "")),
    ("disability",          lambda p: p["personal"].get("Disability Status", "")),
    ("background check",    lambda p: p["personal"].get("Concent to Background Check", "Yes")),
    ("consent to contact",  lambda p: p["personal"].get("Concent to contact", "Yes")),

    # ── Name fields — split full name appropriately ────────────────────────
    ("first name",          lambda p: p["personal"]["full_name"].split()[0]),
    ("given name",          lambda p: p["personal"]["full_name"].split()[0]),
    ("last name",           lambda p: " ".join(p["personal"]["full_name"].split()[1:])),
    ("surname",             lambda p: " ".join(p["personal"]["full_name"].split()[1:])),
    ("family name",         lambda p: " ".join(p["personal"]["full_name"].split()[1:])),
    ("full name",           lambda p: p["personal"]["full_name"]),
    ("your name",           lambda p: p["personal"]["full_name"]),

    # ── Contact info ───────────────────────────────────────────────────────
    ("email",               lambda p: p["personal"]["email"]),
    ("phone",               lambda p: p["personal"]["phone"]),
    ("mobile",              lambda p: p["personal"]["phone"]),
    ("telephone",           lambda p: p["personal"]["phone"]),

    # ── URLs / profiles ────────────────────────────────────────────────────
    ("linkedin",            lambda p: p["personal"]["linkedin"]),
    ("github",              lambda p: p["personal"]["github"]),
    ("portfolio",           lambda p: p["personal"]["github"]),
    ("website",             lambda p: p["personal"]["github"]),

    # ── Location — return shortest useful search term for listboxes ────────
    # "Surrey" triggers the ATS location search; "Surrey, BC" is for text fields
    ("city",                lambda p: "Surrey"),
    ("current city",        lambda p: "Surrey"),
    ("country",             lambda p: "Canada"),
    ("province",            lambda p: "British Columbia"),
    ("state",               lambda p: "British Columbia"),
    ("location",            lambda p: "Surrey, BC"),

    # ── Work authorization ─────────────────────────────────────────────────
    ("work authori",        lambda p: "Yes, I am authorized to work in Canada"),
    ("visa sponsor",        lambda p: "No"),
    ("require sponsor",     lambda p: "No"),

    # ── Common dropdowns ───────────────────────────────────────────────────
    ("hear about",          lambda p: "LinkedIn"),
    ("source",              lambda p: "LinkedIn"),
    ("salary",              lambda p: "Open to discussion"),
    ("compensation",        lambda p: "Open to discussion"),
    ("education",           lambda p: "Bachelor's Degree"),
]


def rule_based_answer(label: str, profile: dict) -> str | None:
    """
    Check the field label against the RULE_MAP and return a deterministic answer.

    Args:
        label:   The form field label text (as detected from the DOM)
        profile: The loaded candidate profile dict

    Returns:
        Answer string if a rule matched, or None if no rule applies
    """
    label_lower = label.lower()
    for keyword, fn in RULE_MAP:
        if keyword in label_lower:
            return fn(profile)
    return None  # No rule matched — will be sent to Claude


# ── Claude prompt for unmapped fields ─────────────────────────────────────────
FIELD_MAPPING_PROMPT = """
You are filling out a job application form on behalf of a candidate.
Map each detected form field to the correct value using the candidate profile below.

CANDIDATE PROFILE:
{profile_text}

JOB BEING APPLIED FOR:
Company: {company}
Role: {role}

DETECTED FORM FIELDS (only the ones that need Claude — common fields already handled):
{fields_json}

STRICT FIELD MAPPING RULES — follow these exactly, in priority order:

PERSONAL FIELDS:
- Any field labelled: first name, given name -> candidate's first name only
- Any field labelled: last name, surname, family name -> candidate's last name only
- Any field labelled: full name, name -> candidate's full name
- Any field labelled: email, e-mail -> exact email address from profile
- Any field labelled: phone, mobile, telephone -> exact phone number from profile
- Any field labelled: city, location, city/town, current location -> city and province only (e.g. "Surrey, BC")
- Any field labelled: country -> "Canada"
- Any field labelled: address, street -> leave as empty string (use MANUAL_REVIEW_NEEDED)
- Any field labelled: postal code, zip -> use MANUAL_REVIEW_NEEDED
- Any field labelled: linkedin -> exact LinkedIn URL from profile
- Any field labelled: github, portfolio, website -> exact GitHub URL from profile

DOCUMENT FIELDS:
- Any field of type "file" OR labelled: resume, CV, upload resume -> return "UPLOAD_RESUME"
- Any field labelled: cover letter, upload cover letter -> return "UPLOAD_COVER_LETTER"

LISTBOX / COMBOBOX (type-ahead search dropdowns):
- Any field of type "listbox", "combobox", role="listbox", role="combobox", or aria-autocomplete
  -> treat like a text field but return the SEARCH TERM to type (not the full sentence)
- Location / city listbox -> return city name only: "Surrey"
- Country listbox -> "Canada"
- State / province listbox -> "British Columbia"
- Skills listbox -> most relevant skill from profile matching the label
- Any other listbox -> return the shortest unambiguous search term that would appear in a dropdown

WORK AUTHORIZATION:
- Any field about work authorization, right to work, eligible to work -> "Yes, I am authorized to work in Canada"
- Any field about visa sponsorship, require sponsorship -> "No"
- Any field about citizenship status -> "Permanent Resident / Work Authorized"

DEMOGRAPHIC & COMPLIANCE (EEO / voluntary self-identification):
- Any field labelled: pronouns, preferred pronouns -> "He/him/his"
- Any field labelled: gender, gender identity -> "Male" (or closest matching option e.g. "Man")
- Any field labelled: race, ethnicity, race/ethnicity -> "Asian" (or closest matching option)
- Any field labelled: veteran, veteran status, military status -> pick the option closest to "I am not a protected veteran" or "Non-Veteran"
- Any field labelled: disability, disability status -> pick the option closest to "No, I do not have a disability"
- Any field labelled: background check, consent to background check -> "Yes"
- Any field labelled: consent to contact -> "Yes"
- For ALL demographic dropdowns: always return the closest matching option text - NEVER return MANUAL_REVIEW_NEEDED for these fields

EXPERIENCE / DROPDOWNS:
- Any field labelled: years of experience -> calculate from work history and pick the closest option
- Any field labelled: highest education -> "Bachelor's Degree"
- Any field labelled: how did you hear / source -> "LinkedIn"
- Any field labelled: salary, compensation, expected salary -> "Open to discussion"

LONG-TEXT / ESSAY FIELDS:
- Any field labelled: summary, professional summary, about you, bio -> use the candidate's 2-3 sentence professional summary
- Any field labelled: cover letter (textarea, not file) -> write a 150-word professional response
- Any field labelled: why do you want to work here, why this company -> write a 2-3 sentence focused answer
- Any field labelled: describe your experience with X -> write a 2-3 sentence answer using the most relevant evidence story

CRITICAL RULES:
- NEVER put the professional summary text into a location, city, or address field
- NEVER put a URL into a name or text field
- NEVER invent information not in the profile
- If you are not confident about a field, return MANUAL_REVIEW_NEEDED

Return ONLY valid JSON - an array of field objects with answers added:
[
  {{
    "field_id": "...",
    "label": "...",
    "type": "text|email|tel|select|textarea|file|checkbox|radio",
    "selector": "...",
    "answer": "the value to fill in, or UPLOAD_RESUME, or UPLOAD_COVER_LETTER, or MANUAL_REVIEW_NEEDED"
  }}
]
"""


# ── ATS detection ──────────────────────────────────────────────────────────────
# Used for logging only — helps debug ATS-specific issues.
ATS_PATTERNS = {
    "greenhouse":     ["greenhouse.io", "boards.greenhouse.io"],
    "lever":          ["jobs.lever.co", "lever.co"],
    "workday":        ["myworkdayjobs.com", "workday.com"],
    "linkedin":       ["linkedin.com/jobs"],
    "smartrecruiters":["smartrecruiters.com"],
    "ashby":          ["ashbyhq.com"],
    "rippling":       ["app.rippling.com", "ats.rippling.com"],
    "generic":        [],
}


def detect_ats(url: str) -> str:
    """
    Identify which ATS system the URL belongs to.
    Returns the ATS name string (e.g. "greenhouse", "rippling") or "generic".
    Used for logging — the field extraction logic is ATS-agnostic.
    """
    for ats, patterns in ATS_PATTERNS.items():
        if any(p in url for p in patterns):
            return ats
    return "generic"


async def extract_fields_from_page(page: Page) -> list[dict]:
    """
    Scan the page DOM and return all visible, interactive form fields.

    Uses JavaScript executed in the browser context to:
    - Find all input, textarea, select, and ARIA combobox/listbox elements
    - Deduplicate by selector
    - Skip hidden elements (zero width/height)
    - Extract label text using multiple strategies (aria-label, <label for="">,
      parent label, placeholder, preceding sibling text)
    - Capture role and aria-autocomplete attributes for listbox detection

    Returns:
        List of dicts, each with: field_id, label, type, selector,
        options (for selects), required, placeholder, role, aria_autocomplete
    """
    fields = await page.evaluate("""
        () => {
            const results = [];
            const seen = new Set();  // Track selectors to avoid duplicates

            // ── Label detection — multiple fallback strategies ─────────────
            function getLabel(el) {
                // Strategy 0: data-testid — most reliable on Rippling/modern ATS
                if (el.getAttribute('data-testid')) return el.getAttribute('data-testid');

                // Strategy 1: aria-label attribute
                if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');

                // Strategy 2: <label for="id"> association
                if (el.id) {
                    const lbl = document.querySelector(`label[for="${el.id}"]`);
                    if (lbl) return lbl.innerText.trim();
                }

                // Strategy 3: the element is inside a <label>
                const parentLabel = el.closest('label');
                if (parentLabel) return parentLabel.innerText.replace(el.value || '', '').trim();

                // Strategy 4: placeholder text (common for search boxes)
                if (el.placeholder) return el.placeholder;

                // Strategy 5: walk backwards through siblings to find label text
                let prev = el.previousElementSibling;
                while (prev) {
                    const t = prev.innerText?.trim();
                    if (t && t.length > 1 && t.length < 100) return t;
                    prev = prev.previousElementSibling;
                }

                // Last resort: use name or id attribute
                return el.name || el.id || 'unknown';
            }

            // ── CSS selector builder — prefer id, fall back to name attr ──
            function getSelector(el) {
                if (el.id) return `#${el.id}`;
                if (el.name) return `[name="${el.name}"]`;
                return el.tagName.toLowerCase();
            }

            // ── Extract <option> texts for <select> elements ───────────────
            function getOptions(el) {
                if (el.tagName === 'SELECT') {
                    return Array.from(el.options).map(o => o.text.trim()).filter(t => t);
                }
                return [];
            }

            // ── Query all interactive elements including ARIA widgets ───────
            // Standard inputs + ARIA combobox/listbox (used by many modern ATS)
            const elements = document.querySelectorAll(
                'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]):not([disabled]),' +
                'textarea:not([disabled]),' +
                'select:not([disabled]),' +
                '[role="combobox"]:not([disabled]),' +
                '[role="listbox"]:not([disabled]),' +
                '[role="radiogroup"],' +         
                '[aria-autocomplete]:not([disabled])'
            );

            elements.forEach(el => {
                const key = getSelector(el);

                // Skip if we've already seen this selector
                if (seen.has(key)) return;
                seen.add(key);

                // Skip invisible elements (display:none or zero size)
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) return;

                // Determine field type:
                // If the element has a role or aria-autocomplete, mark as listbox
                // so the fill handler knows to use the type-then-pick strategy
                const role = el.getAttribute('role') || '';
                const ariaAuto = el.getAttribute('aria-autocomplete') || '';
                const isListbox = role === 'combobox' || role === 'listbox' || ariaAuto !== '';
                const isRadioGroup = role === 'radiogroup';                    // ← add
                const fieldType = isRadioGroup ? 'radio' : (isListbox ? 'listbox' : (el.type || el.tagName.toLowerCase()));
                                 
                // Special case: Rippling radiogroup — read child radio divs for options
                if (role === 'radiogroup') {
                    const radioChildren = el.querySelectorAll('[role="radio"]');
                    const radioOptions = Array.from(radioChildren).map(r => {
                        const p = r.querySelector('p');
                        return p ? p.innerText.trim() : (r.getAttribute('data-value') || '');
                    }).filter(t => t);
                    const hiddenInput = el.querySelector('input[type="radio"]');
                    results.push({
                        field_id: (hiddenInput ? hiddenInput.getAttribute('name') : null) || el.id || `field_${results.length}`,
                        label:    getLabel(el),
                        type:     'radio',
                        selector: getSelector(el),
                        options:  radioOptions,      // ["Yes – I consent...", "No – I do not..."]
                        required: false,
                        placeholder: '',
                        role:     'radiogroup',
                        aria_autocomplete: ''
                    });
                    return;  // skip the normal results.push below
                }

                results.push({
                    field_id:        el.id || el.name || `field_${results.length}`,
                    label:           getLabel(el),
                    type:            fieldType,
                    selector:        key,
                    options:         getOptions(el),   // empty array for non-selects
                    required:        el.required,
                    placeholder:     el.placeholder || '',
                    role:            role,
                    aria_autocomplete: ariaAuto
                });
            });

            return results;
        }
    """)
    return fields


async def map_fields_to_answers(
    fields: list[dict],
    profile: dict,
    jd: dict,
    logger: AgentLogger,
) -> list[dict]:
    """
    Map each detected field to the correct answer.

    Two-stage process:
      Stage 1 (rule-based): deterministic answers for known field types
      Stage 2 (Claude Haiku): only for fields the rule mapper couldn't handle

    This hybrid approach is both cheaper (fewer API calls) and more reliable
    (rules never hallucinate) than sending everything to Claude.

    Args:
        fields:  List of field dicts from extract_fields_from_page()
        profile: Loaded candidate profile
        jd:      Parsed job description dict
        logger:  Agent logger

    Returns:
        List of field dicts with "answer" key added to each
    """
    resolved = []    # Fields answered by rule mapper
    needs_claude = []  # Fields that need Claude

    for field in fields:
        label = field.get("label", "")
        ftype = field.get("type", "")

        # File inputs are always flagged for the upload handler
        if ftype == "file":
            field["answer"] = "UPLOAD_RESUME"
            field["answer_source"] = "rule:file_input"
            resolved.append(field)
            continue

        # ── Auto-resolve radio fields from their option text ───────────────
        # Radio groups expose their real options (from <p> text inside each
        # div[role="radio"]). We scan those options for consent/yes keywords
        # and pick the matching one directly — no Claude needed.
        if ftype == "radio":
            options = field.get("options", [])
            consent_opt = next(
                (o for o in options if any(
                    kw in o.lower() for kw in ["i consent", "i agree", "yes"]
                )), None
            )
            if consent_opt:
                field["answer"] = consent_opt
                field["answer_source"] = "rule:radio-consent"
                resolved.append(field)
                logger.info(f"  [rule:radio] '{label}' -> '{consent_opt[:60]}'")
            else:
                # No consent option found — send to Claude to decide
                needs_claude.append(field)
            continue

        # Try the rule-based mapper first
        rule_answer = rule_based_answer(label, profile)
        if rule_answer is not None:
            field["answer"] = rule_answer
            field["answer_source"] = "rule"  # Tracks which stage answered this field
            resolved.append(field)
            logger.info(f"  [rule] '{label}' -> '{rule_answer[:60]}'")
        else:
            # No rule matched — needs Claude
            needs_claude.append(field)

    # Only call Claude if there are fields it needs to handle
    if needs_claude:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

        prompt = FIELD_MAPPING_PROMPT.format(
            profile_text=profile_summary_for_llm(profile),
            company=jd.get("company", ""),
            role=jd.get("role", ""),
            fields_json=json.dumps(needs_claude, indent=2),
        )

        logger.ai_prompt("Form Field Mapper (unresolved fields)", prompt[:600] + "...")
        logger.info(f"Asking Claude ({CLAUDE_MODEL}) to map {len(needs_claude)} unresolved field(s)...")

        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()
        logger.ai_response("Form Field Mapping", raw)

        # Claude sometimes appends explanation text after the JSON array despite instructions.
        # Robustly extract just the array: find the first '[' and its matching ']'.
        cleaned = re.sub(r"```json|```", "", raw).strip()
        start = cleaned.find("[")
        end   = cleaned.rfind("]")
        if start == -1 or end == -1:
            logger.warning("Claude response contains no JSON array — skipping Claude-mapped fields")
            claude_mapped = []
        else:
            claude_mapped = json.loads(cleaned[start : end + 1])

        # Tag each Claude-mapped field for debugging
        for f in claude_mapped:
            f["answer_source"] = "claude"

        resolved.extend(claude_mapped)

    return resolved


async def detect_and_map_form(
    page: Page,
    profile: dict,
    jd: dict,
    logger: AgentLogger,
) -> list[dict]:
    """
    Full pipeline: detect ATS -> extract fields -> map to profile answers.

    This is the main entry point called by applicator.py.

    Args:
        page:    Playwright page (should be on the application form)
        profile: Loaded candidate profile
        jd:      Parsed job description
        logger:  Agent logger

    Returns:
        List of field dicts with answers ready for fill_field()
    """
    logger.section("🔍", "Form Detection")

    # Detect and log the ATS type (for debugging, not for logic)
    current_url = page.url
    ats = detect_ats(current_url)
    logger.key_value("ATS System detected", ats)
    logger.key_value("Form URL", current_url)

    # Extract all interactive fields from the page DOM
    logger.info("Scanning page for interactive form fields...")
    fields = await extract_fields_from_page(page)
    logger.info(f"Found {len(fields)} interactive field(s)")

    # For combobox/listbox fields, open each one and read its real options.
    # These options are not in the DOM until the field is activated, so we
    # click → read → Escape for each one. Results are stored in field["options"]
    # and saved to form_fields.json so you can inspect them without re-running.
    logger.info("Reading combobox options...")
    for f in fields:
        is_combobox = (
            f.get("role") in ("combobox", "listbox")
            or f.get("aria_autocomplete")
            or f.get("type") == "listbox"
        )
        if not is_combobox or f.get("type") == "file":
            continue

        try:
            selector = f.get("selector", "")
            if not selector:
                continue

            locator = page.locator(selector).first
            if await locator.count() == 0 or not await locator.is_visible():
                continue

            # Check aria-controls / aria-owns for linked listbox id
            controlled_id = await page.evaluate("""
                (sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return '';
                    return el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || '';
                }
            """, selector)

            # Click to open the dropdown
            await locator.click()
            await page.wait_for_timeout(600)

            # Build selector list — try controlled id first, then generic
            option_selectors = []
            if controlled_id:
                option_selectors += [
                    f"#{controlled_id} [role='option']",
                    f"#{controlled_id} li",
                ]
            option_selectors += [
                "[role='listbox'] [role='option']",
                "[role='option']",
                "[role='listbox'] li",
                "ul[role='listbox'] li",
                "[class*='option']",
                "[class*='item']",
            ]

            combobox_options: list[str] = []
            for sel in option_selectors:
                opts = page.locator(sel)
                n = await opts.count()
                if n == 0:
                    continue
                for i in range(min(n, 50)):   # read up to 50 options
                    try:
                        text = (await opts.nth(i).inner_text()).strip()
                        if text and text not in combobox_options:
                            combobox_options.append(text)
                    except Exception:
                        continue
                if combobox_options:
                    break  # found options — stop trying selectors

            # Store in the field dict so it's saved to form_fields.json
            if combobox_options:
                f["options"] = combobox_options
                logger.info(
                    f"  [combobox-options] '{f['label']}': "
                    f"{combobox_options[:6]}"
                    + (" ..." if len(combobox_options) > 6 else "")
                )

            # Close without selecting
            await locator.press("Escape")
            await page.wait_for_timeout(200)

        except Exception as e:
            logger.warning(f"  [combobox-options] Could not read options for '{f.get('label', '?')}': {e}")

    # Log each detected field — now includes combobox options
    for f in fields:
        opts_list = f.get("options", [])
        opts_str  = f" [options: {', '.join(opts_list[:6])}{'...' if len(opts_list) > 6 else ''}]" if opts_list else ""
        role_str  = f" [role={f['role']}]" if f.get("role") else ""
        logger.info(f"  Field: [{f['type']}] '{f['label']}'{role_str}{opts_str}")

    # Map fields to answers (rule-based + Claude)
    mapped = await map_fields_to_answers(fields, profile, jd, logger)

    # Log the final mapping table
    logger.subsection("Field -> Answer Mapping")
    logger.field_table_header()
    for field in mapped:
        source = field.get("answer_source", "?")
        answer = field.get("answer", "")
        logger.field_fill(
            f"{field.get('label', '?')} [{source}]",
            field.get("type", "?"),
            answer,
        )

    return mapped
