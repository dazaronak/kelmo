"""Broker routing is installed only for a configured embed-core provider."""
from types import SimpleNamespace

import pytest

from kelmo.core import archive_core
from kelmo.core.corelink import CoreLink
from kelmo.core.embed_client import EmbedCounter
from kelmo.embeddings import BrokeredProvider
from kelmo.store.errors import ArchiveEmbeddingError
from test_embedding_contract import broker_space


@pytest.mark.parametrize('embed_core,kind,command,described,expected', [
    (True, 'broker', ['/broker'], True, 'remote'),
    (True, 'broker', ['/broker'], False, 'pending'),
    # The embedding core did not answer at start: the channel is still wired, and every call asks it again.
    (True, 'broker', ['/broker'], None, 'remote'),
    (True, 'broker', None, True, 'default'),
    (True, 'local', ['/broker'], True, 'default'),
    (False, 'broker', ['/broker'], True, 'default'),
])
def test_archive_wires_provider_only_when_all_conditions_hold(monkeypatch, embed_core, kind, command,
                                                              described, expected):
    from kelmo.store.memory import embed

    observed = []
    settings = {'provider': kind, 'command': command, 'space': broker_space().to_dict(), 'tokenizer': '/tokenizer'}
    link = SimpleNamespace(serve=lambda: None, say_hello=lambda: None, hold_hello=False)
    monkeypatch.setattr(CoreLink, 'from_descriptor', lambda *args, **kwargs: link)
    monkeypatch.setattr(EmbedCounter, 'connect', lambda *args, **kwargs:
                        None if described is None else SimpleNamespace(provider=described))
    monkeypatch.setattr(embed, 'install_remote_counter', lambda counter: None)

    class Thread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

        def join(self):
            pass

    monkeypatch.setattr(archive_core.threading, 'Thread', Thread)

    def factory(start, *, confined, provider_factory=None):
        def worker():
            if provider_factory is None:
                observed.append('default')
            else:
                try:
                    observed.append('remote' if isinstance(provider_factory(settings), BrokeredProvider) else 'other')
                except ArchiveEmbeddingError:
                    observed.append('pending')
        return worker, []

    monkeypatch.setattr(archive_core, '_worker_factory', factory)
    archive_core.serve({'embed_core': embed_core, 'embedding': settings}, confined=False)
    assert observed == [expected]
