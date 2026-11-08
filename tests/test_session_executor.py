import sys
from kelmo import executor


def test_native_extraction_selects_its_own_prompt_and_schema(monkeypatch):
    seen=[]
    reply={'note_markdown':'Chose B only after approval.','candidate_facts':[],'skip':False}
    def run(argv,payload,**kwargs):
        seen.append((argv,payload))
        return {'structured_output':reply}
    monkeypatch.setattr(executor,'provider_execute',run)
    assert executor.resolve('claude','synthetic',{'task':'extract_final','messages':[]},executable=sys.executable)==reply
    argv,payload=seen[0]
    assert '--json-schema' in argv
    assert 'candidate_facts' in payload['instructions']
    assert 'complete note' in payload['instructions'].lower()
    assert 'Resolve the supplied fact changes' not in payload['instructions']


def test_reconciliation_prompt_does_not_assign_receipts(monkeypatch):
    seen=[]
    def run(argv,payload,**kwargs):
        seen.append(payload)
        return {'structured_output':{'decisions':[]}}
    monkeypatch.setattr(executor,'provider_execute',run)
    executor.resolve('claude','synthetic',{'task':'reconcile_session','candidates':[]},executable=sys.executable)
    assert 'decisions' in seen[0]['instructions']
    assert 'search receipts' in seen[0]['instructions']
