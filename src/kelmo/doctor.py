"""Read-only diagnosis that remains available when durable components are degraded."""
import sqlite3
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from .config import Config
from .errors import KelmoError
from .runtime import IsolationError
from .wiki import engine, links, policy


CHECK_NAMES = (
    'archive_integrity', 'journal_integrity', 'registry_integrity',
    'data_repository',
    'wiki_available', 'wiki_owner', 'wiki_transaction_clear',
    'wiki_committed', 'wiki_consistency', 'budget', 'links',
    'embedding_model', 'hot', 'sessions', 'graph', 'git_identity',
)
# A daily backup window plus a missed run; an older verified backup is reported as a warning.
BACKUP_STALE_HOURS = 36


def _database(path, required_tables=()):
    if path is None:
        return False, set()
    if path.is_symlink() or not path.is_file():
        return False, set()
    try:
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            connection.execute('PRAGMA query_only=ON')
            integrity = connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            return integrity and set(required_tables) <= tables, tables
        finally:
            connection.close()
    except (OSError, sqlite3.Error, UnicodeError):
        return False, set()


def _stale_claims(path):
    """Unexpired claims whose owning process provably ended (journal.owner_gone), read without writing."""
    import time
    from .journal import owner_gone
    try:
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            connection.row_factory = sqlite3.Row
            columns = {row[1] for row in connection.execute('PRAGMA table_info(claims)')}
            if 'owner_pid' not in columns:
                return []
            rows = connection.execute('SELECT * FROM claims WHERE local_worker=0 AND expires_at>?',
                                      (time.time(),)).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return []
    return [{'operation_id': row['operation_id'], 'owner_host': row['owner_host'], 'owner_pid': row['owner_pid']}
            for row in rows if owner_gone(row)]


LONG_WAIT_SECONDS = 3600


def _long_waits(path, session_jobs, clock=None):
    """Notes still waiting their turn an hour after capture: an operation failed on a held lock, a create
    note the worker has not archived, or a session job behind a lock or never taken. Each wait spends no
    attempt, so a stuck lock or a stopped worker would otherwise go unnoticed."""
    import time
    from datetime import datetime
    now = (clock or time.time)()

    def old(created_at):
        try:
            return now - datetime.fromisoformat(created_at).timestamp() > LONG_WAIT_SECONDS
        except (TypeError, ValueError):
            return False

    waits = []
    try:
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT id, intent, state, error_code, created_at FROM operations "
                "WHERE (state='failed' AND error_code='E_BUSY') OR (state='queued' AND terminal=0)").fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        rows = []
    waits += [{'operation_id': row['id'], 'state': row['state'], 'error_code': row['error_code']}
              for row in rows if old(row['created_at'])]
    waits += [{'session_job_id': job['id'], 'state': job['state'], 'error_code': job.get('error_code')}
              for job in session_jobs
              if job['state'] == 'pending' and job.get('error_code') in (None, 'E_BUSY', 'E_CLAIM')
              and old(job.get('created_at'))]
    return waits


def _failed_notes(path, clock=None):
    """How many notes stopped failing, and how long the oldest has waited since it was written.

    Only `kelmo queue list` showed them, so a note that failed for days was invisible to the agent and
    the person who wrote it. The count is the whole set, not the 20 `status` names."""
    import time
    from datetime import datetime
    now = (clock or time.time)()
    try:
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("SELECT created_at FROM operations WHERE state='failed'").fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        rows = []
    ages = []
    for row in rows:
        try:
            ages.append(max(0.0, now - datetime.fromisoformat(row['created_at']).timestamp()))
        except (TypeError, ValueError):
            continue
    return {'count': len(rows), 'oldest_seconds': max(ages) if ages else 0.0}


def _age_text(seconds):
    """One waited time a person can read: hours from a day down, minutes below that."""
    hours = seconds / 3600
    return f'{hours:.1f} hours' if hours >= 1 else f'{max(0, int(seconds // 60))} minutes'


