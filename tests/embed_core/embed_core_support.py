"""Helpers for the embedding core tests.

Synthetic tokenizers and texts, a kelmo/2 peer for `kelmod embed`, a cold-broker vault, and a
daemon that runs the state, embedding and archive cores. Every tokenizer, vault and text is
synthetic and lives under a temp directory.
"""
import hashlib
import json
import os
import random
import socket
import sqlite3
import subprocess
import tempfile
import uuid
from collections import deque
from functools import lru_cache
from pathlib import Path

import pytest
from daemon_harness import KELMOD, SRC, Daemon, wait_for
from tokenizers import (
    Tokenizer,
    decoders,
    models,
    normalizers,
    pre_tokenizers,
    processors,
    trainers,
)

from kelmo.config import Config
from kelmo.contracts import CONTRACT_VERSION, EmbeddingSpace, SourcePacket
from kelmo.core import wire
from kelmo.core.spec import build
from kelmo.service import Service
from kelmo.store.memory.embed import InputPolicy, bound_transform, preparation_digest

PREFIX = 'passage: '
QUERY_PREFIX = 'query: '
ADDITIONS = ['<|pad|>', {'content': '[SPECIAL]', 'lstrip': True}]
SPECIALS = ['[UNK]', '[CLS]', '[SEP]', '[PAD]']
TRAINING_TEXTS = 200
BATCH_TEXTS = 256
BATCH_BYTES = 8 * 1048576
BROKER_FDS = 'SYNTHETIC_COLD_BROKER_FDS'
BROKER_LIMIT = 64
BROKER_DIM = 64
PROJECT = 'embed-core-check'
OBSERVED_AT = '2026-11-10T09:00:00+00:00'
VALID_FROM = '2026-11-10'
GIT_IDENTITY = {
    'GIT_AUTHOR_NAME': 'Synthetic Fixture',
    'GIT_COMMITTER_NAME': 'Synthetic Fixture',
    'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
    'GIT_COMMITTER_EMAIL': 'fixture@example.invalid',
    'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_CONFIG_GLOBAL': '/dev/null',
}
NEEDS_KELMOD = pytest.mark.skipif(
    not KELMOD.is_file(), reason=f'kelmod is not built at {KELMOD}; build it first')

# Characters that are invisible or combine with their neighbour are spelled out with chr().
COMBINING_ACUTE = chr(0x0301)
COMBINING_DIAERESIS = chr(0x0308)
ZWJ = chr(0x200D)
NBSP = chr(0x00A0)
IDEOGRAPHIC_SPACE = chr(0x3000)

# The pieces the synthetic texts are cut from: several scripts, combining marks, emoji with a
# ZWJ sequence and a skin tone, literal special tokens, and whitespace runs.
FRAGMENTS = (
    'Рассада томатов переехала в теплицу, ',
    'полив грядки номер ',
    'Αθήνα, Ἀθῆναι ',
    'cafe' + COMBINING_ACUTE + ' nai' + COMBINING_DIAERESIS + 've ',
    'Straße ＡＢＣ ﬁ ',
    '日本語のテキスト 汉字测试 한국어 ',
    'مرحبا بالعالم ',
    'नमस्ते दुनिया ',
    'emoji \U0001F331 \U0001F44D\U0001F3FD '
    + '\U0001F468' + ZWJ + '\U0001F469' + ZWJ + '\U0001F467 ',
    '<|pad|> ',
    '[SPECIAL]',
    '  [SPECIAL]  ',
    '[CLS] [SEP] ',
    'word word word ',
    '1234567890 punctuation!?;:... ',
    '\t\t',
    '   ',
    '\n\n',
    NBSP + IDEOGRAPHIC_SPACE,
    '\r\n',
)

RECORD = '\n\n'.join(
    f'Section {index}. Рассада томатов номер {index} в теплице, полив [SPECIAL] и <|pad|>, '
    f'\U0001F331 日本語 {index}.' for index in range(40))


@lru_cache(maxsize=None)
def synthetic_texts():
    """2000 texts of 0 to 20000 characters.

    The first three are empty, one character and the longest. The others are cut from random runs
    of FRAGMENTS, so every text mixes scripts, emoji, whitespace and special tokens.
    """
    rng = random.Random(20261110)
    texts = []
    for index in range(2000):
        target = (0, 1, 20000)[index] if index < 3 else rng.randint(0, 20000)
        pieces, size = [], 0
        while size < target:
            piece = rng.choice(FRAGMENTS)
            pieces.append(piece)
            size += len(piece)
        texts.append(''.join(pieces)[:target])
    return tuple(texts)


def _finish(tokenizer):
    """The tokenizer.json bytes of a trained tokenizer that wraps each input in [CLS] and [SEP].

    The special tokens are then counted the way an embedding model's loader counts them.
    """
    cls, sep = tokenizer.token_to_id('[CLS]'), tokenizer.token_to_id('[SEP]')
    tokenizer.post_processor = processors.TemplateProcessing(
        single='[CLS] $A [SEP]', special_tokens=[('[CLS]', cls), ('[SEP]', sep)])
    return tokenizer.to_str().encode('utf-8')


