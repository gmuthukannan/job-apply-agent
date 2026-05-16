"""
applicator.py
=============
Drives the browser to fill and submit the job application form.

Responsibilities:
  - navigate_to_form():         Go to the job URL; skip apply button if already on form
  - upload_documents():         Upload resume + cover letter (handles hidden file inputs)
  - _fill_generic_dropdown():   For fields labelled "Select"/"Search" — arrow-navigate
                                 and pick the closest demographic option
  - _fill_listbox():            Type-then-pick for combobox/search dropdowns
  - fill_field():               Fill a single field by type (text, select, radio, etc.)
  - take_screenshot():          Save a full-page or viewport screenshot
  - apply():                    Master pipeline orchestrating all of the above

No Claude API calls are made in this file — all mapping happens in form_detector.py.

Dry vs Live mode:
  dry:  Fills the form, keeps browser open for review (Ctrl+C to exit)
  live: Fills the form and clicks Submit
"""

import json
import re
from pathlib import Path

from playwright.async_api import Page

from agent.logger import AgentLogger
from agent.form_detector import detect_and_map_form


# ── Submit button selectors (tried in order) ──────────────────────────────────
SUBMIT_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('Submit')",
    "button:has-text('Apply')",
    "button:has-text('Submit Application')",
    "[data-qa='btn-submit']",
    ".submit-btn",
    "#submit-app",
]

# ── Generic placeholder labels ─────────────────────────────────────────────────
# When a dropdown's label is one of these, we can't infer intent from the label.
# Instead we open the dropdown, read all options, and pick the closest demographic match.
GENERIC_LABELS = {"select", "select...", "search", "--", "---", "choose", "choose..."}

# ── Preferred demographic values (checked in order) ───────────────────────────
# For unlabelled dropdowns we scan the option list and pick the first entry that
# matches any of these target strings (case-insensitive substring match).
# Order matters: more specific strings are listed before shorter/ambiguous ones.
PREFERRED_OPTION_TARGETS = [
    # Pronouns
    "he/him/his", "he/him", "he / him",
    # Gender
    "male", "man",
    # Veteran — common ATS phrasings
    "i am not a protected veteran",
    "not a protected veteran",
    "i don't consider myself one of the above",
    "not a veteran",
    # Disability — common ATS phrasings
    "i don't have a disability",
    "i do not have a disability",
    "no disability",
    "choose not to disclose",
    "decline to self-identify",
    "prefer not to answer",
    # Generic positive / consent
    "yes",
    "i consent",
    "i agree",
]

# ── Consent keywords for radio buttons ────────────────────────────────────────
# When filling a radio group, we prefer options whose surrounding text contains
# one of these keywords (in priority order).
CONSENT_KEYWORDS = ["i consent", "i agree", "consent", "agree", "yes, i"]


