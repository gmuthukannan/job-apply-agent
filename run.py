"""
run.py
======
Main entry point and orchestrator for the Job Application Agent.

Usage:
  python run.py --job-url "https://..." --mode dry
  python run.py --job-url "https://..." --mode live
  python run.py --job-url "https://..." --mode dry --profile path/to/profile.json

Pipeline (in order):
  1. Load profile          — profile_loader.py reads profile_v3.json
  2. Parse JD              — jd_parser.py fetches URL, Claude Haiku extracts structure
  3. Fit analysis          — tag overlap + relevant story selection (no Claude call)
  4. Skill gap analysis    — skill_gap.py, Claude Sonnet, HAVE/PARTIAL/MISSING per skill
  5. Generate resume       — doc_generator.py, Claude Opus, tailored PDF
  6. Generate cover letter — doc_generator.py, Claude Sonnet, tailored PDF
  7. Apply                 — applicator.py, Playwright fills + uploads + submits

Output (per run, in output/run_YYYYMMDD_HHMMSS/):
  agent_log.md      Full audit log (all Claude responses, field fills, decisions)
  resume.html/pdf   Generated resume
  cover_letter.html/pdf
  jd.json           Parsed job description
  skill_gap.json    Skill gap analysis
  form_fields.json  Detected fields + mapped answers
  screenshots/      01_blank, 02_filled, 03_submitted (live only)

Model cost summary per run (approximate):
  JD parsing:      Haiku   ~$0.001
  Field mapping:   Haiku   ~$0.002
  Skill gap:       Sonnet  ~$0.02
  Cover letter:    Sonnet  ~$0.02
  Resume:          Opus    ~$0.15
  Vision (if any): Sonnet  ~$0.01 per field
  Total typical:           ~$0.20-0.25 per application run
"""

import argparse
import asyncio
import os
import sys
import json
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from agent.logger import AgentLogger
from agent.profile_loader import load_profile
from agent.jd_parser import parse_jd
from agent.doc_generator import generate_resume, generate_cover_letter
from agent.applicator import apply
from agent.skill_gap import analyse_skill_gaps

load_dotenv()

async def wait_for_human(message: str, logger: AgentLogger):
    """
    Pause the agent and wait for the human to press Enter.
    Works in both terminal and VS Code debug console.
    """
    logger.warning(f"⏸  HUMAN INPUT NEEDED: {message}")
    logger.warning("    Press Enter in the terminal when done...")
    await asyncio.get_event_loop().run_in_executor(None, input, "    → Press Enter to continue: ")
    logger.success("Resuming agent.")


