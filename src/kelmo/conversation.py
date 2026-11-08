"""One model conversation across the rounds of one editor job.

The editor asks the model in rounds: it may answer with a request (pages, a work page, a fact search) or a
proposal Kelmo refuses, and each round sends a new payload. Every round still carries the complete payload, so a
provider that cannot continue a conversation answers exactly as before. A provider that can continue one may
instead send only `message`: the payload keys that changed since the previous round, framed so the model applies
them to the payload it already holds. Which provider continues and how is the provider's business; Kelmo only
names the conversation and the turn.
"""
import hashlib

UPDATE_NOTE = ('Only the payload keys below changed since your last answer. Apply them to the payload you already '
               'hold: "changed" replaces the value of a key, "appended" adds items to the end of that key\'s list, '
               'and "removed" deletes a key. Every other key keeps its earlier value. Answer under the same '
               'instructions and output schema as before.')


def key(*parts):
    """A conversation key from the parts that make one job attempt unique; it reveals none of them."""
    return hashlib.sha256('\0'.join(str(part) for part in parts).encode('utf-8')).hexdigest()[:32]


def update(previous, current):
    """The message that turns the payload a model holds (`previous`) into `current`."""
    changed, appended = {}, {}
    for name, value in current.items():
        if name not in previous:
            changed[name] = value
        elif previous[name] == value:
            continue
        elif (isinstance(value, list) and isinstance(previous[name], list)
              and len(value) > len(previous[name]) and value[:len(previous[name])] == previous[name]):
            appended[name] = value[len(previous[name]):]
        else:
            changed[name] = value
    removed = sorted(name for name in previous if name not in current)
    return {'update': UPDATE_NOTE, 'changed': changed, 'appended': appended, 'removed': removed}


def turn(conversation_key, number, previous, current):
    """The conversation field of one round: turn 0 starts the conversation, a later turn carries its update."""
    if number == 0:
        return {'key': conversation_key, 'turn': 0, 'message': None}
    return {'key': conversation_key, 'turn': number, 'message': update(previous, current)}