async def navigate_to_form(page: Page, job_url: str, logger: AgentLogger) -> str:
    """
    Navigate to the job URL. Handles LinkedIn shortlinks by following redirects.
    If the URL already points to an application form, skips the Apply button search.
    Returns the final URL we landed on.
    """
    logger.info(f"Navigating to job URL: {job_url}")
    await page.goto(job_url, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(2000)

    final_url = page.url
    logger.info(f"Resolved URL: {final_url}")

    # If we're already on an application/apply page, don't hunt for an Apply button
    apply_keywords = ["/apply", "apply?", "application", "step=application"]
    if any(kw in final_url.lower() for kw in apply_keywords):
        logger.info("Already on application form page — skipping Apply button search")
        return final_url

    # Otherwise look for an Apply button to navigate to the form
    apply_selectors = [
        "a:has-text('Apply Now')",
        "a:has-text('Apply')",
        "button:has-text('Apply Now')",
        "[data-qa='btn-apply']",
        ".apply-button",
        "#apply-button",
    ]
    for sel in apply_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible():
                # Skip disabled buttons
                is_disabled = await btn.get_attribute("aria-disabled") or ""
                is_disabled_attr = await btn.get_attribute("disabled")
                if is_disabled.lower() == "true" or is_disabled_attr is not None:
                    logger.info(f"Skipping disabled button: {sel}")
                    continue
                logger.info(f"Found apply button: {sel} — clicking")
                await btn.click(timeout=5000)
                await page.wait_for_timeout(2000)
                break
        except Exception as e:
            logger.info(f"Skipping selector {sel}: {e}")
            continue

    return page.url


async def upload_documents(
    page: Page,
    resume_pdf: Path,
    cover_letter_pdf: Path,
    logger: AgentLogger,
):
    """
    Upload resume and cover letter.
    - Finds all input[type=file] elements and classifies by surrounding text.
    - Also searches for styled cover letter upload buttons/areas separately.
    - Falls back to file chooser API when set_input_files fails.
    """
    logger.subsection("Document Upload")

    async def try_set_files(inp, file_path: Path, label: str) -> bool:
        """Try set_input_files first, fall back to file chooser."""
        # Attempt 1: set_input_files directly (works if input accepts it headlessly)
        try:
            await inp.set_input_files(str(file_path))
            logger.file_upload(label, str(file_path))
            return True
        except Exception:
            pass
        # Attempt 2: Make the hidden input temporarily visible, then set files
        try:
            el = await inp.element_handle()
            await page.evaluate(
                "el => { el.style.display='block'; el.style.visibility='visible'; el.style.opacity='1'; }",
                el
            )
            await inp.set_input_files(str(file_path))
            logger.file_upload(f"{label} (forced visible)", str(file_path))
            return True
        except Exception:
            pass

        # Attempt 3: Find the visible button/label that wraps this input and
        # intercept the file chooser dialog it opens (Rippling pattern)
        try:
            el = await inp.element_handle()

            # Walk up DOM to find the parent <label> or a sibling <button>
            trigger_testid = await page.evaluate("""
                (el) => {
                    // Check parent label for a button with data-testid
                    const lbl = el.closest('label');
                    if (lbl) {
                        const btn = lbl.querySelector('button[data-testid]');
                        if (btn) return '[data-testid="' + btn.getAttribute('data-testid') + '"]';
                        // No button — clicking the label itself triggers the dialog
                        if (lbl.getAttribute('data-testid'))
                            return '[data-testid="' + lbl.getAttribute('data-testid') + '"]';
                    }
                    // Fallback: nearest preceding button
                    let node = el.previousElementSibling;
                    while (node) {
                        if (node.tagName === 'BUTTON') return null;
                        node = node.previousElementSibling;
                    }
                    return null;
                }
            """, el)

            click_target = (
                page.locator(trigger_testid).first
                if trigger_testid
                else page.locator("label").filter(has=inp).first
            )

            async with page.expect_file_chooser(timeout=8000) as fc_info:
                await click_target.click()
            fc = await fc_info.value
            await fc.set_files(str(file_path))
            logger.file_upload(f"{label} (via button/label trigger)", str(file_path))
            return True

        except Exception as e:
            logger.warning(f"Upload failed for {label}: {e}")
            return False
    

    async def get_surrounding_text(inp) -> str:
        try:
            return await page.evaluate("""
                (el) => {
                    // Check data-testid first — most reliable on Rippling
                    const testId = el.getAttribute('data-testid') || '';
                    if (testId) return testId.toLowerCase();

                    let node = el.parentElement;
                    for (let d = 0; d < 6; d++) {
                        if (!node) break;
                        const t = (node.innerText || '').trim();
                        if (t.length > 1 && t.length < 300) return t.toLowerCase();
                        node = node.parentElement;
                    }
                    return (el.getAttribute('aria-label') || el.getAttribute('name') || '').toLowerCase();
                }
            """, await inp.element_handle())
        except Exception:
            return ""

    file_inputs = page.locator("input[type='file']")
    count = await file_inputs.count()
    logger.info(f"Found {count} file input(s) on page")

    uploaded_resume = False
    uploaded_cover = False

    for i in range(count):
        inp = file_inputs.nth(i)
        surrounding = await get_surrounding_text(inp)
        logger.info(f"File input {i+1} context: '{surrounding[:80]}'")

        is_cover = any(w in surrounding for w in ["cover", "letter"])
        is_resume = any(w in surrounding for w in ["resume", "cv"]) or not is_cover

        if is_cover and not uploaded_cover:
            uploaded_cover = await try_set_files(inp, cover_letter_pdf, f"Cover Letter (input {i+1})")
        elif is_resume and not uploaded_resume:
            uploaded_resume = await try_set_files(inp, resume_pdf, f"Resume (input {i+1})")
        await page.wait_for_timeout(800)

    # ── Separate search for cover letter upload if not yet uploaded ───────
    if not uploaded_cover:
        logger.info("Searching for separate cover letter upload area…")
        cover_triggers = [
            "[data-testid='input-cover-letter']",     
            "[data-testid*='cover' i]",                
            "[aria-label*='cover' i]",
            "[aria-label*='Cover' i]",
            "label:has-text('Cover Letter')",
            "button:has-text('Upload Cover')",
            "button:has-text('Attach Cover')",
            "[data-testid*='cover' i]",
            "[placeholder*='cover' i]",
            "div:has-text('Cover Letter') input[type='file']",
        ]
        for sel in cover_triggers:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    tag = await el.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "input":
                        uploaded_cover = await try_set_files(el, cover_letter_pdf, "Cover Letter (targeted)")
                    else:
                        async with page.expect_file_chooser(timeout=5000) as fc_info:
                            await el.click()
                        fc = await fc_info.value
                        await fc.set_files(str(cover_letter_pdf))
                        logger.file_upload("Cover Letter (chooser trigger)", str(cover_letter_pdf))
                        uploaded_cover = True
                    if uploaded_cover:
                        break
            except Exception:
                continue

    # ── No file inputs at all — try generic upload triggers ──────────────
    if count == 0 and not uploaded_resume:
        logger.warning("No file inputs found — trying visible upload buttons")
        for sel in ["button:has-text('Upload')", "button:has-text('Choose file')", "label:has-text('resume')"]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    async with page.expect_file_chooser(timeout=5000) as fc_info:
                        await el.click()
                    fc = await fc_info.value
                    await fc.set_files(str(resume_pdf))
                    logger.file_upload("Resume (generic trigger)", str(resume_pdf))
                    uploaded_resume = True
                    break
            except Exception:
                continue

    if not uploaded_resume:
        logger.warning("⚠ Resume was NOT uploaded — manual upload required")
    if not uploaded_cover:
        logger.info("Cover letter upload area not found — may not be required for this form")


async def _fill_generic_dropdown(
    page: Page,
    locator,
    label: str,
    logger: AgentLogger,
):
    """
    Change 2 — Handle dropdowns whose label is a generic placeholder
    ("Select", "Select...", "Search", "--", etc.).

    Because the label gives us no hint about the field's purpose, we:
      1. Press ArrowDown to open the dropdown and reveal options
      2. Read all visible option texts one by one
      3. Score each option against PREFERRED_OPTION_TARGETS (demographic values)
      4. Click the best-scoring option

    This reliably handles unlabelled gender, pronoun, veteran, and disability
    dropdowns that Claude can't map by label alone.
    """
    logger.info(f"  [generic-dropdown] '{label}' — opening with ArrowDown to read options")

    try:
        # Click to focus the field first
        await locator.click()
        await page.wait_for_timeout(300)

        # ArrowDown opens the dropdown on most ATS implementations
        await locator.press("ArrowDown")
        await page.wait_for_timeout(600)  # wait for option list to render

        # Gather all visible option elements
        option_selectors = [
            "[role='option']",
            "[role='listbox'] li",
            "ul[role='listbox'] li",
            "[class*='option']",
            "[class*='item']",
            "li[data-value]",
        ]

        all_texts: list[str] = []
        working_sel = None

        # Find which selector produces options on this page
        for sel in option_selectors:
            opts = page.locator(sel)
            n = await opts.count()
            if n > 0:
                for i in range(min(n, 30)):
                    try:
                        t = (await opts.nth(i).inner_text()).strip()
                        if t:
                            all_texts.append(t)
                    except Exception:
                        continue
                if all_texts:
                    working_sel = sel
                    break

        if not all_texts:
            logger.warning(f"  [generic-dropdown] No options found for '{label}' — pressing Escape")
            await locator.press("Escape")
            return

        logger.info(f"  [generic-dropdown] {len(all_texts)} options found: {all_texts[:6]}")

        # Score each option against PREFERRED_OPTION_TARGETS.
        # Lower score index = higher priority.
        best_text: str | None = None
        best_score: int = 9999

        for opt_text in all_texts:
            opt_lower = opt_text.lower()
            for score, target in enumerate(PREFERRED_OPTION_TARGETS):
                if target in opt_lower and score < best_score:
                    best_score = score
                    best_text = opt_text

        if best_text:
            # Click the matching option by its exact text
            opt_locator = page.locator(f"{working_sel}").filter(has_text=best_text).first
            await opt_locator.click(timeout=3000)
            logger.field_fill(label, "generic-dropdown", f"→ '{best_text}' (score {best_score})")
        else:
            # No preferred match — press Escape and log for manual review
            await locator.press("Escape")
            logger.warning(f"  [generic-dropdown] No preferred match in options for '{label}' — MANUAL REVIEW NEEDED")

    except Exception as e:
        logger.warning(f"  [generic-dropdown] Failed for '{label}': {e}")
        try:
            await locator.press("Escape")
        except Exception:
            pass


async def get_combobox_options(page: Page, locator, logger: AgentLogger) -> list[str]:
    """
    Read the available options from a role="combobox" field.

    Combobox options are NOT in the DOM until the field is activated.
    They live in a separate listbox element linked via:
      - aria-controls="<listbox-id>"   (most common)
      - aria-owns="<listbox-id>"       (alternative)
      - Or just appended to <body> when opened

    Strategy:
      1. Check aria-controls / aria-owns for a pre-existing listbox id
      2. Click to open the combobox
      3. Wait for [role="option"] elements to appear
      4. Read and return all option texts
    """
    options: list[str] = []

    try:
        # Step 1: Check if aria-controls / aria-owns points to a listbox id
        controlled_id = await page.evaluate("""
            (el) => el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || ''
        """, await locator.element_handle())

        # Step 2: Click to open the dropdown
        await locator.click()
        await page.wait_for_timeout(600)

        # Step 3: Find the listbox — try controlled id first, then generic selectors
        listbox_selectors = []
        if controlled_id:
            listbox_selectors.append(f"#{controlled_id} [role='option']")
            listbox_selectors.append(f"#{controlled_id} li")

        # Generic fallbacks (most ATS append the listbox to <body> or near the input)
        listbox_selectors += [
            "[role='listbox'] [role='option']",
            "[role='option']",
            "[role='listbox'] li",
            "ul[role='listbox'] li",
            "[class*='dropdown'] [class*='option']",
            "[class*='menu'] [class*='item']",
        ]

        for sel in listbox_selectors:
            opts = page.locator(sel)
            count = await opts.count()
            if count == 0:
                continue
            for i in range(min(count, 50)):     # read up to 50 options
                try:
                    text = (await opts.nth(i).inner_text()).strip()
                    if text and text not in options:
                        options.append(text)
                except Exception:
                    continue
            if options:
                break   # found options — stop trying selectors

        # Step 4: Log what we found for debugging
        if options:
            logger.info(f"  [combobox] {len(options)} options found: {options[:8]}"
                        + (" ..." if len(options) > 8 else ""))
        else:
            logger.warning("  [combobox] No options found after opening — field may need typing first")

        # Close the dropdown without selecting anything (Escape)
        await locator.press("Escape")
        await page.wait_for_timeout(200)

    except Exception as e:
        logger.warning(f"  [combobox] get_combobox_options failed: {e}")

    return options



async def _fill_listbox(
    page: Page,
    locator,
    selector: str,
    answer: str,
    label: str,
    logger: AgentLogger,
    field: dict | None = None,
):
    """
    Handle combobox / listbox fields where we have a known answer to fill.

    For role="combobox" fields, get_combobox_options() reads the real available
    options first so we can match exactly instead of guessing.

    Strategies in order:
      1. (combobox only) Read real options via aria-controls → match → click
      2. Click to open → scan visible options → match → click
      3. Type short search term → wait for filtering → match → click
      4. ArrowDown → scan appeared options → click best or Enter
    """
    answer_lower = answer.lower()
    search_term = re.split(r"[/\s,]", answer)[0].strip()
    field = field or {}

    async def click_option_from_texts(option_texts: list[str]) -> bool:
        """Match answer against a list of option text strings and click the best."""
        for text in option_texts:
            if text.lower() == answer_lower:
                try:
                    opt = page.locator("[role='option']").filter(has_text=text).first
                    if await opt.count() == 0:
                        opt = page.locator("li").filter(has_text=text).first
                    await opt.click(timeout=3000)
                    logger.field_fill(label, "listbox", f"{answer} -> exact: '{text}'")
                    return True
                except Exception:
                    pass
        for text in option_texts:
            if answer_lower.startswith(text.lower()) or text.lower().startswith(answer_lower):
                try:
                    opt = page.locator("[role='option']").filter(has_text=text).first
                    await opt.click(timeout=3000)
                    logger.field_fill(label, "listbox", f"{answer} -> prefix: '{text}'")
                    return True
                except Exception:
                    pass
        for text in option_texts:
            if search_term.lower() in text.lower():
                try:
                    opt = page.locator("[role='option']").filter(has_text=text).first
                    await opt.click(timeout=3000)
                    logger.field_fill(label, "listbox", f"{answer} -> partial: '{text}'")
                    return True
                except Exception:
                    pass
        return False

    async def pick_best_option() -> bool:
        """Scan all currently visible options using standard selectors."""
        candidate_selectors = [
            "[role='option']",
            "[role='listbox'] li",
            "ul[role='listbox'] li",
            ".dropdown-menu li",
            "[class*='option']",
            "[class*='item']",
            "[class*='suggestion']",
            "[class*='result']",
            "li[data-value]",
        ]
        for sel in candidate_selectors:
            try:
                opts = page.locator(sel)
                n = await opts.count()
                if n == 0:
                    continue
                texts: list[tuple[int, str]] = []
                for i in range(min(n, 20)):
                    try:
                        t = (await opts.nth(i).inner_text()).strip()
                        if t:
                            texts.append((i, t))
                    except Exception:
                        continue
                if not texts:
                    continue
                for idx, text in texts:
                    if text.lower() == answer_lower:
                        await opts.nth(idx).click(timeout=3000)
                        logger.field_fill(label, "listbox", f"{answer} -> exact: '{text}'")
                        return True
                for idx, text in texts:
                    if answer_lower.startswith(text.lower()) or text.lower().startswith(answer_lower):
                        await opts.nth(idx).click(timeout=3000)
                        logger.field_fill(label, "listbox", f"{answer} -> prefix: '{text}'")
                        return True
                for idx, text in texts:
                    if search_term.lower() in text.lower():
                        await opts.nth(idx).click(timeout=3000)
                        logger.field_fill(label, "listbox", f"{answer} -> partial: '{text}'")
                        return True
            except Exception:
                continue
        return False

    try:
        # Strategy 1 (combobox only): read real options, match, click
        if field.get("role") == "combobox" or field.get("aria_autocomplete"):
            logger.info(f"  [combobox] Reading real options for '{label}'...")
            real_options = await get_combobox_options(page, locator, logger)
            if real_options:
                await locator.click()          # re-open after Escape in get_combobox_options
                await page.wait_for_timeout(400)
                if await click_option_from_texts(real_options):
                    return

        # Strategy 2: click to open, scan visible options
        await locator.click()
        await page.wait_for_timeout(600)
        if await pick_best_option():
            return

        # Strategy 3: type short search term, wait for filtered results
        await locator.fill("")
        await locator.type(search_term, delay=80)
        await page.wait_for_timeout(900)
        if await pick_best_option():
            return

        # Strategy 4: ArrowDown, pick or Enter
        await locator.press("ArrowDown")
        await page.wait_for_timeout(300)
        first_options = page.locator("[role='option']")
        if await first_options.count() > 0:
            texts = []
            for i in range(min(await first_options.count(), 20)):
                try:
                    t = (await first_options.nth(i).inner_text()).strip()
                    if t:
                        texts.append((i, t))
                except Exception:
                    continue
            best = next(
                ((i, t) for i, t in texts if search_term.lower() in t.lower()),
                texts[0] if texts else None,
            )
            if best:
                await first_options.nth(best[0]).click(timeout=3000)
                logger.field_fill(label, "listbox", f"{answer} -> arrow+pick: '{best[1]}'")
                return

        await locator.press("Enter")
        logger.warning(f"  [listbox] All strategies failed for '{label}' — MANUAL REVIEW NEEDED")

    except Exception as e:
        logger.warning(f"  [listbox] Exception for '{label}': {e}")


async def fill_field(page: Page, field: dict, logger: AgentLogger):
    """
    Fill a single non-file form field based on its mapped answer.

    Handles: text, email, tel, textarea, select, listbox/combobox,
             checkbox, radio (with consent detection).

    Special cases:
      - Generic labels ("Select", "Search"): uses _fill_generic_dropdown()
      - Radio buttons: prefers options with consent/agree text (Change 3)
      - Already-filled fields: skipped (checks current value before writing)
      - MANUAL_REVIEW_NEEDED / file uploads: skipped with a log message
    """
    selector  = field.get("selector", "")
    answer    = field.get("answer", "")
    field_type = field.get("type", "text")
    label     = field.get("label", selector)

    # Skip blanks, file uploads (handled by upload_documents()), and unfilled markers
    if not selector or not answer:
        return
    if answer in ("UPLOAD_RESUME", "UPLOAD_COVER_LETTER"):
        return
    if field_type == "file":
        return
    if answer == "MANUAL_REVIEW_NEEDED":
        logger.warning(f"  [skip] Manual review needed for: '{label}'")
        return

    try:
        locator = page.locator(selector).first
        if await locator.count() == 0:
            logger.warning(f"  [not found] Selector '{selector}' ('{label}')")
            return
        if not await locator.is_visible():
            return

        # ── Skip already-filled fields ─────────────────────────────────────
        # ATS parsers often pre-fill fields after resume upload.
        # Reading the current value avoids overwriting correct data.
        try:
            if field_type in ("select", "select-one"):
                selected_text = await locator.evaluate(
                    "el => el.options[el.selectedIndex]?.text?.trim() || ''"
                )
                # These placeholder texts mean the field is still empty
                default_words = ("", "select", "please select", "choose", "--", "none", "select...")
                if selected_text.lower() not in default_words:
                    logger.info(f"  [skip] '{label}' already set to '{selected_text}'")
                    return
            elif field_type not in ("checkbox", "radio", "file"):
                current = await locator.input_value()
                if current.strip():
                    logger.info(f"  [skip] '{label}' already filled: '{current[:50]}'")
                    return
        except Exception:
            pass  # If we can't read current value, proceed to fill anyway

        # ── Change 2: Generic label → open + scan + pick demographic match ─
        # If the label is a placeholder like "Select" or "Search", we can't
        # infer the field's purpose from the label. Open the dropdown and
        # pick the closest match from PREFERRED_OPTION_TARGETS instead.
        if label.lower().strip() in GENERIC_LABELS and field_type in (
            "listbox", "combobox", "select", "select-one"
        ):
            await _fill_generic_dropdown(page, locator, label, logger)
            return

        # ── Listbox / combobox (type-ahead search) ─────────────────────────
        if (
            field_type in ("listbox", "combobox")
            or field.get("role") in ("combobox", "listbox")
            or field.get("aria_autocomplete")
        ):
            await _fill_listbox(page, locator, selector, answer, label, logger, field)
            return

        # ── Standard <select> dropdown ─────────────────────────────────────
        if field_type in ("select", "select-one"):
            try:
                await locator.select_option(label=answer)
            except Exception:
                try:
                    await locator.select_option(value=answer)
                except Exception:
                    # Partial label match — find closest option text
                    try:
                        options = await locator.locator("option").all_text_contents()
                        match = next(
                            (o for o in options
                             if answer.lower() in o.lower() or o.lower() in answer.lower()),
                            None,
                        )
                        if match:
                            await locator.select_option(label=match)
                        else:
                            logger.warning(
                                f"  [select] No match for '{label}': "
                                f"wanted '{answer}', options: {options[:5]}"
                            )
                    except Exception as e3:
                        logger.warning(f"  [select] Failed for '{label}': {e3}")
            logger.field_fill(label, "select", answer)
            return

        # ── Checkbox ────────────────────────────────────────────────────────
        if field_type == "checkbox":
            if answer.lower() in ("true", "yes", "1", "checked"):
                await locator.check()
            else:
                await locator.uncheck()
            logger.field_fill(label, "checkbox", answer)
            return

        # ── Change 3: Radio button — prefer "I consent" / "I agree" options ─
        # Many ATS forms have a radio group for background check consent.
        # We first look for the option whose surrounding text contains consent
        # keywords, before falling back to value-based matching.
        if field_type == "radio":
            # Rippling pattern: role="radiogroup" container with div[role="radio"] children.
            # Each child has a <p> with the label text. The hidden input is inside but
            # has zero size — we click the div, not the input.
            selector_base = selector

            # Try role="radio" children of this field first (Rippling/ARIA pattern)
            radio_divs = page.locator(f"{selector_base} [role='radio']")
            use_divs = await radio_divs.count() > 0

            if use_divs:
                count = await radio_divs.count()
                # Pass 1: match consent keywords against <p> text inside each div
                for i in range(count):
                    div = radio_divs.nth(i)
                    try:
                        p_text = ""
                        p = div.locator("p").first
                        if await p.count() > 0:
                            p_text = (await p.inner_text()).lower()
                        if not p_text:
                            p_text = (await div.inner_text()).lower()
                        for kw in CONSENT_KEYWORDS:
                            if kw in p_text:
                                await div.click()
                                logger.field_fill(label, "radio-consent", f"'{kw}' -> '{p_text[:60]}'")
                                return
                    except Exception:
                        continue

                # Pass 2: match data-value="true" or answer substring
                for i in range(count):
                    div = radio_divs.nth(i)
                    try:
                        dv = (await div.get_attribute("data-value") or "").lower()
                        if dv in answer.lower() or answer.lower() in dv:
                            await div.click()
                            logger.field_fill(label, "radio", f"data-value='{dv}'")
                            return
                    except Exception:
                        continue

            else:
                # Standard input[type="radio"] fallback
                name_attr = field.get("field_id", "")
                radios = page.locator(f"[name='{name_attr}']") if name_attr else page.locator("input[type='radio']")
                count = await radios.count()
                for i in range(count):
                    radio = radios.nth(i)
                    try:
                        surrounding = await page.evaluate("""
                            (el) => {
                                const p = el.closest('[role="radio"]')?.querySelector('p');
                                if (p) return p.innerText.toLowerCase();
                                let node = el.parentElement;
                                for (let d = 0; d < 4; d++) {
                                    if (!node) break;
                                    const t = (node.innerText || '').trim();
                                    if (t.length > 0 && t.length < 300) return t.toLowerCase();
                                    node = node.parentElement;
                                }
                                return (el.getAttribute('value') || '').toLowerCase();
                            }
                        """, await radio.element_handle())
                        for kw in CONSENT_KEYWORDS:
                            if kw in surrounding:
                                await radio.click()
                                logger.field_fill(label, "radio-consent", f"'{kw}' found")
                                return
                    except Exception:
                        continue
                for i in range(count):
                    radio = radios.nth(i)
                    val = await radio.get_attribute("value") or ""
                    if val.lower() in answer.lower() or answer.lower() in val.lower():
                        await radio.check()
                        logger.field_fill(label, "radio", val)
                        return

            logger.warning(f"  [radio] No matching option for '{label}'")
            return

        # ── Text / email / tel / textarea ───────────────────────────────────
        await locator.click()
        await locator.fill("")        # clear any existing value first
        await locator.type(answer, delay=25)  # type slowly to trigger JS listeners
        logger.field_fill(label, field_type, answer)

    except Exception as e:
        logger.warning(f"  [error] Could not fill '{label}': {e}")



async def take_screenshot(page: Page, name: str, screenshots_dir: Path, logger: AgentLogger) -> str:
    """Take a full-page screenshot and save it."""
    path = screenshots_dir / f"{name}.png"
    await page.screenshot(path=str(path), full_page=True)
    logger.screenshot(str(path))
    return str(path)


async def apply(
    page: Page,
    job_url: str,
    profile: dict,
    jd: dict,
    resume_pdf: Path,
    cover_letter_pdf: Path,
    mode: str,
    run_dir: Path,
    logger: AgentLogger,
    wait_for_human=None,
):
    """
    Full application pipeline:
    1. Navigate to form
    2. Screenshot the blank form
    3. Detect + map all fields
    4. Fill every field
    5. Upload documents
    6. Screenshot the filled form
    7. Submit (live mode) or stop (dry mode)
    """
    logger.section("🤖", "Form Automation")
    logger.key_value("Run mode", mode.upper())

    screenshots_dir = run_dir / "screenshots"
    screenshots_dir.mkdir(exist_ok=True)

    # Step 1: Navigate
    form_url = await navigate_to_form(page, job_url, logger)
    logger.key_value("Form URL", form_url)

    # Step 2: Screenshot blank form
    await take_screenshot(page, "01_blank_form", screenshots_dir, logger)

    # Step 3: Upload resume FIRST — most ATS parse it and pre-fill fields
    logger.section("📎", "Uploading Resume (before form fill)")
    await upload_documents(page, resume_pdf, cover_letter_pdf, logger)
    await page.wait_for_timeout(2500)  # give ATS time to parse and auto-fill

    # Step 4: Detect + map fields (after resume parse, ATS may have pre-filled some)
    mapped_fields = await detect_and_map_form(page, profile, jd, logger)

    # Save field map JSON for audit
    fields_json_path = run_dir / "form_fields.json"
    fields_json_path.write_text(json.dumps(mapped_fields, indent=2), encoding="utf-8")
    logger.info(f"Field map saved → {fields_json_path}")

    # Step 5: Fill remaining fields
    logger.section("✏️", "Filling Form Fields")
    for field in mapped_fields:
        await fill_field(page, field, logger)
        await page.wait_for_timeout(150)

    await page.wait_for_timeout(1000)

    # Step 6: Screenshot filled form
    await take_screenshot(page, "02_filled_form", screenshots_dir, logger)
    logger.success("All fields filled")

    # Step 7: Submit or dry-run
    logger.section("🚀", "Submission")

    if mode == "dry":
        logger.warning("DRY RUN — form filled but NOT submitted.")
        logger.info("Browser left open for review — press Ctrl+C in the terminal when done.")
        logger.info(f"Screenshots also saved in: {screenshots_dir}")
        await page.wait_for_timeout(86400000)  # hold browser open for up to 24h (Ctrl+C to exit)
        return "DRY_RUN — not submitted"

    # LIVE mode: find and click submit button
    logger.info("LIVE MODE — locating submit button…")
    submitted = False
    for sel in SUBMIT_SELECTORS:
        try:
            btn = page.locator(sel).first
            if await btn.count() == 0 or not await btn.is_visible():
                continue
            is_disabled = await btn.get_attribute("aria-disabled") or ""
            is_disabled_attr = await btn.get_attribute("disabled")
            if is_disabled.lower() == "true" or is_disabled_attr is not None:
                logger.info(f"Submit button found but disabled ({sel}) — form may have validation errors")
                continue
            logger.info(f"Clicking submit: {sel}")
            await btn.click(timeout=5000)
            await page.wait_for_timeout(3000)
            await take_screenshot(page, "03_submitted", screenshots_dir, logger)
            submitted = True
            logger.success("Application submitted!")
            await take_screenshot(page, "03_submitted", screenshots_dir, logger)

            # Slowly scroll to bottom so Cloudflare renders
            logger.info("Scrolling to bottom for Cloudflare verification...")
            for _ in range(20):
                await page.evaluate("window.scrollBy(0, 300)")
                await page.wait_for_timeout(300)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)
            await take_screenshot(page, "04_bottom_of_page", screenshots_dir, logger)

            # ── Human-in-the-loop checkpoint ──────────────────────────────
            # Yield control to the user for Cloudflare / any manual step.
            # Agent waits here until Enter is pressed in the terminal.
            if wait_for_human:
                await wait_for_human(
                    "Complete the Cloudflare verification in the browser, then press Enter.",
                    logger,
                )
            # ──────────────────────────────────────────────────────────────

            await take_screenshot(page, "05_after_verification", screenshots_dir, logger)
            logger.success("Human verification complete — application done!")
            break
            # ── Wait for "Verify you are human" checkbox ──────────────
            # Some ATS show a Cloudflare/human-verification step after
            # submission. Wait 60 seconds for it to appear, then click it.
            """ logger.info("Waiting 60s for human verification checkbox...")
            await page.wait_for_timeout(60000)
            captcha_selectors = [
                "input[type='checkbox'][id*='cf-']",        # Cloudflare checkbox input
                ".cf-turnstile input",                       # Cloudflare Turnstile
                "iframe[src*='challenges.cloudflare']",      # Cloudflare iframe
                "[aria-label*='verify' i]",                  # Generic verify label
                "[aria-label*='human' i]",                   # Generic human label
                "input[type='checkbox']:visible",            # Any visible checkbox
            ]

            verified = False
            for cap_sel in captcha_selectors:
                try:
                    cap = page.locator(cap_sel).first
                    if await cap.count() > 0 and await cap.is_visible():
                        # For iframes (Cloudflare Turnstile), click inside the frame
                        tag = await cap.evaluate("el => el.tagName.toLowerCase()")
                        if tag == "iframe":
                            frame = await cap.content_frame()
                            if frame:
                                cb = frame.locator("input[type='checkbox']").first
                                if await cb.count() > 0:
                                    await cb.click()
                                    verified = True
                        else:
                            await cap.click()
                            verified = True
                        if verified:
                            logger.success("Human verification checkbox clicked!")
                            await page.wait_for_timeout(3000)
                            await take_screenshot(page, "04_verified", screenshots_dir, logger)
                            break
                except Exception:
                    continue

            if not verified:
                logger.warning("No human verification checkbox found — may not have appeared")
            # ─────────────────────────────────────────────────────────────
            # Scroll to bottom — Cloudflare verification appears there after submission
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")  # scroll again in case page grew
            #await take_screenshot(page, "03_submitted", screenshots_dir, logger)
            logger.info("Browser held open — press Ctrl+C when done reviewing.")
            await page.wait_for_timeout(86400000)  # hold browser open for up to 24h (Ctrl+C to exit)
            break """
            
        except Exception as e:
            logger.info(f"Submit selector {sel} failed: {e}")
            continue

    if not submitted:
        logger.warning("Could not find submit button — manual submission required")
        return "NEEDS_MANUAL_SUBMIT"

    return "SUBMITTED"
