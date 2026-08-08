"""Regression coverage for the Termux broad install profile."""

from pathlib import Path
import tomllib


REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def test_pyproject_defines_termux_all_without_known_blockers() -> None:
    project = tomllib.loads(PYPROJECT.read_text())
    project_name = project["project"]["name"]
    termux_all = project["project"]["optional-dependencies"]["termux-all"]

    assert f"{project_name}[termux]" in termux_all
    assert f"{project_name}[matrix]" not in termux_all
    assert f"{project_name}[voice]" not in termux_all


def test_install_script_prefers_termux_all_then_fallbacks() -> None:
    text = INSTALL_SH.read_text()
    assert "pip install -e '.[termux-all]' -c constraints-termux.txt" in text
    assert "Termux broad profile (.[termux-all]) failed, trying baseline Termux profile..." in text
    assert "Termux baseline profile (.[termux]) failed, trying base install..." in text