def build_run_dir() -> Path:
    """Create a timestamped output directory for this run."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path("output") / f"run_{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


async def main(job_url: str, mode: str, profile_path: str):
    # ── Setup ──────────────────────────────────────────────────────────────
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set. Copy .env.example → .env and add your key.")
        sys.exit(1)

    run_dir = build_run_dir()
    logger = AgentLogger(run_dir)

    logger.section("🚀", "Job Application Agent — Starting")
    logger.key_value("Job URL", job_url)
    logger.key_value("Mode", mode.upper())
    logger.key_value("Profile", profile_path)
    logger.key_value("Run directory", str(run_dir))
    logger.key_value("Started", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # ── Load profile ───────────────────────────────────────────────────────
    logger.section("👤", "Loading Candidate Profile")
    profile = load_profile(profile_path)
    logger.success(f"Profile loaded: {profile['personal']['full_name']}")
    logger.key_value("Career tags", ", ".join(profile.get("career_tags", [])))
    logger.key_value("Evidence stories", str(len(profile.get("evidence_stories", []))))
    logger.key_value("Work history entries", str(len(profile.get("work_history", []))))

    resume_pdf = None
    cover_letter_pdf = None
    status = "UNKNOWN"

    async with async_playwright() as pw:
        # Launch browser — visible so you can watch it work
        use_existing = os.getenv("USE_EXISTING_BROWSER", "false").lower() == "true"

        if use_existing:
            # Connect to already-running Chrome — stays open when Python exits
            browser = await pw.chromium.connect_over_cdp(
                f"http://localhost:{os.getenv('CHROME_DEBUG_PORT', '9222')}"
            )
            logger.info("Connected to existing Chrome session")
            # Use the existing default context and page
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await context.new_page()
        else:
            browser = await pw.chromium.launch(
                headless=False,
                args=["--start-maximized"],
            )
            context = await browser.new_context(
                viewport={"width": 1440, "height": 900},
            )
            page = await context.new_page()
        """ browser = await pw.chromium.launch(
            headless=False,
            args=["--start-maximized"],
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(run_dir / "video") if mode == "live" else None,
        )
        page = await context.new_page() """

        try:
            # ── Step 1: Parse job description ──────────────────────────────
            jd = await parse_jd(page, job_url, logger)

            # Save JD to run dir
            jd_path = run_dir / "jd.json"
            jd_path.write_text(json.dumps(jd, indent=2), encoding="utf-8")
            logger.info(f"JD saved → {jd_path}")

            # ── Step 2: Fit analysis (logged) ──────────────────────────────
            logger.section("🎯", "Profile Fit Analysis")
            jd_tags = jd.get("key_themes", []) + [jd.get("role", "")]
            profile_tags = profile.get("career_tags", [])
            overlap = set(t.lower() for t in jd_tags) & set(t.lower() for t in profile_tags)
            logger.key_value("JD themes", ", ".join(jd.get("key_themes", [])))
            logger.key_value("Profile tags", ", ".join(profile_tags))
            logger.key_value("Tag overlap", ", ".join(overlap) if overlap else "none direct — Claude will find deeper match")

            relevant_stories = [
                s for s in profile.get("evidence_stories", [])
                if any(tag in " ".join(jd.get("key_themes", [])).lower() for tag in s.get("tags", []))
            ]
            logger.subsection("Most relevant evidence stories for this role")
            for story in relevant_stories:
                logger.info(f"[{story['id']}] {story['headline']}")

            # ── Step 3: Skill gap analysis ──────────────────────────────────
            gap_analysis = await analyse_skill_gaps(profile, jd, logger)
            gap_path = run_dir / "skill_gap.json"
            gap_path.write_text(json.dumps(gap_analysis, indent=2), encoding="utf-8")
            logger.info(f"Skill gap analysis saved → {gap_path}")

            # ── Step 4: Generate resume ─────────────────────────────────────
            resume_data, resume_pdf = await generate_resume(page, profile, jd, run_dir, logger)

            # ── Step 5: Generate cover letter ───────────────────────────────
            cover_letter_text, cover_letter_pdf = await generate_cover_letter(
                page, profile, jd, resume_data, run_dir, logger
            )

            #For Testing
            #resume_pdf = "D:\Muthukannan\Projects\JobApplyAgent\Muthukannan_Resume_Operations_AI_Engineer.pdf"
            #cover_letter_pdf = "D:\Muthukannan\Projects\JobApplyAgent\cover_letter.pdf"
            # ── Step 6: Apply ───────────────────────────────────────────────
            # Use apply_url from JD if available, otherwise use the original URL
            apply_url = jd.get("apply_url") or job_url
            if not apply_url.startswith("http"):
                apply_url = job_url

            status = await apply(
                page=page,
                job_url=apply_url,
                profile=profile,
                jd=jd,
                resume_pdf=resume_pdf,
                cover_letter_pdf=cover_letter_pdf,
                mode=mode,
                run_dir=run_dir,
                logger=logger,
                wait_for_human=wait_for_human,
            )

        except KeyboardInterrupt:
            logger.warning("Dry-run review ended by user (Ctrl+C) — browser closing.")
            status = "DRY_RUN — user closed"
        except Exception as e:
            logger.error(f"Agent encountered an error: {e}")
            import traceback
            logger.error(traceback.format_exc())
            status = f"ERROR: {e}"
            raise

        finally:
            # Always pause in live mode so you can review before browser closes
            if mode == "live":
                logger.warning("Keeping browser open for 10 seconds for final review…")
                await page.wait_for_timeout(10000)

            if not use_existing:
                # Only close if we launched the browser ourselves
                await context.close()
                await browser.close()
            else:
                logger.info("Leaving existing Chrome session open.")

    # ── Final summary ──────────────────────────────────────────────────────
    logger.summary(
        job_url=job_url,
        mode=mode,
        resume_pdf=str(resume_pdf) if resume_pdf else "not generated",
        cover_letter_pdf=str(cover_letter_pdf) if cover_letter_pdf else "not generated",
        status=status,
    )

    print(f"\n{'='*60}")
    print(f"  LOG FILE   → {run_dir}/agent_log.md")
    print(f"  RESUME     → {run_dir}/resume.pdf")
    print(f"  COVER LTR  → {run_dir}/cover_letter.pdf")
    print(f"  SKILL GAPS → {run_dir}/skill_gap.json")
    print(f"  FIELD MAP  → {run_dir}/form_fields.json")
    print(f"  SCREENSHOTS→ {run_dir}/screenshots/")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Job Application Agent")
    parser.add_argument(
        "--job-url",
        default=os.getenv("JOB_URL", ""),
        help="URL of the job posting (or set JOB_URL in .env)",
    )
    parser.add_argument(
        "--mode",
        choices=["dry", "live"],
        default=os.getenv("RUN_MODE", "dry"),
        help="dry = fill but do not submit | live = fill and submit (or set RUN_MODE in .env)",
    )
    parser.add_argument(
        "--profile",
        default=os.getenv("PROFILE", "profile_v3.json"),
        help="Path to profile JSON (or set PROFILE in .env)",
    )

    args = parser.parse_args()

    if not args.job_url:
        parser.print_help()
        print("\n  Set JOB_URL in your .env file  OR  pass --job-url on the command line.")
        sys.exit(1)
    asyncio.run(main(args.job_url, args.mode, args.profile))
