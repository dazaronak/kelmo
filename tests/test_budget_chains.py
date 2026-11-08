"""The optional [budget] table: which instruction files the budget check measures, and what it says when none are."""
import json

import pytest

from kelmo.budget import load as load_budget
from kelmo.budget import validate
from kelmo.config import Config
from kelmo.doctor import diagnose
from kelmo.errors import KelmoError
from kelmo.service import Service
from kelmo.wiki import policy
from test_fact_checks import content

pytest_plugins = ['test_fact_checks']

MARKER = '\n# budget under test\n'


def vault(tmp_path):
    root = tmp_path / 'vault'
    Service.initialize(root, 'synthetic')
    return root


def configure(root, table):
    path = root / 'kelmo.toml'
    base = path.read_text().split(MARKER)[0].rstrip('\n')
    table = table.lstrip()
    if not table.startswith('[') and not table.startswith('budget.'):
        table = '[budget]\n' + table
    path.write_text(base + '\n' + MARKER + table + '\n')
    return root


def instructions(base):
    (base / 'projects/alpha').mkdir(parents=True, exist_ok=True)
    (base / 'projects/beta').mkdir(parents=True, exist_ok=True)
    (base / 'instructions.md').write_text('Global instruction.\n')
    (base / 'projects/alpha/CLAUDE.md').write_text('Alpha project instruction.\n')
    (base / 'projects/beta/CLAUDE.md').write_text('Beta project instruction.\n')
    return base


def chain_table(base, **overrides):
    values = {'name': 'desk', 'root': str(base), 'global': ['instructions.md'],
              'projects': ['projects/*/CLAUDE.md']}
    values.update(overrides)
    lines = ['[[budget.chain]]', 'name = "%s"' % values['name'], 'root = "%s"' % values['root']]
    for key in ('global', 'projects'):
        if values[key]:
            lines.append('%s = [%s]' % (key, ', '.join('"%s"' % item for item in values[key])))
    return '\n'.join(lines)


def test_a_vault_that_configures_no_budget_reports_that_nothing_was_measured(tmp_path):
    scope = policy.check(vault(tmp_path))['scope']
    assert scope == {'hub_limits_enforced': True, 'hot_chains_checked': 0,
                     'hot_chains_configured': 0, 'hot_chains_uncovered': [], 'hot_imports_unresolved': []}


def test_a_configured_chain_is_measured_once_per_instruction_file_it_loads(tmp_path):
    root = vault(tmp_path)
    configure(root, chain_table(instructions(tmp_path / 'instructions')))
    result = policy.check(root)
    assert [chain['profile'] for chain in result['chains']] == ['desk', 'desk/alpha', 'desk/beta']
    assert result['scope'] == {'hub_limits_enforced': True, 'hot_chains_checked': 3,
                               'hot_chains_configured': 1, 'hot_chains_uncovered': [],
                               'hot_imports_unresolved': []}
    assert result['ok'] and result['violations'] == []
    assert all(chain['estimated_tokens'] > 0 for chain in result['chains'])


def test_a_project_file_is_counted_beside_the_shared_instruction_it_is_loaded_with(tmp_path):
    root = vault(tmp_path)
    base = instructions(tmp_path / 'instructions')
    configure(root, chain_table(base, projects=[]))
    shared_only = policy.check(root)['chains'][0]['estimated_tokens']
    configure(root, chain_table(base))
    with_project = {chain['profile']: chain['estimated_tokens'] for chain in policy.check(root)['chains']}
    assert with_project['desk/alpha'] > shared_only


def test_the_token_limit_comes_from_configuration_and_a_chain_over_it_is_named(tmp_path):
    root = vault(tmp_path)
    base = instructions(tmp_path / 'instructions')
    (base / 'instructions.md').write_text('Global instruction. ' * 60 + '\n')
    configure(root, 'budget.token_limit = 20\n' + chain_table(base))
    result = policy.check(root)
    assert result['token_limit'] == 20
    assert result['ok'] is False
    assert any(violation.startswith('HOT budget exceeded: desk') for violation in result['violations'])
    configure(root, 'budget.token_limit = 100000\n' + chain_table(base))
    assert policy.check(root)['ok'] is True


