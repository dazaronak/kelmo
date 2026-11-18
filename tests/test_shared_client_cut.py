"""Two edges of the shared client's resend after a cut: a failed read drops only the channel it failed on, never one
another thread opened in its place, and the outbox schema is brought up to date once when two processes open an
older outbox at the same time."""
import sqlite3
import sys
import threading
import time

from kelmo.client import Outbox, SharedClient, prepare_profile
from kelmo.config import ClientProfile

AUTHORITY = '0b1e6b0c-6f7a-4d4e-9c1a-5a3d2f1e0c9b'


class Channel:
    def __init__(self, name):
        self.name, self.closed, self.grace = name, False, 0

    def close(self):
        self.closed = True


def test_a_cut_read_leaves_the_channel_another_thread_opened_in_its_place(tmp_path, monkeypatch):
    prepare_profile(tmp_path / 'client', AUTHORITY, [sys.executable, '-c', 'pass'], timeout_seconds=5)
    client = SharedClient(ClientProfile.load(tmp_path / 'client'))
    dead, replacement = Channel('dead'), Channel('replacement')
    sends = []

    def connect():
        if client._channel is None:
            client._channel = dead
        return client._channel

    def send(channel, op, args, key):
        sends.append(channel.name)
        if len(sends) == 1:
            # The bridge answered for a server it lost; meanwhile another thread opened a healthy channel.
            client._channel = replacement
            return False, {'code': 'E_TRANSPORT', 'message': 'The daemon closed the channel.', 'retryable': True,
                           'cut': True}
        return True, {'answer': channel.name}

    monkeypatch.setattr(client, '_connect', connect)
    monkeypatch.setattr(client, '_send', send)
    try:
        result = client.call('search', {'query': 'exports'})
    finally:
        client._channel = None
        client.outbox.close()

    assert not replacement.closed, 'the read closed a channel another thread had just opened'
    assert result == {'answer': 'replacement'}
    assert sends == ['dead', 'replacement']


def test_an_older_outbox_opened_while_another_process_migrates_it_opens(tmp_path):
    directory = tmp_path / 'outbox'
    Outbox(directory).close()
    path = directory / 'outbox.sqlite3'
    with sqlite3.connect(path) as raw:
        raw.execute('ALTER TABLE entries DROP COLUMN cuts')
    other = sqlite3.connect(path, isolation_level=None)
    other.execute('BEGIN IMMEDIATE')  # another process is opening the same older outbox and migrating it
    outcome = []

    def open_outbox():
        try:
            outcome.append(Outbox(directory))
        except Exception as error:  # noqa: BLE001 - the failure is the outcome under test
            outcome.append(error)

    opener = threading.Thread(target=open_outbox)
    opener.start()
    time.sleep(0.5)
    other.execute('ALTER TABLE entries ADD COLUMN cuts INTEGER NOT NULL DEFAULT 0')
    other.execute('COMMIT')
    other.close()
    opener.join(30)
    assert len(outcome) == 1 and isinstance(outcome[0], Outbox), outcome
    outcome[0].close()
