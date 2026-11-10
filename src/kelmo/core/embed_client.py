"""The archive core's way to the embedding core: token counts by `embed.count` through kelmod.

`EmbedCounter` is what `kelmo.store.memory.embed.install_remote_counter` takes: a window over the tokenizer the
embedding core serves (same identity and preparation) counts there, so the archive core parses no tokenizer.
"""
import os
import sys

from ..errors import KelmoError
from .context import Context

# Texts per call and characters per call: a call stays far below the 32 MiB frame bound.
BATCH_TEXTS = 256
BATCH_CHARS = 4 * 1048576
SECONDS = 60.0


class EmbedCounter:
    def __init__(self, link, identity, preparation):
        self.link = link
        self.identity = identity
        self.preparation = preparation

    @classmethod
    def connect(cls, link, seconds=30.0):
        """The counter of the embedding core behind `link`, or None (with the reason on stderr) when it does not
        answer: the archive then counts with its own tokenizer, as before."""
        try:
            described = link.call('embed.describe', {}, Context.local(os.urandom(16).hex(), seconds)).result(
                seconds + 5)
        except (KelmoError, TimeoutError) as error:
            print(f'kelmo archive core: no embedding core ({error}); counting locally', file=sys.stderr, flush=True)
            return None
        return cls(link, described['tokenizer_identity'], described['preparation'])

    def serves(self, identity, preparation):
        return (identity, preparation) == (self.identity, self.preparation)

    def _batches(self, texts):
        batch, size = [], 0
        for text in texts:
            if batch and (len(batch) >= BATCH_TEXTS or size + len(text) > BATCH_CHARS):
                yield batch
                batch, size = [], 0
            batch.append(text)
            size += len(text)
        if batch:
            yield batch

    def count(self, texts, prefix):
        counts = []
        for batch in self._batches(texts):
            context = Context.local(os.urandom(16).hex(), SECONDS)
            body = self.link.call('embed.count', {'texts': batch, 'prefix': prefix}, context).result(SECONDS + 5)
            if (body.get('identity'), body.get('preparation')) != (self.identity, self.preparation):
                raise KelmoError('E_EMBEDDING', 'The embedding core changed its tokenizer.')
            counts += body['counts']
        return counts
