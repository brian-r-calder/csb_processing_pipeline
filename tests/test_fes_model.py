from pathlib import Path

from ocscsb.library.fes_model import get_fes_config_path


def test_get_fes_config_path():
    fes_config: Path = get_fes_config_path()
    assert fes_config.name == 'fes2022_config.yml'
    assert fes_config.exists()
    assert fes_config.is_file()