def test_an_anchor_prefix_is_configuration_and_not_a_reserved_name(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    for role in ('AUTHOR', 'COMMITTER'):
        monkeypatch.setenv(f'GIT_{role}_NAME', 'Synthetic Budget Tests')
        monkeypatch.setenv(f'GIT_{role}_EMAIL', 'budget@example.invalid')
    root = vault(tmp_path)
    base = instructions(tmp_path / 'instructions')
    (base / 'shared.md').write_text('Shared instruction. ' * 40 + '\n')
    (base / 'instructions.md').write_text('Global instruction.\n@~/.agent-imports/shared.md\n')
    configure(root, chain_table(base))
    unresolved = policy.check(root)
    assert unresolved['ok'] is True and unresolved['violations'] == []
    assert unresolved['scope']['hot_imports_unresolved'] == ['%s -> ~/.agent-imports/shared.md' % profile
                                                             for profile in ('desk', 'desk/alpha', 'desk/beta')]
    assert unresolved['chains'][0]['unresolved_imports'] == ['~/.agent-imports/shared.md']
    configure(root, chain_table(base) + '\n[budget.anchors]\n"~/.agent-imports/" = "%s"\n' % base)
    resolved = policy.check(root)
    assert resolved['violations'] == [] and resolved['ok'] is True
    (base / 'instructions.md').write_text('Global instruction.\n')
    assert resolved['chains'][0]['estimated_tokens'] > policy.check(root)['chains'][0]['estimated_tokens']


def test_a_home_prefix_resolves_against_the_vault_only_when_the_vault_configures_it(tmp_path):
    root = vault(tmp_path)
    (root / 'knowledge/wiki/facts.md').write_text('Vault-side instruction.\n' * 40)
    base = instructions(tmp_path / 'instructions')
    (base / 'instructions.md').write_text('Global instruction.\n@~/.kelmo-test-anchor/knowledge/wiki/facts.md\n')
    configure(root, chain_table(base))
    bare = policy.check(root)
    assert bare['violations'] == []
    assert bare['chains'][0]['unresolved_imports'] == ['~/.kelmo-test-anchor/knowledge/wiki/facts.md']
    configure(root, chain_table(base) + '\n[budget.anchors]\n"~/.kelmo-test-anchor/" = "%s"\n' % root)
    anchored = policy.check(root)
    assert anchored['violations'] == [] and 'unresolved_imports' not in anchored['chains'][0]
    assert anchored['chains'][0]['estimated_tokens'] > bare['chains'][0]['estimated_tokens']


def test_a_chain_that_names_no_instruction_file_is_reported_uncovered(tmp_path):
    root = vault(tmp_path)
    (tmp_path / 'empty').mkdir()
    configure(root, chain_table(tmp_path / 'empty', projects=[]))
    result = policy.check(root)
    assert result['ok'] is True
    assert result['scope'] == {'hub_limits_enforced': True, 'hot_chains_checked': 0,
                               'hot_chains_configured': 1, 'hot_chains_uncovered': ['desk'],
                               'hot_imports_unresolved': []}


def test_doctor_names_the_configured_chain_it_could_not_measure(service):
    configure(service.root, chain_table(service.root / 'no-such-instructions', projects=[]))
    result = diagnose(service.root)
    assert result['budget_policy']['hot_chains_uncovered'] == ['desk']
    assert any('desk' in warning for warning in result['warnings'])


def test_doctor_names_an_import_the_vault_does_not_hold_without_calling_the_chain_unhealthy(service):
    base = instructions(service.root / 'instructions')
    (base / 'instructions.md').write_text('Global instruction.\n@~/.kelmo-test-anchor/knowledge/wiki/absent.md\n')
    configure(service.root, chain_table(base))
    result = diagnose(service.root)
    assert result['checks']['budget'] is True and result['issues']['budget'] == []
    assert result['budget_policy']['hot_imports_unresolved'] == [
        '%s -> ~/.kelmo-test-anchor/knowledge/wiki/absent.md' % profile for profile in ('desk', 'desk/alpha', 'desk/beta')]
    assert any('absent.md' in warning for warning in result['warnings'])


def test_doctor_reports_what_each_configured_chain_measured(service):
    base = instructions(service.root / 'instructions')
    (base / 'instructions.md').write_text('Global instruction.\n@~/.kelmo-test-anchor/knowledge/wiki/absent.md\n')
    configure(service.root, chain_table(base))
    chains = diagnose(service.root)['budget_chains']
    assert [chain['profile'] for chain in chains] == ['desk', 'desk/alpha', 'desk/beta']
    assert all(chain['estimated_tokens'] > 0 for chain in chains)
    assert chains[0]['unresolved_imports'] == ['~/.kelmo-test-anchor/knowledge/wiki/absent.md']


def test_an_import_cycle_stays_a_violation_although_a_missing_page_is_only_named(tmp_path):
    root = vault(tmp_path)
    base = instructions(tmp_path / 'instructions')
    (base / 'instructions.md').write_text('Global instruction.\n@loop.md\n')
    (base / 'loop.md').write_text('@instructions.md\n')
    configure(root, chain_table(base, projects=[]))
    result = policy.check(root)
    assert result['violations'] == ['anchor resolution failed: desk (ValueError)']
    assert result['scope']['hot_imports_unresolved'] == []


def test_a_configured_chain_limit_blocks_the_wiki_commit(service):
    base = instructions(service.root / 'instructions')
    (base / 'instructions.md').write_text('Global instruction. ' * 200 + '\n')
    configure(service.root, 'budget.token_limit = 20\n' + chain_table(base))
    receipt = service.reconcile('A durable fact.', 'projects/fixture/fixture.md')
    operation = service.capture(content({'result': 'not_found', 'search_id': receipt['search_id']}),
                                'note.md', 'lead', 'promote')
    result = service.run(operation['id'])
    assert result['state'] == 'failed'
    assert result['error_code'] == 'E_BUDGET'
    assert service.status()['facts'] == 0


@pytest.mark.parametrize('table', [
    'budget.unknown = 1',
    '[[budget.chain]]\nname = "desk"',
    '[[budget.chain]]\nname = "two words"\nroot = "/tmp"',
    '[[budget.chain]]\nname = "desk"\nroot = "/tmp"\nprojects = ["**/CLAUDE.md"]',
    '[[budget.chain]]\nname = "desk"\nroot = "/tmp"\nglobal = ["../outside.md"]',
    '[[budget.chain]]\nname = "desk"\nroot = "~/x"\n[[budget.chain]]\nname = "desk"\nroot = "~/y"',
    'budget.token_limit = 0',
    '[budget.anchors]\n"~agent" = "/tmp"',
])
def test_an_invalid_budget_table_stops_the_vault_from_loading(tmp_path, table):
    root = configure(vault(tmp_path), table)
    with pytest.raises(KelmoError) as refused:
        Config.load(root)
    assert refused.value.code == 'E_CONFIG'
    assert str(refused.value)
    with pytest.raises(KelmoError):
        load_budget(root)


def test_a_vault_initialized_before_the_table_exists_still_loads_and_then_measures(tmp_path):
    root = vault(tmp_path)
    assert Config.load(root).budget is None
    configure(root, chain_table(instructions(tmp_path / 'instructions')))
    with Service(root) as reopened:
        assert [chain['name'] for chain in reopened.config.budget['chains']] == ['desk']
        assert policy.check(root, budget=reopened.config.budget)['scope']['hot_chains_checked'] == 3


def test_the_config_read_reports_the_budget_table_the_vault_validated(tmp_path):
    base = instructions(tmp_path / 'instructions')
    root = configure(vault(tmp_path), 'budget.token_limit = 12345\n' + chain_table(base)
                                      + '\n[budget.anchors]\n"~/.notes/" = "%s"\n' % base)
    budget = json.loads(json.dumps(Config.load(root).as_dict()))['budget']
    assert budget['token_limit'] == 12345
    assert [chain['name'] for chain in budget['chains']] == ['desk']
    assert budget['chains'][0]['root'] == str(base)
    assert budget['chains'][0]['global'] == ['instructions.md']
    assert budget['chains'][0]['projects'] == ['projects/*/CLAUDE.md']
    assert budget['anchors'] == [['~/.notes/', str(base)]]


def test_a_vault_that_configures_no_budget_reports_none_on_the_config_read(tmp_path):
    assert Config.load(vault(tmp_path)).as_dict()['budget'] is None


def test_the_chain_root_is_the_user_home_or_a_vault_relative_directory(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    result = validate({'chain': [{'name': 'desk', 'root': '~/instructions'}]}, tmp_path / 'vault')
    assert result['chains'][0]['root'] == tmp_path / 'instructions'
    relative = validate({'chain': [{'name': 'desk', 'root': 'instructions'}]}, tmp_path / 'vault')
    assert relative['chains'][0]['root'] == tmp_path / 'vault' / 'instructions'
    assert validate(None, tmp_path / 'vault') is None
