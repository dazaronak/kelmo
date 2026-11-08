"""Native hook events expressed through the same memory service."""
from .wiki import engine
from .errors import KelmoError
import sqlite3

PENDING_WARNING = 'Pending updates unavailable: the journal could not be read. The next prompt will retry.'


def _pending(service, project, registry=None):
    warning = getattr(service, 'pending_warning', '')
    if warning or getattr(service, 'journal', None) is None:
        return [], warning
    from .creation import pending_operations
    try:
        return [{'operation_id': operation['id'], 'project': request.get('project'),
                 'state': operation['state'], 'pending': request['pending']}
                for operation, request in pending_operations(service, project, include_unarchived=True, registry=registry)
                if request.get('pending')], ''
    except (sqlite3.Error, KelmoError, OSError, ValueError):
        return [], PENDING_WARNING


def hook(service, action, payload, *, acknowledge=None):
    if not isinstance(payload, dict):
        raise KelmoError('E_INPUT', 'Hook payload must be a JSON object.')
    if action in ('start', 'prompt'):
        from .config import Config
        from .hot import adopted, render, has_hub_generation
        pending_warning = ''
        pending_items = []
        try:
            config = Config.load(service.root)
            # A full service supplies its trusted reader; payload fields never enroll a root.
            reader = getattr(service, 'knowledge', None) if config.hot is not None and adopted(config) else None
            if reader is None and config.hot is not None and adopted(config):
                from .wiki.pages import KnowledgeReader
                reader = KnowledgeReader(config)
            if (config.hot and config.hot.get('views') and adopted(config)
                    and has_hub_generation(config, reader)):
                from .hot import hub_context
                from .hub_delivery import prepare, install_set
                delivered = hub_context(config, payload.get('cwd'), reader=reader)
                pending, pending_warning = _pending(service, delivered['project'], delivered.get('_registry'))
                provider = payload.get('kelmo_provider', 'claude')
                context = prepare(config, payload, delivered['hub_set'], delivered['project'],
                                  delivered['hub_work'], pending, provider=provider,
                                  current=lambda: install_set(config.state / 'hot', delivered['hub_set']),
                                  stale=delivered['freshness'] != 'current', acknowledge=None if pending_warning else acknowledge,
                                  notice='\n'.join(filter(None, (delivered['warning'], pending_warning))), work_path=delivered['hub_work_path'],
                                  aliases=delivered.get('project_aliases'),
                                  unlinked=delivered['project'] is None and bool(payload.get('cwd')), action=action)
            else:
                delivered = render(config, payload.get('cwd'), reader=reader)
                from .hub_delivery import legacy_delivery
                from .contracts import Generation
                registry = (reader.registry(Generation.from_dict(delivered['generation']))
                            if reader is not None and 'generation' in delivered else None)
                pending_items, pending_warning = _pending(service, delivered['project'], registry)
                context = legacy_delivery(config.state, delivered, pending_items[-20:],
                                          provider=payload.get('kelmo_provider', 'claude'), action=action,
                                          notice=pending_warning)
        except (KelmoError, OSError, ValueError) as error:
            code = error.code if isinstance(error, KelmoError) else 'E_HOT'
            state = 'incomplete' if code == 'E_BUDGET' else 'unavailable'
            context = f'Kelmo HOT {state} ({code}). No selected pages were delivered.'
            if code in ('E_BUDGET', 'E_GENERATION', 'E_BINDING'):
                context += ' ' + str(error)
        return {'hookSpecificOutput': {'hookEventName': 'SessionStart' if action == 'start' else 'UserPromptSubmit',
                                      'additionalContext': context}}
    message = payload.get('last_assistant_message')
    if isinstance(message, str) and message.strip():
        if len(message.encode()) > 1048576:
            raise KelmoError('E_INPUT', 'Hook evidence exceeds the intake limit; submit a scoped note.')
        # Raw hook evidence is not authorized fact promotion.
        service.capture(message, 'session-evidence.md', role='collector',
                        idempotency_key=engine.digest(str(payload.get('session_id', '')) + message))
    return {}
