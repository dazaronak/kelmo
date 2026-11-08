"""Validated session-memory settings shared by installer and runtime."""
from pathlib import Path

if __package__:
    from .errors import KelmoError
else:
    from errors import KelmoError


def validate(raw, root):
    raw = {} if raw is None else raw
    allowed = {'mode', 'sources', 'checkpoint_turns', 'max_snapshot_bytes', 'max_input_bytes',
               'executor_config','paused','extraction','tool_stub_chars','tool_result_chars','tool_error_chars',
               'max_calls'}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise KelmoError('E_CONFIG', 'Unsupported session-memory settings.')
    mode = raw.get('mode', 'off')
    if mode not in ('off', 'auto', 'archive-only'):
        raise KelmoError('E_CONFIG', 'Session memory mode must be off, auto or archive-only.')
    values = {'mode': mode}
    if type(raw.get('paused',False)) is not bool:
        raise KelmoError('E_CONFIG','Session paused flag must be boolean.')
    values['paused']=raw.get('paused',False)
    extraction = raw.get('extraction', 'tail')
    if extraction not in ('tail', 'complete'):
        raise KelmoError('E_CONFIG', 'Session extraction must be tail or complete.')
    values['extraction'] = extraction
    # What extraction reads of tool traffic (session_worker.reduce): a call as one line of at most
    # tool_stub_chars, a result whole up to tool_result_chars, an error result up to tool_error_chars;
    # max_calls bounds the model calls of one complete extraction.
    for key, default, ceiling, minimum in [('checkpoint_turns',30,1000,1),
            ('max_snapshot_bytes',16*1048576,64*1048576,1), ('max_input_bytes',131072,1048576,1024),
            ('tool_stub_chars',200,4000,40), ('tool_result_chars',400,65536,0),
            ('tool_error_chars',1500,65536,0), ('max_calls',8,64,1)]:
        value = raw.get(key, default)
        if type(value) is not int or not minimum <= value <= ceiling:
            raise KelmoError('E_CONFIG', f'Invalid session limit: {key}.')
        values[key] = value
    sources = raw.get('sources', {})
    if not isinstance(sources, dict) or set(sources) - {'claude', 'codex'}:
        raise KelmoError('E_CONFIG', 'Session sources must map supported providers to directory lists.')
    values['sources'] = {}
    for provider, roots in sources.items():
        if not isinstance(roots, list) or not roots:
            raise KelmoError('E_CONFIG', 'Enrolled session source roots must be a nonempty list.')
        paths = []
        for item in roots:
            if not isinstance(item,str) or not item.strip() or any(ord(c)<32 for c in item):
                raise KelmoError('E_CONFIG', 'Invalid session source root.')
            path = Path(item).expanduser()
            path = path if path.is_absolute() else root / path
            paths.append(str(path.absolute()))
        values['sources'][provider] = paths
    executor = raw.get('executor_config')
    if executor is not None:
        if not isinstance(executor,str) or not executor.strip() or any(ord(c)<32 for c in executor):
            raise KelmoError('E_CONFIG', 'Invalid session executor selection.')
        path = Path(executor).expanduser()
        values['executor_config'] = str(path if path.is_absolute() else root / path)
    return values
