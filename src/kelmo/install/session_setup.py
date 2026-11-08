"""Installer-owned session settings and synthetic pipeline readiness."""
import json
from pathlib import Path
import tempfile

from ..config import Config
from ..errors import KelmoError
from ..session_settings import validate


def prepare(args, home, vault, *, check_provider=True):
    from .setup import client_paths, profile_options, read_json
    from ..daemon import validate_config
    from ..executor import provider_path
    config = Config.load(vault) if (vault/'kelmo.toml').is_file() else None
    existing=config.sessions if config else validate(None,vault)
    state = config.state if config else vault/'.state'
    mode=getattr(args,'session_memory',None) or existing['mode']
    provider,model=getattr(args,'extractor_provider',None),getattr(args,'extractor_model',None)
    if bool(provider)!=bool(model):
        raise KelmoError('E_CONFIG','Extractor provider and model must be selected together.')
    if provider and mode=='off':
        raise KelmoError('E_CONFIG','Enable session memory before selecting an extraction model.')
    profile=existing.get('executor_config')
    if profile and mode!='off' and not provider and not args.provider and not args.executor_config:
        validate_config(read_json(Path(profile),required=True))
    if mode!='off' and not (provider or args.provider or args.executor_config or profile or (state/'executor.json').is_file()):
        raise KelmoError('E_CONFIG','Session memory requires an extraction-capable executor.')
    sources=dict(existing['sources'])
    for client in ([] if args.client=='none' else ['claude','codex'] if args.client=='both' else [args.client]):
        selected, _ = client_paths(home, client, **profile_options(args))
        sources.setdefault(client,[str(selected/('projects' if client=='claude' else 'sessions'))])
    settings={**existing,'mode':mode,'sources':sources}
    if getattr(args,'session_memory',None) is not None:
        settings['paused']=False
    return {'settings':settings,'provider':provider,'model':model,
            'provider_executable':provider_path(provider) if provider and check_provider else None,
            'write_settings':mode!='off' or getattr(args,'session_memory',None) is not None}


def probe(vault, profile, settings):
    from ..service import Service
    from ..model_store import import_model
    from ..session_control import configure
    from ..sessions import Sessions
    from ..session_worker import tick
    with tempfile.TemporaryDirectory(prefix='kelmo-session-probe-') as directory:
        base=Path(directory).resolve()
        target=base/'vault'
        Service.initialize(target,'setup-probe',project='setup-probe')
        config = Config.load(vault)
        if config.embedding is not None:
            from ..model_selection import use
            import tomllib
            selected = tomllib.loads((config.root / 'kelmo.toml').read_text())['embedding']
            key = 'model_dir' if selected['provider'] == 'local' else 'tokenizer'
            selected[key] = config.embedding[key]
            use(target, selected)
        elif (config.cache/'model').is_dir():
            import_model(target,config.cache/'model')
        configure(target,{'mode':settings['mode']})
        for name,message in [('decision','We rejected A and chose B for project setup-probe, only after explicit approval. Record that durable decision.'),
                ('history','We inspected a temporary log during troubleshooting, found no durable facts and made no decision or change. Preserve this work history with no wiki candidates.')]:
            path=base/(name+'.jsonl')
            path.write_text(json.dumps({'type':'user','message':{'content':message}})+'\n')
            sessions=Sessions(Config.load(target))
            try:
                sessions.capture('claude',name,path,base,'SessionEnd')
            finally:
                sessions.close()
            outcome=tick(target,profile['argv'],timeout=profile['timeout_seconds'],max_attempts=1)
            expected=('wiki_applied' if name=='decision' and settings['mode']=='auto' else 'wiki_unchanged')
            if not outcome['jobs'] or outcome['jobs'][0]['state']!=expected:
                state=outcome['jobs'][0]['state'] if outcome['jobs'] else 'not_started'
                code=outcome['jobs'][0].get('error_code') if outcome['jobs'] else 'E_EXECUTOR'
                raise KelmoError('E_SETUP',f'Session pipeline probe {name} failed ({state}, {code}); '
                                'inspect extraction capability before enabling hooks.')
        with Service(target) as service:
            if service.status()['archive_records']!=2:
                raise KelmoError('E_SETUP','Session pipeline probe did not preserve both complete notes.')
    return {'extraction_ready':True,'session_pipeline_ready':True,'native_hooks_verified':False}


def finish(args, plan, vault, executable, fallback):
    from .setup import read_json,write
    from ..daemon import validate_config
    from ..session_control import configure
    from ..sessions import Sessions
    settings=plan['settings']
    ready={'extraction_ready':False,'session_pipeline_ready':False,'native_hooks_verified':False}
    if settings['mode']!='off':
        state = Config.load(vault).state
        selected=Path(settings.get('executor_config') or fallback or state/'executor.json')
        if plan['provider']:
            selected=state/'session-executor.json'
            profile={'argv':[str(executable),'executor','--provider',plan['provider'],'--model',plan['model'],
                '--provider-executable',plan['provider_executable'],'--timeout','560'],'timeout_seconds':600}
            write(selected,(json.dumps(profile)+'\n').encode())
        elif args.provider or args.executor_config:
            selected=Path(fallback)
        profile=validate_config(read_json(selected,required=True))
        if not profile['argv']:
            raise KelmoError('E_CONFIG','Session extraction executor has no command.')
        ready=probe(vault,profile,settings)
        settings['executor_config']=str(selected)
    if plan['write_settings']:
        configure(vault,settings)
        if settings['mode']!='off':
            Sessions(Config.load(vault)).close()
    return dict(ready,session_memory_mode=settings['mode'])
