import json

import pytest

from kelmo.errors import KelmoError


def test_codex_bootstrap_is_not_user_work_and_tool_output_stays_evidence():
    from kelmo.session_transcripts import parse
    rows = [
        {'type': 'session_meta', 'payload': {'id': 'native'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [
            {'type': 'input_text', 'text': '<environment_context>generated</environment_context>\nActual request'}]}},
        {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'tool-1',
                                            'output': 'Synthetic tool evidence'}},
        {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'Actual request'}},
    ]
    result = parse('codex', b''.join(json.dumps(r).encode() + b'\n' for r in rows))
    assert result['user_turns'] == 1
    assert len(result['messages']) == 2
    assert result['messages'][0]['text'] == 'Actual request'
    assert result['messages'][1]['role'] == 'tool'
    assert result['messages'][1]['record'] == 3


def test_claude_tool_result_does_not_count_as_user_turn():
    from kelmo.session_transcripts import parse
    raw = json.dumps({'type': 'user', 'message': {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 'call', 'content': 'Synthetic result'}]}}).encode() + b'\n'
    result = parse('claude', raw)
    assert result['user_turns'] == 0
    assert result['messages'][0]['role'] == 'tool' and 'error' not in result['messages'][0]
    failed = json.dumps({'type': 'user', 'message': {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 'call', 'content': 'Exit 2', 'is_error': True}]}}).encode() + b'\n'
    assert parse('claude', failed)['messages'][0]['error'] is True


@pytest.mark.parametrize('provider, start, headless', [
    ('claude', {'type': 'user', 'entrypoint': 'sdk-cli', 'message': {'content': 'Helper task'}}, True),
    ('claude', {'type': 'user', 'entrypoint': 'cli', 'message': {'content': 'Requester request'}}, False),
    ('codex', {'type': 'session_meta', 'payload': {'id': 'n', 'originator': 'codex_exec'}}, True),
    ('codex', {'type': 'session_meta', 'payload': {'id': 'n', 'originator': 'codex-tui'}}, False),
    ('claude', {'type': 'user', 'message': {'content': 'No launch record'}}, False),
])
def test_the_first_launch_record_tells_a_headless_run_from_an_interactive_session(provider, start, headless):
    from kelmo.session_transcripts import parse
    rows = [{'type': 'summary', 'summary': 'carries no launch'}, start] if provider == 'claude' else [start]
    assert parse(provider, b''.join(json.dumps(r).encode() + b'\n' for r in rows))['headless'] is headless


@pytest.mark.parametrize('raw', [b'[]\n', b'{bad}\n', b'\xff\n'])
def test_invalid_complete_record_fails(raw):
    from kelmo.session_transcripts import parse
    with pytest.raises(KelmoError):
        parse('claude', raw)


def test_final_record_without_newline_is_accepted_when_complete():
    from kelmo.session_transcripts import parse
    raw = b'{"type":"user","message":{"content":"Actual request"}}'
    result = parse('claude', raw)
    assert result['records'] == 1
    assert result['partial_tail'] is False


@pytest.mark.parametrize('content', [None, 4, [None]])
def test_invalid_codex_message_content_is_not_silently_skipped(content):
    from kelmo.session_transcripts import parse
    raw = json.dumps({'type': 'response_item', 'payload': {
        'type': 'message', 'role': 'user', 'content': content}}).encode() + b'\n'
    with pytest.raises(KelmoError):
        parse('codex', raw)


def test_wrong_provider_format_is_rejected():
    from kelmo.session_transcripts import parse
    raw = b'{"type":"response_item","payload":{"type":"message","role":"user","content":"request"}}\n'
    with pytest.raises(KelmoError):
        parse('claude', raw)


def test_codex_event_only_messages_require_supported_adapter():
    from kelmo.session_transcripts import parse
    raw = b'{"type":"event_msg","payload":{"type":"user_message","message":"request"}}\n'
    with pytest.raises(KelmoError):
        parse('codex', raw)
