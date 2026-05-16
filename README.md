# Job Application Agent 🤖

An AI-powered agent that reads any job posting, tailors your resume and cover letter using Claude, fills the application form automatically via Playwright, and submits — all with a full audit log of every decision made.

Built as a submission for Opendoor's AI Ops Engineer role challenge:
> *"If you apply to this role ONLY using AI (including filling out the forms + creating the documents) & tell us how you did it, we'll move you straight to interview."*

---

## How It Works

```
Job URL
  └── Claude Haiku    → Parse job description & extract requirements
  └── Claude Sonnet   → Skill gap analysis (HAVE / PARTIAL / MISSING per skill)
  └── Claude Opus     → Tailor resume to the JD
  └── Claude Sonnet   → Write cover letter
  └── Playwright      → Upload resume → fill form → handle dropdowns → submit
  └── Human-in-loop   → Cloudflare / captcha checkpoint before confirming
```

Every run produces a full audit log capturing every AI response, every field filled, and every document generated.

---

## Project Structure

```
job-apply-agent/
├── run.py                        # Entry point & orchestrator
├── profile_v3.json               # Your candidate profile (edit this)
├── requirements.txt
├── .env                          # API key + run config (never commit)
├── .env.example                  # Template for .env
├── .gitignore
│
├── agent/
│   ├── logger.py                 # Rich console + markdown audit log
│   ├── profile_loader.py         # Load & index profile_v3.json
│   ├── jd_parser.py              # Fetch + parse job description (Haiku)
│   ├── doc_generator.py          # Resume (Opus) + cover letter (Sonnet) → PDF
│   ├── form_detector.py          # Detect ATS fields + map answers (Haiku)
│   ├── skill_gap.py              # Skill gap analysis + project suggestions (Sonnet)
│   └── applicator.py             # Playwright form automation
│
├── templates/
│   ├── resume.html               # Jinja2 resume template
│   └── cover_letter.html         # Jinja2 cover letter template
│
└── output/
    └── run_YYYYMMDD_HHMMSS/      # Created per run
        ├── agent_log.md          # ★ Full audit log
        ├── Muthukannan_Resume_<Role>.pdf
        ├── cover_letter.pdf
        ├── resume.html
        ├── cover_letter.html
        ├── jd.json               # Parsed job description
        ├── skill_gap.json        # Skill gap analysis
        ├── form_fields.json      # Detected fields + mapped answers
        └── screenshots/
            ├── 01_blank_form.png
            ├── 02_filled_form.png
            ├── 03_submitted.png
            └── 04_after_verification.png
```

---

## Architecture

### Two-Stage Field Mapping

Form fields are mapped to answers in two stages:

**Stage 1 — Rule-based (deterministic, zero API cost)**
34 hardcoded rules handle known fields reliably:
- Personal info (name, email, phone, LinkedIn, GitHub)
- Demographic/EEO fields (pronouns, gender, race, veteran status, disability)
- Location fields (city, country, province)
- Work authorization, salary, source

**Stage 2 — Claude Haiku (unknown/custom fields only)**
Only fields the rule mapper couldn't handle are sent to Claude. Haiku is fast and cheap for this lookup-style task.

### Smart Combobox Handling

For `role="combobox"` fields, the agent:
1. Reads `aria-controls` to find the linked listbox element
2. Clicks to open the dropdown
3. Reads all real option texts (up to 50)
4. Matches against the profile answer using exact → prefix → partial matching
5. Clicks the best match

### Generic Dropdown Detection

When a field's label is a placeholder (`"Select"`, `"Search"`, `"--"`), the agent opens the dropdown with `ArrowDown`, reads all options, and scores them against a prioritised list of demographic target values.

### Radio Button Detection

Rippling-style radio groups use `div[role="radiogroup"]` with `div[role="radio"]` children. The agent reads `<p>` text inside each child and clicks the one containing consent keywords (`"i consent"`, `"i agree"`, `"yes"`).

### Model Cost Per Run

| Task | Model | Approx. cost |
|---|---|---|
| JD parsing | Claude Haiku | ~$0.001 |
| Form field mapping | Claude Haiku | ~$0.002 |
| Skill gap analysis | Claude Sonnet | ~$0.02 |
| Cover letter | Claude Sonnet | ~$0.02 |
| Resume | Claude Opus | ~$0.15 |
| **Total** | | **~$0.20–0.25** |

---

## Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.11+ |
| Google Chrome | Latest |
| Anthropic API key | [console.anthropic.com](https://console.anthropic.com/keys) |

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/your-username/job-apply-agent.git
cd job-apply-agent
```

### 2. Create a virtual environment

```bash
python -m venv venv

# Mac/Linux
source venv/bin/activate

# Windows
venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 4. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
ANTHROPIC_API_KEY=sk-ant-your-key-here
JOB_URL=https://ats.rippling.com/en-CA/opendoor/jobs/...
RUN_MODE=dry
PROFILE=profile_v3.json

# Optional: connect to existing Chrome instead of launching a new one
USE_EXISTING_BROWSER=false
CHROME_DEBUG_PORT=9222
```

### 5. Edit your profile

Open `profile_v3.json` and update with your details. Key sections:

- `personal` — name, contact, pronouns, EEO fields
- `evidence_stories` — your strongest achievements with metrics
- `work_history` — roles with `evidence_links` to stories
- `skills_master` — categorised skill list
- `certifications` / `education`

---

## Running

### Dry run (recommended first — fills form but does not submit)

```bash
python run.py --job-url "https://..." --mode dry
```

The browser stays open after filling. Review every field, then press **Ctrl+C** to exit.

### Live run (submits the application)

```bash
python run.py --job-url "https://..." --mode live
```

### Using .env (no CLI args needed)

Set `JOB_URL` and `RUN_MODE` in `.env`, then:

```bash
python run.py
```

### VS Code debugger

Create `.vscode/launch.json`:

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Debug Agent",
      "type": "debugpy",
      "request": "launch",
      "program": "${workspaceFolder}/run.py",
      "console": "integratedTerminal",
      "cwd": "${workspaceFolder}",
      "justMyCode": false,
      "envFile": "${workspaceFolder}/.env"
    }
  ]
}
```

Press **F5** to start. Set breakpoints anywhere in the agent files.

---

## Using an Existing Chrome Session

To keep Chrome open after the agent exits (useful for Cloudflare verification):

**Step 1 — Launch Chrome with remote debugging:**

```bash
# Windows
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\chrome-debug-profile"
```

**Step 2 — Set in `.env`:**

```env
USE_EXISTING_BROWSER=true
CHROME_DEBUG_PORT=9222
```

The agent connects to this Chrome session. When Python exits, Chrome stays open exactly where it is.

---

## Human-in-the-Loop Checkpoint

In live mode, after form submission the agent:
1. Scrolls slowly to the bottom of the page
2. Takes a screenshot
3. **Pauses and waits for you** to complete any Cloudflare/captcha verification
4. Press **Enter** in the terminal to confirm and let the agent take a final screenshot

```
⏸  HUMAN INPUT NEEDED: Complete the Cloudflare verification in the browser, then press Enter.
    → Press Enter to continue: _
```

---

## Reading the Audit Log

Every run writes `output/run_YYYYMMDD_HHMMSS/agent_log.md`. Open it in VS Code or any markdown viewer.

| Section | Contents |
|---|---|
| 📋 JD Analysis | Claude's structured extraction of the job posting |
| 🧩 Skill Gap | HAVE / PARTIAL / MISSING per JD requirement + project suggestions |
| 📄 Resume | Tailoring decisions + full generated resume text |
| ✉️ Cover Letter | Full generated cover letter |
| 🔍 Form Detection | Every field found, its type, role, and available options |
| ✏️ Field Mapping | Table of field → answer → source (rule / claude) |
| 📎 Document Upload | Which files were uploaded and how |
| 🚀 Submission | Status, screenshots, verification checkpoint |

---

## Supported ATS Systems

| ATS | Status |
|---|---|
| Rippling | ✅ Tested |
| Greenhouse | ✅ Supported |
| Lever | ✅ Supported |
| Workday | ✅ Supported |
| SmartRecruiters | ✅ Supported |
| Ashby | ✅ Supported |
| LinkedIn Easy Apply | ✅ Supported |
| Generic HTML forms | ✅ Supported |

---

## Extending the Agent

| Want to... | Where |
|---|---|
| Add a new field mapping rule | `RULE_MAP` in `agent/form_detector.py` |
| Change resume layout | `templates/resume.html` |
| Change cover letter layout | `templates/cover_letter.html` |
| Add profile fields | `profile_v3.json` + `agent/profile_loader.py` |
| Change AI models | `CLAUDE_MODEL` / `RESUME_MODEL` / `COVER_MODEL` constants |
| Add batch mode | Wrap `main()` in a loop in `run.py` |
| Add a new ATS | `ATS_PATTERNS` dict in `agent/form_detector.py` |

---

## Debugging

**Check the log first** — `agent_log.md` shows every AI response and field fill. 90% of issues are visible there without touching the code.

**Playwright inspector** — step through each browser action:

```bash
# Windows
set PWDEBUG=1 && python run.py --mode dry

# Mac/Linux
PWDEBUG=1 python run.py --mode dry
```

**Test field mapping in isolation** — without launching a browser:

```python
# test_field_map.py
import asyncio, json
from dotenv import load_dotenv
from agent.profile_loader import load_profile
from agent.logger import AgentLogger
from agent.form_detector import map_fields_to_answers
from pathlib import Path

load_dotenv()

test_fields = [
    {"field_id": "pronouns", "label": "Select", "type": "listbox",
     "selector": "#pronouns", "options": [], "role": "combobox"}
]

async def main():
    logger = AgentLogger(Path("output/test"))
    profile = load_profile("profile_v3.json")
    jd = {"company": "Opendoor", "role": "AI Ops Engineer"}
    result = await map_fields_to_answers(test_fields, profile, jd, logger)
    print(json.dumps(result, indent=2))

asyncio.run(main())
```

---

## Common Errors

| Error | Fix |
|---|---|
| `JSONDecodeError: Extra data` | Claude appended text after JSON — already handled by `rfind` extraction |
| `Locator.click: Timeout` | Element is hidden — agent uses `set_input_files` bypass for file inputs |
| `SystemExit: 2` | `--job-url` not set — add `JOB_URL` to `.env` |
| `UnicodeEncodeError` | Windows `cp1252` issue — all file writes use `encoding="utf-8"` |
| Field filled with wrong value | Check `agent_log.md` for `[rule]` vs `[claude]` source — add a rule to `RULE_MAP` |
| Resume not uploading | ATS uses hidden input — agent tries 3 methods including label/button trigger |

---

## Tech Stack

| Layer | Technology |
|---|---|
| AI | Anthropic Claude (Haiku / Sonnet / Opus) |
| Browser automation | Microsoft Playwright |
| Document generation | Jinja2 + Playwright PDF |
| Console output | Rich |
| Runtime | Python 3.11+ asyncio |

---

## License

MIT