def _stale_git_locks(repository, clock=None):
    """Git lock files in the knowledge repository older than STALE_GIT_LOCK_SECONDS: a crashed Git
    process left them, and every publication waits behind them until a person removes them."""
    import time
    from .wiki.engine import STALE_GIT_LOCK_SECONDS
    now = (clock or time.time)()
    git_dir = Path(repository) / '.git'
    found = []
    try:
        candidates = [git_dir / 'index.lock', *git_dir.glob('refs/**/*.lock'), git_dir / 'HEAD.lock']
        for path in candidates:
            try:
                if now - path.stat().st_mtime > STALE_GIT_LOCK_SECONDS:
                    found.append(path.relative_to(git_dir).as_posix())
            except OSError:
                continue
    except OSError:
        return []
    return found


def _managed_targets(registry):
    # The registry projects exactly its registered pages; it creates no implicit project hubs.
    return sorted(registry['pages'])


def _unsafe_targets(wiki, registry):
    unsafe = []
    for target in _managed_targets(registry):
        path = wiki / target
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(wiki):
                unsafe.append(target)
        except (OSError, RuntimeError):
            unsafe.append(target)
    return unsafe


def _internal(config, relative):
    try:
        return config.internal(relative)
    except (KelmoError, OSError):
        return None


def _dirty_pages(config, wiki, pages):
    targets = {path: str(path.relative_to(wiki)) for path in pages}
    registry = config.registry
    targets[registry] = config.registry_relative
    repositories = {}
    data_repository = engine.repository_root(config.knowledge_git_root)
    wiki_repository = engine.repository_root(wiki)
    for path, label in targets.items():
        repository = data_repository if path == registry else wiki_repository
        repositories.setdefault(repository, {})[str(path.relative_to(repository))] = label
    dirty = set()
    for repository, names in repositories.items():
        records = iter(engine.git(repository, 'status', '--porcelain=v1', '-z',
                                  '--untracked-files=all', '--', *sorted(names),
                                  binary=True).decode('utf-8').split('\0'))
        for record in records:
            if not record:
                continue
            if record[3:] in names:
                dirty.add(names[record[3:]])
            if 'R' in record[:2] or 'C' in record[:2]:
                original = next(records, '')
                if original in names:
                    dirty.add(names[original])
    return sorted(dirty)


def _orphan_sources(config, journal_path, journal_ok):
    sources = _internal(config, '.state/sources')
    if sources is None:
        return 0
    if sources.is_symlink() or not sources.is_dir():
        return 0
    known = set()
    if journal_ok:
        try:
            connection = sqlite3.connect(journal_path.as_uri() + '?mode=ro', uri=True)
            try:
                known = {row[0] for row in connection.execute('SELECT source_hash FROM operations')}
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            known = set()
    try:
        return sum(path.suffix == '.md' and path.stem not in known for path in sources.iterdir())
    except OSError:
        return 0


def _graph_state(config):
    from .graph import Graph

    path = _internal(config, 'archive/archive.db')
    if path is None or not path.is_file():
        return {'status': 'failed', 'error_code': 'E_STATE'}
    try:
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            service = SimpleNamespace(root=config.root, config=config, archive=connection)
            return Graph(service).stats()['graph']
        finally:
            connection.close()
    except (KelmoError, OSError, sqlite3.Error):
        return {'status': 'failed', 'error_code': 'E_GRAPH'}


def hub_diagnostics(config):
    """Inspect generated views and configured links without opening instruction profiles."""
    from .hub_embed import inspect as inspect_embeds
    hot = config.hot or {}
    directory = config.wiki.parent / 'hot'
    budget = hot.get('views', {}).get('max_bytes', 48000)
    result = {}
    try:
        selections = inspect_embeds(config)
    except (KelmoError, OSError, ValueError) as error:
        selections = {}
        selection_error = str(error)
    else:
        selection_error = None
    for name, project in hot.get('projects', {}).items():
        view = directory / (name + '.md')
        mandatory = directory / 'mandatory.md'
        full = directory / (name + '.full.md')
        def present(path):
            return path.is_file() and not path.is_symlink()
        exists = present(view)
        result[name] = {
            'links': {key: project.get(key, []) for key in ('roots', 'common_dirs', 'remotes')},
            'view_present': exists, 'mandatory_present': present(mandatory),
            'full_present': present(full),
            'view_over_budget_bytes': max(0, view.stat().st_size - budget) if exists else None,
            'delivery': selections.get(name, {}),
            'selection_error': selection_error,
            'native_hook_trust': 'Approve and verify native hook trust in the client; file inspection cannot verify trust.',
            'repair': (f'kelmo project add {name}' if not (exists and present(mandatory) and present(full))
                       else 'Check client hook order and instruction imports in the selected profile.'),
        }
    return result


