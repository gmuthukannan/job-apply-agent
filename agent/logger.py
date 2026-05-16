"""
logger.py — Dual-output logger: rich console + structured markdown file.
Every AI response, decision, field fill, and document generated is captured here.
"""

import os
from datetime import datetime
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text
from rich import box


class AgentLogger:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.log_path = run_dir / "agent_log.md"
        self.console = Console()
        self._init_log()

    def _init_log(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write("# Job Application Agent — Run Log\n\n")
            f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write("---\n\n")

    def _write(self, text: str):
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(text + "\n")

    # ── Section headers ────────────────────────────────────────────────────

    def section(self, emoji: str, title: str):
        self.console.print()
        self.console.rule(f"[bold cyan]{emoji}  {title}[/bold cyan]")
        self._write(f"\n## {emoji} {title}\n")

    def subsection(self, title: str):
        self.console.print(f"\n[bold yellow]▸ {title}[/bold yellow]")
        self._write(f"\n### {title}\n")

    # ── Standard messages ──────────────────────────────────────────────────

    def info(self, msg: str):
        self.console.print(f"  [dim]→[/dim] {msg}")
        self._write(f"- {msg}")

    def success(self, msg: str):
        self.console.print(f"  [bold green]✓[/bold green] {msg}")
        self._write(f"- ✅ {msg}")

    def warning(self, msg: str):
        self.console.print(f"  [bold yellow]⚠[/bold yellow]  {msg}")
        self._write(f"- ⚠️ {msg}")

    def error(self, msg: str):
        self.console.print(f"  [bold red]✗[/bold red] {msg}")
        self._write(f"- ❌ {msg}")

    # ── Structured data ────────────────────────────────────────────────────

    def key_value(self, key: str, value: str):
        self.console.print(f"  [cyan]{key}:[/cyan] {value}")
        self._write(f"- **{key}:** {value}")

    def decision(self, label: str, value: str):
        self.console.print(f"  [bold magenta]Decision[/bold magenta] [{label}]: [white]{value}[/white]")
        self._write(f"- 🧠 **Decision [{label}]:** {value}")

    # ── AI output capture ──────────────────────────────────────────────────

    def ai_prompt(self, label: str, prompt: str):
        """Log the prompt sent to Claude."""
        self.console.print(f"\n  [dim italic]Prompt → {label}[/dim italic]")
        self._write(f"\n<details>\n<summary>📤 Prompt: {label}</summary>\n\n```\n{prompt.strip()}\n```\n\n</details>\n")

    def ai_response(self, label: str, content: str):
        """Log Claude's full response — this is the key audit trail."""
        self.console.print(
            Panel(
                Text(content[:1200] + ("…" if len(content) > 1200 else ""), overflow="fold"),
                title=f"[bold green]Claude → {label}[/bold green]",
                border_style="green",
                padding=(0, 1),
            )
        )
        self._write(f"\n#### 🤖 Claude Response: {label}\n\n```\n{content.strip()}\n```\n")

    def document(self, label: str, content: str):
        """Log the full text of a generated document (resume, cover letter)."""
        self.console.print(
            Panel(
                content,
                title=f"[bold blue]📄 {label}[/bold blue]",
                border_style="blue",
                padding=(0, 1),
            )
        )
        self._write(f"\n#### 📄 {label}\n\n```\n{content.strip()}\n```\n")

    # ── Form filling log ───────────────────────────────────────────────────

    def field_fill(self, field_label: str, field_type: str, value: str):
        display_value = value if len(value) < 80 else value[:77] + "..."
        self.console.print(f"  [cyan]✎[/cyan] [{field_type}] [bold]{field_label}[/bold] → {display_value}")
        self._write(f"| `{field_label}` | {field_type} | {value[:200]} |")

    def field_table_header(self):
        self._write("\n| Field | Type | Value Used |\n|---|---|---|")

    def file_upload(self, field_label: str, file_path: str):
        self.console.print(f"  [magenta]📎[/magenta] Uploading [bold]{Path(file_path).name}[/bold] → {field_label}")
        self._write(f"- 📎 **Upload:** `{field_label}` ← `{file_path}`")

    def screenshot(self, path: str):
        self.console.print(f"  [dim]📸 Screenshot → {path}[/dim]")
        self._write(f"- 📸 Screenshot: `{path}`")

    # ── Run summary ────────────────────────────────────────────────────────

    def summary(self, job_url: str, mode: str, resume_pdf: str, cover_letter_pdf: str, status: str):
        self.console.print()
        self.console.rule("[bold green]Run Complete[/bold green]")
        summary_md = f"""
## 🏁 Run Summary

| Item | Value |
|---|---|
| **Status** | {status} |
| **Mode** | {mode} |
| **Job URL** | {job_url} |
| **Resume PDF** | `{resume_pdf}` |
| **Cover Letter PDF** | `{cover_letter_pdf}` |
| **Log File** | `{self.log_path}` |
| **Completed** | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |
"""
        self._write(summary_md)
        self.console.print(f"  [bold green]Status:[/bold green] {status}")
        self.console.print(f"  [bold]Log:[/bold] {self.log_path}")
        self.console.print(f"  [bold]Resume:[/bold] {resume_pdf}")
        self.console.print(f"  [bold]Cover Letter:[/bold] {cover_letter_pdf}")
        self.console.print()
