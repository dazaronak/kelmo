"""Versioned hook definitions shared by setup and removal."""
import shlex

EVENTS = ('SessionStart','UserPromptSubmit','Stop','PreCompact','SessionEnd')


def definitions(executable,vault,client,mode=None,edge=None):
    """The managed hooks. With `edge` ({'kelmod': path, 'state': client state directory}) each hook is
    `kelmod hook ACTION`, which reaches the machine's daemon or spools the event; otherwise `kelmo hook`."""
    values = {'SessionStart':('start',20), 'UserPromptSubmit':('prompt',20)}
    if mode is None:
        values['Stop']=('capture',20)
    elif mode!='off':
        values.update({'Stop':('session-stop',10),'PreCompact':('session-checkpoint',10),
                       'SessionEnd':('session-end',10)})
    result = {}
    for event,(action,timeout) in values.items():
        if edge is not None:
            command=[str(edge['kelmod']),'hook',action,'--provider',client,'--runtime-dir',str(edge['runtime']),
                     '--state',str(edge['state'])]
        else:
            command=[str(executable),'hook',action]
            if action.startswith('session-') or action in ('start', 'prompt'):
                command+=['--provider',client]
            command+=['--vault',str(vault)]
        result[event]={'type':'command','command':shlex.join(command),'timeout':timeout,
                       'statusMessage':'kelmo managed hook'}
        if client == 'codex' and event in ('SessionStart', 'UserPromptSubmit'):
            result[event]['additionalContextLimit'] = 0
    return result


def variants(executable,vault,client,event,edge=None):
    current = [value[event] for mode in (None,'auto') for target in ((None, edge) if edge else (None,))
            if event in (value:=definitions(executable,vault,client,mode,target))]
    # Recognize hooks installed before provider-aware delivery.
    if event == 'SessionStart':
        command = shlex.join([str(executable), 'hook', 'start', '--vault', str(vault)])
        current.append({'type': 'command', 'command': command, 'timeout': 20,
                        'statusMessage': 'kelmo managed hook'})
    return current