def diagnose(root):
    root = Path(root).expanduser()
    checks = {name: False for name in CHECK_NAMES}
    warnings = []
    issues = {'wiki_consistency': [], 'wiki_uncommitted': [], 'budget': []}
    budget_policy = {}
    budget_chains = []
    hot_policy = {}
    session_memory = {}
    graph = {'status': 'failed', 'error_code': 'E_CONFIG'}
    try:
        if root.is_symlink():
            raise ValueError
        config = Config.load(root)
    except Exception:  # noqa: BLE001 - Invalid configuration is itself diagnostic state.
        warnings.append('Vault configuration is missing, invalid, or uses a symlink; review it before retrying.')
        return {'healthy': False, 'checks': checks, 'orphan_sources': 0,
                'issues': issues, 'warnings': warnings,
                'budget_policy': budget_policy, 'budget_chains': budget_chains,
                'hot_policy': hot_policy, 'project_hubs': {}, 'session_memory': session_memory,
                'graph': graph, 'backup': None,
                'runtime_isolation': {'launcher_available': False},
                'deployment_parity_verified': False}

    graph = _graph_state(config)
    launcher_available = False
    try:
        if sys.platform == 'linux':
            from .linux_runtime import launcher, seccomp_filter
            seccomp_filter()
            path = launcher()
        elif sys.platform == 'darwin':
            path = Path('/usr/bin/sandbox-exec')
        else:
            raise ValueError('Unsupported runtime platform.')
        launcher_available = path.is_file() and os.access(path, os.X_OK)
    except (OSError, RuntimeError, ValueError, IsolationError):
        pass
    if not launcher_available:
        warnings.append('Runtime sandbox launcher is unavailable (bubblewrap on x86_64 Linux, sandbox-exec on '
                        'macOS). A local vault does not use it; a shared server\'s confined archive process does.')
    try:
        checks['git_identity'] = all(subprocess.run(
            ['git', '-C', str(config.knowledge_git_root), '-c', 'user.useConfigOnly=true', 'var', name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False,
        ).returncode == 0 for name in ('GIT_AUTHOR_IDENT', 'GIT_COMMITTER_IDENT'))
    except (OSError, subprocess.TimeoutExpired):
        pass
    if not checks['git_identity']:
        warnings.append('E_GIT_IDENTITY: configure a Git author and committer in the knowledge repository.')
    checks['graph'] = graph['status'] != 'failed'
    if not checks['graph']:
        warnings.append('Selected wiki graph needs attention; inspect graph stats and repair the source before rebuilding.')

    from .hot import check_all
    from .session_control import inspect
    session_memory=inspect(config.root)
    checks['sessions']=(config.sessions['mode']=='off' or config.sessions['paused'] or
        (not session_memory.get('error_code') and not any(j['state'] in ('needs_review','wiki_needs_review')
                                                        for j in session_memory['jobs'])))
    if not checks['sessions']:
        warnings.append('Session memory needs attention; inspect sessions list and retry the affected job after resolving its cause.')
    try:
        hot_policy = check_all(config)
        checks['hot'] = True
        for name, over in hot_policy['views_over_budget'].items():
            warnings.append(f'Generated view hot/{name} is {over} bytes over views_max_bytes after every '
                            'degradation step; raise the budget or move facts off the map page.')
    except (KelmoError, OSError, ValueError):
        warnings.append('HOT selection is unavailable or over budget; run kelmo hot --check-all.')

    archive_path = _internal(config, 'archive/archive.db')
    try:
        # The data repository is the bound knowledge repository; under schema 1 that is the root itself.
        checks['data_repository'] = engine.repository_root(config.knowledge_git_root) == config.knowledge_git_root
    except (KelmoError, OSError, RuntimeError, ValueError):
        pass
    checks['archive_integrity'], _ = _database(archive_path, ('memories',))
    journal_path = _internal(config, '.state/journal.sqlite3')
    checks['journal_integrity'], _ = _database(journal_path, ('operations', 'events', 'claims'))
    stale = _stale_claims(journal_path) if checks['journal_integrity'] else []
    if stale:
        issues['stale_claims'] = stale
        warnings.append(f'{len(stale)} operation claim(s) belong to a process that no longer runs; the next worker '
                        'pass takes them over: ' + ', '.join(item['operation_id'] for item in stale[:5]) + '.')
    try:
        locks = _stale_git_locks(config.knowledge_git_root)
    except (KelmoError, OSError, AttributeError):
        locks = []
    if locks:
        issues['stale_git_locks'] = locks
        warnings.append('A Git lock older than ten minutes holds every publication back; if no Git process '
                        'is running, remove it from the knowledge repository: ' + ', '.join(locks) + '.')
    waits = _long_waits(journal_path, session_memory.get('jobs', [])) if checks['journal_integrity'] else []
    if waits:
        issues['long_waits'] = waits
        named = [item.get('operation_id') or item.get('session_job_id') for item in waits[:5]]
        warnings.append(f'{len(waits)} note(s) have waited over an hour for their turn; look for a stuck lock '
                        '(.state/*.lock, a Git index.lock) or a stopped worker: ' + ', '.join(named) + '.')
    stopped = _failed_notes(journal_path) if checks['journal_integrity'] else {'count': 0, 'oldest_seconds': 0.0}
    if stopped['count']:
        issues['failed_notes'] = stopped
        warnings.append(f"{stopped['count']} note(s) failed and are waiting for a person: kelmo queue list names "
                        'them, kelmo trace <id> and kelmo status name the reason, and kelmo queue retry <id> gives '
                        'a note its attempts back. The oldest was written ' + _age_text(stopped['oldest_seconds'])
                        + ' ago.')

    registry = None
    registry_path = _internal(config, config.registry_relative)
    if registry_path is not None and registry_path.is_file():
        try:
            registry = engine.load_registry(config.root)
            checks['registry_integrity'] = True
        except (OSError, UnicodeError, ValueError):
            pass

    default_wiki = config.root / 'knowledge/wiki'
    wiki = (_internal(config, 'knowledge/wiki') if config.wiki == default_wiki
            else config.wiki)
    checks['wiki_available'] = (wiki is not None and wiki.is_dir() and not wiki.is_symlink()
                                and not (wiki / '.git').is_symlink())
    if checks['wiki_available']:
        checks['wiki_owner'] = engine.wiki_owner_valid(config.root, wiki)
    transaction = _internal(config, '.state/wiki-transaction.json')
    checks['wiki_transaction_clear'] = (transaction is not None and not transaction.exists()
                                          and not transaction.is_symlink())

    if registry is not None and checks['wiki_available'] and checks['wiki_owner']:
        unsafe = _unsafe_targets(wiki, registry)
        issues['wiki_consistency'] = unsafe
        if not unsafe:
            try:
                # The publisher's own projection at the committed publication sequence.
                pages = engine.render_registry(config.root, registry, wiki)
                checks['wiki_consistency'] = all(
                    path.exists() and not path.is_symlink()
                    and path.read_text(encoding='utf-8') == text
                    for path, text in pages.items())
                checks['wiki_committed'] = engine.wiki_committed(
                    config.root, registry, wiki_root=wiki)
                if not checks['wiki_committed']:
                    try:
                        issues['wiki_uncommitted'] = _dirty_pages(config, wiki, pages)
                    except (KelmoError, OSError, RuntimeError, ValueError):
                        warnings.append('Git status is unavailable; inspect the wiki and data repositories before retrying.')
                budget = policy.check(config.root, wiki_root=wiki, budget=config.budget)
                checks['budget'] = budget['ok']
                issues['budget'] = budget['violations']
                budget_policy = budget['scope']
                budget_chains = budget['chains']
                if budget_policy['hot_chains_uncovered']:
                    warnings.append('Configured instruction chains name no instruction file and were not measured: '
                                    + ', '.join(budget_policy['hot_chains_uncovered'])
                                    + '. Check each budget.chain root before trusting the budget check.')
                if budget_policy['hot_imports_unresolved']:
                    warnings.append('Configured instruction chains import pages this vault does not hold: '
                                    + '; '.join(budget_policy['hot_imports_unresolved'])
                                    + '. Those chains were measured with the import line kept and the page absent.')
                report = links.audit_managed(config.root, registry, wiki_root=wiki)
                checks['links'] = not report['broken'] and not report['ambiguous']
            except (KelmoError, OSError, RuntimeError, UnicodeError, ValueError):
                issues['wiki_consistency'] = _managed_targets(registry)

    model = _internal(config, '.cache/embeddings/model')
    embedding_warning = ('Selected embedding provider unavailable; check its profile, local artifacts '
                         'or broker channel. Search can use the lexical fallback.')
    if config.embedding is not None:
        try:
            from .embeddings import provider_from_settings
            provider = provider_from_settings(config.embedding)
            provider.embed_queries(['Synthetic model health check']).validate_count(1)
            checks['embedding_model'] = True
        except Exception:  # noqa: BLE001 - Provider failures are a health result.
            channel = config.embedding.get('fds_env')
            if config.embedding.get('provider') == 'broker' and channel and not os.environ.get(channel):
                embedding_warning = (f'{channel} is unset: doctor ran outside the embedding broker, so the '
                                     'provider was not reached; run it under kelmo broker to probe the channel.')
    elif model is not None and model.is_dir() and not model.is_symlink():
        try:
            from kelmo.store.memory.embed import embed
            embed('Synthetic model health check', cache_dir=str(config.cache))
            checks['embedding_model'] = True
        except Exception:  # noqa: BLE001 - Providers normalize model failures inconsistently.
            pass

    orphans = _orphan_sources(config, journal_path, checks['journal_integrity'])
    if orphans:
        warnings.append('Unreferenced source files were retained.')
    if not checks['archive_integrity']:
        warnings.append('Archive database is missing or unreadable; restore it before runtime.')
    if not checks['journal_integrity']:
        warnings.append('Operation journal is missing or unreadable; restore it before processing queued work.')
    if not checks['registry_integrity']:
        warnings.append('Fact registry is missing or invalid; restore it before applying facts.')
    if not checks['data_repository']:
        warnings.append('The data directory must have its own Git repository; restore its Git metadata before applying facts.')
    if not checks['wiki_available'] or not checks['wiki_owner']:
        warnings.append('Wiki is unavailable or its ownership record is invalid; review enrollment before retrying.')
    elif not checks['wiki_consistency']:
        warnings.append('Managed wiki pages are inconsistent; review the listed relative paths before retrying.')
    elif not checks['wiki_committed']:
        detail = ('the listed relative paths' if issues['wiki_uncommitted'] else
                  'managed wiki and registry changes')
        warnings.append(f'Wiki has uncommitted managed changes; restore {detail} to the committed bytes and '
                        'record any text you want to keep through kelmo create, then retry. Committing a hand '
                        'edit invalidates the knowledge generation.')
    if not checks['wiki_transaction_clear']:
        warnings.append('An unfinished wiki transaction was retained. Inspect managed-file changes, '
                        'then retry the original operation; do not delete the transaction marker.')
    if issues['budget']:
        warnings.append('Wiki budget exceeded; inspect the budget issues and review managed content before retrying.')
    if not checks['embedding_model']:
        warnings.append(embedding_warning)
    from .snapshot import last_backup
    backup = last_backup(config)
    if backup is not None and (backup.get('unreadable') or backup['age_hours'] > BACKUP_STALE_HOURS):
        warnings.append('The last verified vault backup is unreadable or older than '
                        f'{BACKUP_STALE_HOURS} hours; run kelmo snapshot backup in a writer pause.')
    return {'healthy': all(checks.values()), 'checks': checks, 'orphan_sources': orphans,
            'issues': issues, 'warnings': warnings,
            'budget_policy': budget_policy, 'budget_chains': budget_chains,
            'hot_policy': hot_policy, 'project_hubs': hub_diagnostics(config), 'session_memory': session_memory,
            'graph': graph, 'backup': backup,
            'runtime_isolation': {'launcher_available': launcher_available},
            'deployment_parity_verified': False}
