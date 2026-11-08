import json
import os
import pty
import subprocess
import sys


def run(*args, content=None):
    return subprocess.run([sys.executable, '-m', 'kelmo.cli', *map(str, args)],
                          input=content, capture_output=True, text=True, timeout=20, check=False)


def run_at_a_terminal(*args, env, answer):
    """Run the CLI with a real tty on stdin, so the interactive path is the one taken."""
    master, slave = pty.openpty()
    child = subprocess.Popen([sys.executable, '-m', 'kelmo.cli', *map(str, args)],
                             stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=env, text=True)
    os.close(slave)
    os.write(master, answer.encode())
    out, err = child.communicate(timeout=30)
    os.close(master)
    return child.returncode, out, err


def test_help_and_schema_work_without_a_vault(tmp_path):
    result = run('--vault', tmp_path / 'absent', '--help')
    assert result.returncode == 0
    assert 'promote' in result.stdout and 'trace' in result.stdout
    assert not (tmp_path / 'absent').exists()
    assert json.loads(run('help', '--json').stdout)['ok'] is True


def test_init_and_capture_through_the_installed_interface(tmp_path):
    root = tmp_path / 'vault'
    created = run('init', '--vault', root, '--label', 'test-node', '--json', '--non-interactive')
    assert created.returncode == 0, created.stderr
    assert json.loads(created.stdout)['result']['initialized'] is True
    captured = run('add', '--vault', root, '--file', '-', '--role', 'collector', '--json', content='Local evidence.')
    envelope = json.loads(captured.stdout)
    assert captured.returncode == 0
    assert envelope['operation']['state'] == 'captured'
    trace = run('trace', envelope['operation']['id'], '--vault', root, '--json')
    assert json.loads(trace.stdout)['result']['events'][0]['stage'] == 'captured'


def test_json_errors_do_not_leak_a_traceback_or_create_storage(tmp_path):
    missing = tmp_path / 'missing'
    result = run('status', '--vault', missing, '--json', '--non-interactive')
    assert result.returncode != 0
    assert json.loads(result.stdout)['error']['code'] == 'E_NOT_INITIALIZED'
    assert 'Traceback' not in result.stderr
    assert not missing.exists()


def test_init_never_adopts_or_overwrites_an_unrelated_directory(tmp_path):
    (tmp_path / 'keep.txt').write_text('untouched')
    result = run('init', '--vault', tmp_path, '--label', 'test-node', '--json', '--non-interactive')
    assert result.returncode != 0
    assert (tmp_path / 'keep.txt').read_text() == 'untouched'
    assert not (tmp_path / 'kelmo.toml').exists()


def test_admin_verbs_are_separate_operator_commands(tmp_path):
    root = tmp_path / 'vault'
    assert run('init', '--vault', root, '--label', 'test-node', '--json', '--non-interactive').returncode == 0
    commands = json.loads(run('help', '--json').stdout)['result']['commands']
    assert 'admin' in commands
    counted = run('admin', 'count', '--vault', root, '--json')
    assert json.loads(counted.stdout)['result'] == {'count': 0}
    stats = json.loads(run('admin', 'stats', '--vault', root, '--json').stdout)['result']
    assert stats['total'] == 0 and stats['search_log_mode'] == 'off'
    refused = run('admin', 'dump', '--vault', root, '--json')
    assert refused.returncode != 0 and json.loads(refused.stdout)['error']['code'] == 'E_INPUT'
    missing = run('admin', 'purge', 'no-such-record', '--vault', root, '--json')
    assert missing.returncode != 0 and json.loads(missing.stdout)['error']['code'] == 'E_USAGE'


def test_init_prompt_offers_the_vault_the_environment_named(tmp_path):
    """A client that named its vault once must not be prompted into building a second one."""
    home, chosen = tmp_path / 'home', tmp_path / 'chosen'
    home.mkdir()
    code, out, err = run_at_a_terminal('init', '--label', 'test-node',
                                       env=dict(os.environ, HOME=str(home), KELMO_VAULT=str(chosen),
                                                GIT_AUTHOR_NAME='Synthetic CLI Tests',
                                                GIT_AUTHOR_EMAIL='cli@example.invalid',
                                                GIT_COMMITTER_NAME='Synthetic CLI Tests',
                                                GIT_COMMITTER_EMAIL='cli@example.invalid'),
                                       answer='\n')
    assert code == 0, out + err
    assert f'Vault directory [{chosen}]:' in err
    assert (chosen / 'kelmo.toml').is_file()
    assert not (home / '.local/share/kelmo/default').exists()


def test_init_prompt_still_takes_the_path_typed_at_it(tmp_path):
    home, typed = tmp_path / 'home', tmp_path / 'typed'
    home.mkdir()
    code, out, err = run_at_a_terminal('init', '--label', 'test-node',
                                       env=dict(os.environ, HOME=str(home),
                                               KELMO_VAULT=str(tmp_path / 'chosen'),
                                               GIT_AUTHOR_NAME='Synthetic CLI Tests',
                                               GIT_AUTHOR_EMAIL='cli@example.invalid',
                                               GIT_COMMITTER_NAME='Synthetic CLI Tests',
                                               GIT_COMMITTER_EMAIL='cli@example.invalid'),
                                       answer=f'{typed}\n')
    assert code == 0, out + err
    assert (typed / 'kelmo.toml').is_file()
    assert not (tmp_path / 'chosen').exists()