@lru_cache(maxsize=None)
def wordlevel_bytes():
    """A WordLevel tokenizer trained on the synthetic texts."""
    tokenizer = Tokenizer(models.WordLevel(unk_token='[UNK]'))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    trainer = trainers.WordLevelTrainer(vocab_size=2000, min_frequency=2, special_tokens=SPECIALS)
    tokenizer.train_from_iterator(synthetic_texts()[:TRAINING_TEXTS], trainer=trainer)
    return _finish(tokenizer)


@lru_cache(maxsize=None)
def bpe_bytes():
    """A byte-level BPE tokenizer with an NFKC and lowercase normalizer, trained on the texts."""
    tokenizer = Tokenizer(models.BPE(unk_token='[UNK]'))
    tokenizer.normalizer = normalizers.Sequence([normalizers.NFKC(), normalizers.Lowercase()])
    tokenizer.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tokenizer.decoder = decoders.ByteLevel()
    trainer = trainers.BpeTrainer(vocab_size=1000, min_frequency=2, special_tokens=SPECIALS,
                                  initial_alphabet=pre_tokenizers.ByteLevel.alphabet())
    tokenizer.train_from_iterator(synthetic_texts()[:TRAINING_TEXTS], trainer=trainer)
    return _finish(tokenizer)


def _batches(texts):
    """The texts in calls the frame bound accepts: at most BATCH_TEXTS texts, BATCH_BYTES each."""
    batch, size = [], 0
    for text in texts:
        weight = len(text.encode('utf-8'))
        if batch and (len(batch) == BATCH_TEXTS or size + weight > BATCH_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(text)
        size += weight
    if batch:
        yield batch


class Channel:
    """The far end of a kelmo/2 channel.

    Frames that arrive early wait in order until they are asked for.
    """

    def __init__(self, sock):
        self.sock = sock
        self.reader = wire.FrameReader()
        self.inbox = deque()

    def frame(self, timeout=60.0):
        """The next frame as (header, body), whichever call it answers."""
        while not self.inbox:
            self._receive(timeout)
        return self.inbox.popleft()

    def call(self, op, body, timeout=60.0):
        """(header, body) of the reply to one call."""
        call_id = uuid.uuid4().hex
        header = {'v': wire.VERSION, 'kind': 'call', 'id': call_id, 'op': op,
                  'body_type': 'json', 'deadline_ms': 60000}
        self.sock.sendall(wire.encode(header, body))
        while True:
            for index, (reply, value) in enumerate(self.inbox):
                if reply['id'] == call_id:
                    del self.inbox[index]
                    return reply, value
            self._receive(timeout)

    def _receive(self, timeout):
        self.sock.settimeout(timeout)
        data = self.sock.recv(1 << 16)
        if not data:
            raise ConnectionError('kelmod embed closed the channel')
        self.inbox.extend(self.reader.feed(data))


def _as_descriptor_three(fd):
    """Runs in the child before exec: the channel becomes descriptor 3, as the cores expect."""
    if fd == 3:
        os.set_inheritable(3, True)
    else:
        os.dup2(fd, 3)


class EmbedCore:
    """`kelmod embed` on a socketpair.

    Its kelmo/2 channel is descriptor 3 in the core. This object holds the peer end.
    """

    def __init__(self, tokenizer, log, additions=(), preparation=''):
        ours, theirs = socket.socketpair()
        fd = theirs.fileno()
        argv = [str(KELMOD), 'embed', '--tokenizer', str(tokenizer),
                '--additions', json.dumps(list(additions)), '--preparation', preparation]
        with log.open('w') as sink:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
                close_fds=False, preexec_fn=lambda: _as_descriptor_three(fd))
        theirs.close()
        self.channel = Channel(ours)
        try:
            self.hello = self.channel.frame()
        except (ConnectionError, TimeoutError) as error:
            self.close()
            raise AssertionError(f'kelmod embed sent no hello: {log.read_text()}') from error
        assert self.hello[0]['kind'] == 'hello', self.hello

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        """Closes the channel. The core ends on its own; one that does not is killed."""
        self.channel.sock.close()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    def call(self, op, body):
        return self.channel.call(op, body)

    def count(self, texts, prefix=''):
        """Counts of `texts`, and the set of (identity, preparation) pairs the answers name."""
        counts, answers = [], set()
        for batch in _batches(texts):
            header, body = self.channel.call('embed.count', {'texts': batch, 'prefix': prefix})
            assert header['ok'] is True, header
            counts += body['counts']
            answers.add((body['identity'], body['preparation']))
        return counts, answers

    def status(self):
        header, body = self.channel.call('admin.status', {})
        assert header['ok'] is True, header
        return body

    def trim(self):
        header, body = self.channel.call('admin.trim', {})
        assert header['ok'] is True, header
        return body['trimmed']


