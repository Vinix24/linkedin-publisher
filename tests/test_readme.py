"""Every publisher.toml example in the README must be a config the tool accepts."""
import re
from pathlib import Path

import pytest

from linkedin_publisher.config import load_config

README = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
EXAMPLES = re.findall(r"```toml\n(.*?)```", README, flags=re.S)


def test_the_readme_has_toml_examples():
    assert len(EXAMPLES) >= 4


@pytest.mark.parametrize("example", EXAMPLES, ids=[f"example-{i}" for i in range(len(EXAMPLES))])
def test_each_readme_example_loads(tmp_path, example):
    (tmp_path / "publisher.toml").write_text(example, encoding="utf-8")
    load_config(str(tmp_path / "publisher.toml"))


def test_the_require_approval_flag_is_documented_where_a_person_looks(capsys):
    from linkedin_publisher import cli, templates
    assert "--require-approval" in README and "--require-approval" in templates.CONFIG
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["publish", "--help"])
    assert exit_info.value.code == 0 and "--require-approval" in capsys.readouterr().out
