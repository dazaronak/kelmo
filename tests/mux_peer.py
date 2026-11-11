"""Small kelmo/2 subprocess peer for black-box client channel tests."""
import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from kelmo.core import wire


def send(header, body):
    view = memoryview(wire.encode(header, body))
    while view:
        view = view[os.write(sys.stdout.fileno(), view):]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--legacy', action='store_true')
    parser.add_argument('--no-client-request', action='store_true')
    parser.add_argument('--die-after', type=int)
    parser.add_argument('--refuse')
    # With an authority the peer answers the client's own `hello` request as a server would, and `--legacy`
    # serves kelmo-client/1 lines, one request at a time, to a client that opens with one.
    parser.add_argument('--authority')
    parser.add_argument('--die-once', help='Marker file: exit on the first request while it does not exist')
    options = parser.parse_args()

    def result(request, key):
        if options.authority and request.get('op') == 'hello':
            return {'authority_id': options.authority, 'operations': ['hello', 'search', 'capture']}
        return {'echo': request['args'], 'key': key}

    def serve_lines(buffer):
        while True:
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                request = json.loads(line)
                time.sleep(request.get('args', {}).get('delay', 0))
                reply = {'protocol': 'kelmo-client/1', 'request_id': request['request_id'], 'ok': True,
                         'result': result(request, request.get('idempotency_key'))}
                os.write(sys.stdout.fileno(), json.dumps(reply).encode('utf-8') + b'\n')
            chunk = os.read(sys.stdin.fileno(), 65536)
            if not chunk:
                return
            buffer += chunk
    reader = wire.FrameReader()
    first = True
    write_lock = threading.Lock()
    answer_count = 0
    answer_lock = threading.Lock()

    def answer(header, request):
        nonlocal answer_count
        if options.die_once and request.get('op') != 'hello' and not os.path.exists(options.die_once):
            # The first request after a hello is received and never answered; the next process serves.
            open(options.die_once, 'w').close()
            os._exit(0)
        delay = request.get('args', {}).get('delay', 0)
        time.sleep(delay)
        if options.refuse:
            reply_header, body = wire.reply(
                header['id'], error={
                    'code': options.refuse,
                    'message': 'refused',
                    'retryable': True,
                }
            )
        else:
            reply_header, body = wire.reply(header['id'], {
                'protocol': 'kelmo-client/1',
                'request_id': request['request_id'],
                'ok': True,
                'result': result(request, header.get('key')),
            })
        with write_lock:
            send(reply_header, body)
        with answer_lock:
            answer_count += 1
            if options.die_after is not None and answer_count >= options.die_after:
                os._exit(0)

    while True:
        chunk = os.read(sys.stdin.fileno(), 65536)
        if not chunk:
            return
        if first:
            first = False
            if options.legacy:
                # As the 0.1.1 server does: nothing is answered before a whole line has arrived.
                while b'\n' not in chunk:
                    more = os.read(sys.stdin.fileno(), 65536)
                    if not more:
                        return
                    chunk += more
                if options.authority and chunk.startswith(b'{'):
                    return serve_lines(chunk)
                os.write(sys.stdout.fileno(), b'{"protocol":"kelmo-client/1","ok":false}\n')
                return
        for header, body in reader.feed(chunk):
            if header['kind'] == 'hello':
                operations = [] if options.no_client_request else ['state.client_request']
                send({
                    'v': 2,
                    'kind': 'hello',
                    'id': header['id'],
                    'body_type': 'json',
                    'protocols': ['kelmo/2'],
                    'operations': operations,
                }, None)
                if options.die_after == 0:
                    return
            elif header['kind'] == 'call':
                threading.Thread(target=answer, args=(header, body['request']), daemon=True).start()


if __name__ == '__main__':
    main()
