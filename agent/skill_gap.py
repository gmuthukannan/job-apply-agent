"""
skill_gap.py
============
Compares the job's required skills against the candidate's profile,
identifies gaps, and suggests a concrete hands-on project for each gap.

Model choice: claude-sonnet — skill gap analysis requires genuine reasoning
(comparing nuanced requirements against nuanced experience), so Haiku is
too weak here. Sonnet hits the right quality/cost balance.

Flow:
  1. Build prompt with full profile + JD requirements
  2. Claude returns structured JSON: skills_analysis + gap_projects + summary
  3. Results are logged to console and markdown file
  4. JSON is saved to skill_gap.json in the run directory
"""

import json
import os
import re

import anthropic

from agent.logger import AgentLogger
from agent.profile_loader import profile_summary_for_llm


# ── Model selection ────────────────────────────────────────────────────────────
# Sonnet is used here because skill gap analysis involves reasoning:
# "does 14 years of ETL + some LangChain count as 'AI Ops experience'?"
# Haiku would make too many shallow judgments. Opus would be overkill.
CLAUDE_MODEL = "claude-sonnet-4-6"


# ── Prompt ─────────────────────────────────────────────────────────────────────
SKILL_GAP_PROMPT = """
You are a senior technical career coach performing a precise skill gap analysis.

CANDIDATE PROFILE:
{profile_text}

JOB DESCRIPTION:
Company: {company}
Role: {role}
Requirements:
{requirements}

Nice to have:
{nice_to_have}

TASK:
1. Read every required and preferred skill/qualification from the JD.
2. Check each one against the candidate's skills, work history, certifications, and evidence stories.
3. Classify each skill as: HAVE, PARTIAL, or MISSING.
   - HAVE: candidate clearly demonstrates this
   - PARTIAL: candidate has adjacent/related experience but not direct evidence
   - MISSING: no evidence in the profile at all
4. For every MISSING or PARTIAL skill, suggest ONE concrete hands-on project (buildable in 1-4 weeks)
   that would both teach the skill AND produce a portfolio artefact.

Return ONLY valid JSON - no markdown, no explanation:
{{
  "skills_analysis": [
    {{
      "skill": "exact skill name from JD",
      "status": "HAVE | PARTIAL | MISSING",
      "evidence": "brief reason why (1 sentence)",
      "is_required": true | false
    }}
  ],
  "gap_projects": [
    {{
      "skill": "the missing/partial skill",
      "gap_severity": "CRITICAL | MODERATE | NICE_TO_HAVE",
      "project_title": "short catchy project name",
      "project_description": "2-3 sentence description of what to build",
      "what_youll_learn": "the specific competency gained",
      "deliverable": "what tangible artefact you produce (e.g. GitHub repo, deployed API)",
      "estimated_weeks": 1,
      "tech_stack": ["tool1", "tool2"],
      "first_step": "the single first action to take today to start this project"
    }}
  ],
  "summary": {{
    "total_skills_assessed": 0,
    "have": 0,
    "partial": 0,
    "missing": 0,
    "critical_gaps": 0,
    "overall_fit_score": "X/10",
    "fit_narrative": "2-sentence plain-English summary of candidacy strength"
  }}
}}
"""


async def analyse_skill_gaps(
    profile: dict,
    jd: dict,
    logger: AgentLogger,
) -> dict:
    """
    Run the full skill gap analysis and log results to console + markdown.

    Args:
        profile: Loaded candidate profile dict
        jd:      Parsed job description dict
        logger:  Agent logger

    Returns:
        Structured gap analysis dict (also saved as skill_gap.json by run.py)
    """
    logger.section("🧩", "Skill Gap Analysis")

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # Format requirements as a bulleted list for the prompt
    requirements_text = "\n".join(f"  - {r}" for r in jd.get("requirements", []))
    nice_text = "\n".join(f"  - {r}" for r in jd.get("nice_to_have", []))

    prompt = SKILL_GAP_PROMPT.format(
        profile_text=profile_summary_for_llm(profile),
        company=jd.get("company", ""),
        role=jd.get("role", ""),
        requirements=requirements_text,
        nice_to_have=nice_text,
    )

    logger.ai_prompt("Skill Gap Analyser", prompt[:500] + "...")
    logger.info(f"Analysing skill coverage via Claude ({CLAUDE_MODEL})...")

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8000,  # Gap analysis JSON can be large if many skills
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

    # Robustly extract just the JSON object
    cleaned = re.sub(r"```json|```", "", raw).strip()
    start = cleaned.find("{")
    end   = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object in skill gap response: {raw[:200]}")
    analysis = json.loads(cleaned[start : end + 1])

    # Log results to console and markdown file
    _log_results(analysis, logger)
    return analysis


