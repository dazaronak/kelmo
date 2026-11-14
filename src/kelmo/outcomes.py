"""Every settled source carries one outcome and the reason that produced it."""

from .errors import KelmoError
from .journal import now

OUTCOMES = ('applied', 'archive-only', 'unresolved')
REASON_LIMIT = 400
NAMED_FACTS = 8

CAUSES = {
    'E_UNRESOLVED': 'the executor declared these facts unresolvable',
    'E_STALE': 'the registry moved after this request was created',
    'E_UNION': 'the commit of a batch was refused as a whole; the note is asked again alone',
    'E_PROPOSAL': 'the proposal did not fit the requested facts',
    'E_EXECUTOR': 'the executor did not return a usable answer',
    'E_CONFLICT': 'the wiki state conflicts with this request',
    'E_BUSY': 'no worker could take this request',
    'E_EMBEDDING': 'the embedding service could not index this source',
    'E_GIT': 'Git refused the wiki commit',
    'E_IO': 'the vault could not be read or written',
    'E_PROCESSING': 'the worker failed while processing this source',
    'E_QUOTA': 'the provider is rate limited',
}


def schema(connection):
    with connection:
        connection.execute('''CREATE TABLE IF NOT EXISTS source_outcomes (
            operation_id TEXT PRIMARY KEY, disposition TEXT NOT NULL,
            reason TEXT NOT NULL, updated_at TEXT NOT NULL
        )''')


def validate(disposition, reason):
    if disposition not in OUTCOMES:
        raise KelmoError('E_OUTCOME', 'Source outcome must be one of ' + ', '.join(OUTCOMES) + '.')
    reason = ' '.join(str(reason).split())
    if not reason or len(reason) > REASON_LIMIT:
        raise KelmoError('E_OUTCOME',
                        'Source outcome reason must be 1 to ' + str(REASON_LIMIT) + ' characters; got '
                        + str(len(reason)) + '.')
    return disposition, reason


def named_facts(keys, room=REASON_LIMIT):
    """The first keys, each whole, as many as `room` characters hold (at most NAMED_FACTS); the rest as a count."""
    keys = sorted(keys)
    for count in range(min(len(keys), NAMED_FACTS), 0, -1):
        extra = len(keys) - count
        text = ', '.join(keys[:count]) + ('' if not extra else ' (+' + str(extra) + ' more)')
        if len(text) <= room:
            return text
    return '(+' + str(len(keys)) + ' more)' if keys else ''


def fact_reason(keys, prefix, suffix=''):
    return prefix + named_facts(keys, REASON_LIMIT - len(prefix) - len(suffix)) + suffix


def cause(code):
    return CAUSES.get(code, 'the worker could not complete this operation')


def exhaustion_reason(code, attempts, keys):
    detail = cause(code)
    if keys:
        detail += ' for ' + named_facts(keys)
    return repair_reason('E_ATTEMPTS', detail + ' after ' + str(attempts) + ' attempt(s)')


def clipped(reason):
    """One outcome reason that fits the limit: whitespace collapsed, a long one cut with an ellipsis."""
    reason = ' '.join(str(reason).split())
    return reason if len(reason) <= REASON_LIMIT else reason[:REASON_LIMIT - 3] + '...'


def repair_reason(code, error):
    return clipped(str(code) + ': ' + str(error))


def promotion_outcome(facts, source_hash):
    """The outcome of a promote that reached the wiki: which facts this source now carries."""
    keys = sorted(key for key, fact in facts.items() if fact.get('note_id') == source_hash)
    if keys:
        return 'applied', fact_reason(keys, 'fact applied: ')
    return 'archive-only', 'every requested change was already in the registry; the note adds no new fact'


def write(connection, operation_id, disposition, reason):
    """Store the outcome for one settled source; the caller owns the transaction."""
    disposition, reason = validate(disposition, reason)
    connection.execute('''INSERT INTO source_outcomes(operation_id,disposition,reason,updated_at)
        VALUES (?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET disposition=excluded.disposition,
        reason=excluded.reason, updated_at=excluded.updated_at''', (operation_id, disposition, reason, now()))
    return {'disposition': disposition, 'reason': reason}


def record(connection, operation_id, disposition, reason):
    with connection:
        return write(connection, operation_id, disposition, reason)


def clear(connection, operation_id):
    """A source that is unsettled again has no outcome until it settles; the caller owns the transaction."""
    connection.execute('DELETE FROM source_outcomes WHERE operation_id=?', (operation_id,))


def get(connection, operation_id):
    row = connection.execute('SELECT disposition,reason,updated_at FROM source_outcomes WHERE operation_id=?',
                             (operation_id,)).fetchone()
    return dict(row) if row is not None else None


def summary(connection):
    counts = dict.fromkeys(OUTCOMES, 0)
    for row in connection.execute('SELECT disposition,COUNT(*) AS n FROM source_outcomes GROUP BY disposition'):
        counts[row['disposition']] = row['n']
    return counts
