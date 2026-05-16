"""
profile_loader.py
=================
Loads profile_v3.json and builds an agent-ready data structure.

Key responsibilities:
  - Parse the JSON profile file
  - Build a story index (id -> story dict) for fast lookup
  - Resolve evidence_links on each work history entry to full story objects
  - Produce a compact plain-text representation for LLM prompts

No Claude calls are made here — this is pure Python data manipulation.
"""

import json
from pathlib import Path


def load_profile(path: str = "profile_v3.json") -> dict:
    """
    Load the profile JSON and enrich it for agent use.

    Enrichments added:
      - profile["_story_index"]: dict mapping story id -> full story object
      - Each work_history entry gets an "evidence_resolved" key containing
        the full story objects for its evidence_links (not just IDs)

    Args:
        path: Path to the profile JSON file

    Returns:
        Enriched profile dict ready for use by all agent modules
    """
    with open(path, encoding="utf-8") as f:
        profile = json.load(f)

    # Build a lookup table: story_id -> story dict
    # This avoids O(n) searches when resolving evidence links later
    story_index = {s["id"]: s for s in profile.get("evidence_stories", [])}

    # Attach the index to the profile for downstream use
    profile["_story_index"] = story_index

    # For each work history entry, resolve its evidence_links from IDs
    # to full story objects — makes it easier for doc_generator to access
    # story details without needing to look them up separately
    for role in profile.get("work_history", []):
        role["evidence_resolved"] = [
            story_index[link]
            for link in role.get("evidence_links", [])
            if link in story_index  # silently skip any broken links
        ]

    return profile


def get_stories_for_tags(profile: dict, tags: list[str]) -> list[dict]:
    """
    Filter evidence stories by tag overlap.

    Used by the fit analysis section in run.py to find which of the
    candidate's stories are most relevant to the target job's themes.

    Args:
        profile: Loaded profile dict (from load_profile)
        tags: List of tag strings to match against (e.g. JD key_themes)

    Returns:
        List of story dicts whose tags overlap with the given tags
    """
    tag_set = set(t.lower() for t in tags)

    return [
        story
        for story in profile.get("evidence_stories", [])
        if set(t.lower() for t in story.get("tags", [])) & tag_set
    ]


def profile_summary_for_llm(profile: dict) -> str:
    """
    Produce a compact, structured plain-text block of the full profile.

    This text is injected into every Claude prompt that needs candidate
    context (resume generation, cover letter, field mapping, skill gap).
    Keeping it structured but concise controls token usage.

    Format:
        CANDIDATE: ...
        LOCATION: ...
        [personal details]
        SUMMARY: ...
        CORE STRENGTHS: ...
        KEY SKILLS: ...
        EVIDENCE STORIES: ...
        WORK HISTORY: ...
        CERTIFICATIONS: ...
        EDUCATION: ...
        TONE PREFERENCE: ...
        LLM STORIES: ...

    Returns:
        Multi-line string ready to embed in any prompt
    """
    p = profile["personal"]

    # ── Personal info block ────────────────────────────────────────────────
    lines = [
        f"CANDIDATE: {p['full_name']}",
        f"LOCATION: {p['location']}",
        f"EMAIL: {p['email']} | PHONE: {p['phone']}",
        f"LINKEDIN: {p['linkedin']} | GITHUB: {p['github']}",
        # Demographic fields — included so field mapper can use them
        f"PRONOUNS: {p.get('Pronouns', 'He/him/his')}",
        f"GENDER: {p.get('Gender', 'Male')}",
        f"RACE / ETHNICITY: {p.get('Race', 'Asian')}",
        f"VETERAN STATUS: {p.get('Veteran Status', 'Non-Veteran')}",
        f"DISABILITY STATUS: {p.get('Disability Status', 'No Disability')}",
        f"CONSENT TO BACKGROUND CHECK: {p.get('Concent to Background Check', 'Yes')}",
        f"CONSENT TO CONTACT: {p.get('Concent to contact', 'Yes')}",
        "",
        f"SUMMARY:\n{profile.get('summary', '')}",
        "",
        "CORE STRENGTHS:",
        *[f"  - {s}" for s in profile.get("core_strengths", [])],
        "",
        "KEY SKILLS:",
    ]

    # ── Skills — one line per category ────────────────────────────────────
    for category, items in profile.get("skills_master", {}).items():
        lines.append(f"  {category.upper()}: {', '.join(items)}")

    # ── Evidence stories with metrics ─────────────────────────────────────
    # These are the candidate's most impactful achievements with numbers.
    # Claude uses these to write strong resume bullets and cover letter paragraphs.
    lines += ["", "EVIDENCE STORIES (with metrics):"]
    for story in profile.get("evidence_stories", []):
        metrics = story.get("metrics", {})
        # Format metrics as "key: value | key: value" or "no metrics"
        metric_str = (
            " | ".join(f"{k}: {v}" for k, v in metrics.items())
            if metrics else "no metrics"
        )
        lines.append(f"  [{story['id']}] {story['headline']}")
        lines.append(f"    -> {story['summary']}")
        lines.append(f"    -> Metrics: {metric_str}")

    # ── Work history — title, company, bullets ─────────────────────────────
    lines += ["", "WORK HISTORY:"]
    for role in profile.get("work_history", []):
        end = role.get("end", "present")
        lines.append(f"\n  {role['title']} @ {role['company']} ({role['start']} - {end})")
        for r in role.get("responsibilities", []):
            lines.append(f"    * {r}")

    # ── Certifications ─────────────────────────────────────────────────────
    lines += ["", "CERTIFICATIONS:"]
    for c in profile.get("certifications", []):
        lines.append(f"  - {c}")

    # ── Education ──────────────────────────────────────────────────────────
    edu = profile.get("education", {})
    lines.append(f"\nEDUCATION: {edu.get('degree', '')} - {edu.get('university', '')}")

    # ── LLM-specific narrative stories ────────────────────────────────────
    # These are pre-written impact sentences that Claude can use verbatim
    # or as inspiration when writing the resume/cover letter narrative.
    lines += [
        "",
        "TONE PREFERENCE: " + profile.get("preferred_tone", ""),
        "",
        "LLM STORIES (use these for narrative impact):",
        *[f"  - {s}" for s in profile.get("stories_for_llm", [])],
    ]

    return "\n".join(lines)
