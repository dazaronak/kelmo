#!/usr/bin/env python3
"""An `[alerts]` command: send the alert text on stdin to a Telegram chat.

Usage: telegram-alert.py SETTINGS_FILE, where SETTINGS_FILE holds TELEGRAM_BOT_TOKEN=... and
TELEGRAM_CHAT_ID=... lines (other lines are ignored). Exits 0 once Telegram accepts the message. A failure
is reported by its exception type alone, so neither the token nor the chat id can reach a log.

    [alerts]
    command = ["/usr/bin/python3", "-I", "/opt/kelmo/telegram-alert.py", "/etc/kelmo/telegram.conf"]
"""
import json
import sys
import urllib.parse
import urllib.request

NAMES = ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID')


def main(settings_path):
    text = sys.stdin.read(4096).strip()
    if not text:
        raise ValueError('empty alert')
    values = {}
    with open(settings_path, encoding='utf-8') as settings:
        for line in settings:
            name, _, value = line.partition('=')
            if name.strip() in NAMES:
                values[name.strip()] = value.strip().strip('\'"')
    if not all(values.get(name) for name in NAMES):
        raise ValueError('telegram settings missing')
    request = urllib.request.Request(
        'https://api.telegram.org/bot' + values['TELEGRAM_BOT_TOKEN'] + '/sendMessage',
        data=urllib.parse.urlencode({'chat_id': values['TELEGRAM_CHAT_ID'], 'text': text}).encode())
    with urllib.request.urlopen(request, timeout=20) as response:
        if json.loads(response.read(65536)).get('ok') is not True:
            raise ValueError('telegram refused the message')


if __name__ == '__main__':
    try:
        main(sys.argv[1])
    except Exception as error:  # noqa: BLE001 - Report the type only; the message could carry the token.
        print(json.dumps({'alert_delivery': 'failed', 'error': type(error).__name__}), file=sys.stderr)
        sys.exit(1)
