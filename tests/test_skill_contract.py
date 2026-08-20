from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).parents[1]


def test_skill_contract_files_and_frontmatter() -> None:
    skill = ROOT / "skills" / "jnby-news-watch"
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: jnby-news-watch\n")
    assert "description:" in text
    assert (skill / "agents" / "openai.yaml").is_file()
    assert (skill / "scripts" / "run.py").is_file()


def test_cli_help_lists_supported_commands() -> None:
    runner = ROOT / "skills" / "jnby-news-watch" / "scripts" / "run.py"
    result = subprocess.run(
        [sys.executable, str(runner), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0
    for command in ("digest", "focus", "deepen", "health"):
        assert command in result.stdout
