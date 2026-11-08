"""`packaging/kelmo-isolated` runs src/kelmo/bootstrap.py, which is the only place the isolated
launcher asks a person where the vault should be. The default launcher hands the prompt to
kelmo.cli instead, and tests/test_cli.py measures that one."""
import os
import pty
import subprocess
import sys
import tempfile
from pathlib import Path

BOOTSTRAP = Path(__file__).parents[1] / 'src' / 'kelmo' / 'bootstrap.py'


def prompt_at_a_terminal(env, answer):
    """Run the bootstrap with a real tty on stdin and answer its first question."""
    master, slave = pty.openpty()
    child = subprocess.Popen([sys.executable, '-I', '-S', str(BOOTSTRAP), 'init', '--label', 'fixture'],
                             stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=env, text=True)
    os.close(slave)
    os.write(master, answer.encode())
    out, err = child.communicate(timeout=60)
    os.close(master)
    return out, err


def test_init_prompt_offers_the_vault_the_environment_named():
    """What the bootstrap offers must be the vault every other verb of the same install would use."""
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary).resolve()
        chosen = base / 'chosen'
        out, err = prompt_at_a_terminal(dict(HOME=str(base), KELMO_VAULT=str(chosen),
                                             PATH='/usr/bin:/bin'), answer='\n')
        assert f'Vault directory [{chosen}]:' in out, out + err
        assert '.local/share/kelmo/default' not in out, out + err


def test_init_prompt_offers_the_default_directory_when_nothing_named_one():
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary).resolve()
        out, err = prompt_at_a_terminal(dict(HOME=str(base), PATH='/usr/bin:/bin'), answer='\n')
        assert f"Vault directory [{base / '.local/share/kelmo/default'}]:" in out, out + err