def _log_results(analysis: dict, logger: AgentLogger):
    """
    Format and write the gap analysis to both the rich console and agent_log.md.

    Sections written:
      - Fit scoreboard (overall score, counts per status)
      - Skills breakdown (HAVE / PARTIAL / MISSING with evidence)
      - Gap project table (summary table + detail cards)
    """
    summary  = analysis.get("summary", {})
    skills   = analysis.get("skills_analysis", [])
    projects = analysis.get("gap_projects", [])

    # ── Scoreboard ─────────────────────────────────────────────────────────
    logger.subsection("Fit Scoreboard")
    logger.key_value("Overall fit score",  summary.get("overall_fit_score", "?"))
    logger.key_value("Skills assessed",    str(summary.get("total_skills_assessed", len(skills))))
    logger.key_value("Have",               str(summary.get("have", 0)))
    logger.key_value("Partial",            str(summary.get("partial", 0)))
    logger.key_value("Missing",            str(summary.get("missing", 0)))
    logger.key_value("Critical gaps",      str(summary.get("critical_gaps", 0)))
    logger.info(summary.get("fit_narrative", ""))

    # ── Skills breakdown — grouped by status ───────────────────────────────
    logger.subsection("Skills Breakdown")

    have    = [s for s in skills if s["status"] == "HAVE"]
    partial = [s for s in skills if s["status"] == "PARTIAL"]
    missing = [s for s in skills if s["status"] == "MISSING"]

    if have:
        logger.info("** Skills you HAVE:**")
        for s in have:
            req = " *(required)*" if s.get("is_required") else " *(nice-to-have)*"
            logger.info(f"  * {s['skill']}{req} - {s['evidence']}")

    if partial:
        logger.info("")
        logger.info("**Partial skills (related but not direct):**")
        for s in partial:
            req = " *(required)*" if s.get("is_required") else " *(nice-to-have)*"
            logger.info(f"  * {s['skill']}{req} - {s['evidence']}")

    if missing:
        logger.info("")
        logger.info("**Missing skills:**")
        for s in missing:
            req = " *(required)*" if s.get("is_required") else " *(nice-to-have)*"
            logger.info(f"  * {s['skill']}{req} - {s['evidence']}")

    # ── Gap projects ───────────────────────────────────────────────────────
    if not projects:
        logger.success("No skill gaps detected - strong match for this role!")
        return

    logger.subsection("Suggested Projects to Close the Gaps")

    # Write a summary table to the markdown log
    with open(logger.log_path, "a", encoding="utf-8") as f:
        f.write("\n| # | Skill Gap | Severity | Project | Est. Time |\n")
        f.write("|---|---|---|---|---|\n")
        for i, p in enumerate(projects, 1):
            f.write(
                f"| {i} | {p['skill']} | {p['gap_severity']} "
                f"| {p['project_title']} | {p['estimated_weeks']}w |\n"
            )
        f.write("\n")

    # Write detail cards for each project (collapsible in markdown viewers)
    for i, project in enumerate(projects, 1):
        # Severity icon for easy scanning
        severity_icon = {
            "CRITICAL":      "🔴",
            "MODERATE":      "🟡",
            "NICE_TO_HAVE":  "🟢",
        }.get(project.get("gap_severity", "MODERATE"), "🟡")

        header = f"{severity_icon} Project {i}: {project['project_title']}"

        # Console output
        logger.info(f"\n{'─'*60}")
        logger.info(f"  {header}")
        logger.info(f"  Closes gap:        {project['skill']} ({project['gap_severity']})")
        logger.info(f"  What to build:     {project['project_description']}")
        logger.info(f"  What you'll learn: {project['what_youll_learn']}")
        logger.info(f"  Deliverable:       {project['deliverable']}")
        logger.info(f"  Tech stack:        {', '.join(project.get('tech_stack', []))}")
        logger.info(f"  Time estimate:     {project['estimated_weeks']} week(s)")
        logger.info(f"  Start today:       {project['first_step']}")

        # Markdown detail block
        with open(logger.log_path, "a", encoding="utf-8") as f:
            f.write(f"\n<details>\n<summary>{header}</summary>\n\n")
            f.write(f"- **Skill gap:** {project['skill']} ({project['gap_severity']})\n")
            f.write(f"- **What to build:** {project['project_description']}\n")
            f.write(f"- **What you'll learn:** {project['what_youll_learn']}\n")
            f.write(f"- **Deliverable:** {project['deliverable']}\n")
            f.write(f"- **Tech stack:** {', '.join(project.get('tech_stack', []))}\n")
            f.write(f"- **Time estimate:** {project['estimated_weeks']} week(s)\n")
            f.write(f"- **Start today:** {project['first_step']}\n")
            f.write("\n</details>\n")
