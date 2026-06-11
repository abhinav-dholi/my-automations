"""Generate Claude Code skills from manifests that declare the 'skill' trigger.

Writes .claude/skills/<id>/SKILL.md (directory name = the /command). Claude
Code discovers these even though .claude/ is gitignored, and live-reloads them.

- runtime: claude  -> skill body IS the orchestration prompt, so invoking
  /<id> orchestrates in the CURRENT session (no nested headless claude). The
  manifest's allowed_tools become the skill's allowed-tools.
- runtime: code    -> skill instructs Claude to run `auto run <id>` and report,
  with allowed-tools scoped to exactly that command.
"""
from __future__ import annotations

from pathlib import Path

import config

SKILLS_DIR = config.REPO_ROOT / ".claude" / "skills"


def _frontmatter(description: str, allowed_tools: list[str]) -> str:
    lines = ["---", f"description: {description}"]
    if allowed_tools:
        # Claude Code frontmatter expects a space-separated tool list.
        lines.append(f"allowed-tools: {' '.join(allowed_tools)}")
    lines.append("---")
    return "\n".join(lines)


def _body_for(m) -> tuple[str, list[str]]:
    if m.runtime == "claude":
        prompt = m.entry_path.read_text().rstrip()
        return prompt, m.allowed_tools
    # code automation: run it through the deterministic runner.
    cmd = f"python3 cli/auto run {m.id}"
    body = (
        f"Run the **{m.id}** automation, then report the result.\n\n"
        f"Execute from the repo root:\n\n"
        f"```bash\n{cmd}\n```\n\n"
        f"Summarize what it did and surface any error output."
    )
    return body, [f"Bash({cmd})"]


def generate_one(m) -> Path:
    body, allowed = _body_for(m)
    desc = m.description or f"Run the {m.id} automation"
    content = _frontmatter(desc, allowed) + "\n\n" + body + "\n"
    skill_dir = SKILLS_DIR / m.id
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(content)
    return path


def generate_all(manifests) -> list[Path]:
    return [generate_one(m) for m in manifests if "skill" in m.triggers]
