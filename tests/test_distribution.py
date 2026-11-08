"""Opt-in acceptance checks over the artifacts that will be distributed."""
import os
from pathlib import Path
import tarfile
import tomllib
import zipfile

import pytest


def test_distribution_excludes_working_files():
    wheel = os.environ.get('KELMO_TEST_WHEEL')
    source = os.environ.get('KELMO_TEST_SDIST')
    if not wheel or not source:
        pytest.skip('Select freshly built wheel and sdist for artifact acceptance.')
    with zipfile.ZipFile(Path(wheel)) as archive:
        names = archive.namelist()
        assert not any('.git' in Path(name).parts or '/_docs/' in '/' + name for name in names)
    with tarfile.open(Path(source)) as archive:
        names = archive.getnames()
        assert not any('.git' in Path(name).parts or '/_docs/' in '/' + name for name in names)


def test_wheel_and_selected_runtime_match_source_bytes():
    selected = os.environ.get('KELMO_TEST_WHEEL')
    if not selected:
        pytest.skip('Select a freshly built wheel for source parity checks.')
    repo = Path(__file__).resolve().parents[1]
    expected = {}
    for package in ('kelmo', 'kelmo.store'):
        for source in (repo / 'src' / package).rglob('*'):
            if source.suffix not in ('.py', '.sql') or not source.is_file():
                continue
            relative = source.relative_to(repo / 'src').as_posix()
            expected[relative] = source.read_bytes()
    prefix = os.environ.get('KELMO_TEST_PREFIX')
    runtime = Path(prefix) if prefix else None
    if runtime:
        sites = list((runtime / 'lib').glob('python*/site-packages'))
        assert len(sites) == 1, 'Select one installed Python runtime.'
    with zipfile.ZipFile(selected) as wheel:
        packaged = {name: wheel.read(name) for name in wheel.namelist()
                    if name.startswith(('kelmo/', 'kelmo/store/'))
                    and Path(name).suffix in ('.py', '.sql')}
        assert packaged == expected, 'Wheel Python/schema bytes differ from the selected checkout.'
        if runtime:
            assert {name: (sites[0] / name).read_bytes() for name in expected} == expected
        config = tomllib.loads((repo / 'pyproject.toml').read_text())
        mapping = config['tool']['hatch']['build']['targets']['wheel']
        for group, scheme, destination in (('shared-scripts', 'scripts', 'bin'),
                                            ('shared-data', 'data', '')):
            for source, target in mapping[group].items():
                members = [name for name in wheel.namelist()
                           if name.endswith(f'.data/{scheme}/{target}')]
                assert len(members) == 1, target
                content = wheel.read(members[0])
                assert content == (repo / source).read_bytes(), source
                if runtime:
                    assert (runtime / destination / target).read_bytes() == content, target