def toml_value(value):
    """A TOML literal for one special-token addition: a string, or an inline table of its fields."""
    if isinstance(value, dict):
        return '{ ' + ', '.join(f'{key} = {json.dumps(item)}' for key, item in value.items()) + ' }'
    return json.dumps(value)


def set_embedding(root, section):
    path = root / 'kelmo.toml'
    text = path.read_text().split('[embedding]')[0]
    path.write_text(text + section)


def cold_broker_vault(base, monkeypatch):
    """A vault whose embedding is a broker with no channel.

    The tokenizer and the input policy are declared, so chunks are cut from them while the broker
    is down, and vectors wait for it.
    """
    for key, value in GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv(BROKER_FDS, raising=False)
    root = base / 'vault'
    Service.initialize(root, 'fixture-node')
    tokenizer = root / '.cache' / 'broker-tokenizer.json'
    tokenizer.parent.mkdir(parents=True, exist_ok=True)
    tokenizer.write_bytes(bpe_bytes())
    identity = 'sha256:' + hashlib.sha256(tokenizer.read_bytes()).hexdigest()
    policy = InputPolicy(identity, BROKER_LIMIT, QUERY_PREFIX, PREFIX,
                         preparation_digest(ADDITIONS))
    space = EmbeddingSpace(CONTRACT_VERSION, 'broker-model@sha256:' + '0' * 64, identity,
                           bound_transform('broker:query', policy.query_prefix, policy),
                           bound_transform('broker:passage', policy.document_prefix, policy),
                           'mean', 'none', 'l2', BROKER_DIM, 'float32', 'little')
    lines = ['[embedding]', 'provider = "broker"', f'fds_env = "{BROKER_FDS}"',
             'tokenizer = ".cache/broker-tokenizer.json"', '[embedding.input]',
             f'max_tokens = {BROKER_LIMIT}', f'query_prefix = "{QUERY_PREFIX}"',
             f'document_prefix = "{PREFIX}"',
             'special_tokens = [' + ', '.join(toml_value(token) for token in ADDITIONS) + ']',
             '[embedding.space]']
    lines += [f'{key} = {json.dumps(value)}' for key, value in space.to_dict().items()]
    set_embedding(root, '\n'.join(lines) + '\n')
    return root


def archive_chunks(root):
    """(memory id, index, text, profile id) of every chunk in the vault's archive file.

    Read after the process that wrote them has ended.
    """
    connection = sqlite3.connect(Config.load(root).archive)
    try:
        return connection.execute(
            'SELECT memory_id, idx, text, profile_id FROM chunks '
            'ORDER BY memory_id, idx').fetchall()
    finally:
        connection.close()


def store_like_memory_store(service, content):
    """Captures `content` as the state core's memory_store does for the same arguments.

    One compat source packet, captured and run.
    """
    digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
    packet = SourcePacket(content.encode('utf-8'), 'compat', digest, 'memory-store.md', OBSERVED_AT,
                          kind='event', domain='dev', project=PROJECT, tags=(),
                          valid_from=VALID_FROM)
    operation = service.capture_source(packet, service.origin, 'embed-core-check')
    if not operation['terminal']:
        operation = service.run(operation['id'])
    assert operation['state'] == 'archived', operation
    return operation


def all_ready(daemon):
    status = daemon.status()
    if status is None:
        return False
    names = sorted(core['name'] for core in status['cores'])
    return names == ['archive', 'embed', 'state'] and all(core['ready'] for core in status['cores'])


def start_vault_daemon(root, workdir):
    """kelmod serving `root` with the state, embedding and archive cores, the archive in process.

    Returns once the three cores are ready.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    runtime = Path(tempfile.mkdtemp(prefix='ec', dir='/tmp'))
    spec = build(root, runtime_dir=runtime, source_root=SRC, archive_core=True,
                 archive_in_process=True, embed_core=True, kelmod=KELMOD)
    spec_path = workdir / 'spec.json'
    spec_path.write_text(json.dumps(spec))
    log = (workdir / 'kelmod.log').open('w')
    proc = subprocess.Popen([str(KELMOD), 'serve', '--spec', str(spec_path)],
                            stdout=log, stderr=log)
    daemon = Daemon(proc, spec_path, runtime, workdir / 'scratch', log)
    try:
        wait_for(lambda: all_ready(daemon), timeout=120,
                 what='the state, embedding and archive cores')
    except BaseException:
        daemon.stop()
        raise
    return daemon


def kelmod_call(daemon, op, body):
    """`kelmod call` against a running daemon. The reply is on stdout."""
    command = [str(KELMOD), 'call', op, '--runtime-dir', str(daemon.runtime),
               '--body', json.dumps(body), '--deadline-ms', '120000']
    return subprocess.run(command, capture_output=True, text=True, timeout=180)
