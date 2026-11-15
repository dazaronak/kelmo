"""What became of every part of a note: the units an editor answer must account for.

A note is cut into numbered units by `units`, a pure function of its text, so nothing about them is stored:
the same numbers come back whenever an answer is checked or a trace is written. An answer names, on each item
it proposes, the units that item states, and lists every other unit in `left` with one of `REASONS`.
`ledger` reads an answer against the units and says which are still open; `refusal` words the repair.

Nothing here decides whether a note is published. The editor asks for a repair while it has repairs left and
records what stayed open: an accounting gap costs rounds, never a note.

What the code can check, it checks: a unit number exists, a named item was shown or is the note's own and
was taken, a `stated` item mentions something the unit names, a `context` unit names nothing, an `elsewhere`
page exists and is not one the answer could have changed. Whether an `evidence` unit really is only evidence
is the model's judgement; it is recorded with its unit so that a reader can look at exactly those.
"""
import re

# Why a unit is in no proposed item. `stated` and `evidence` name the items that hold or carry it, so the
# unit stays reachable from the wiki; `context` makes no claim; `elsewhere` belongs on a page this
# conversation may not change.
REASONS = ('stated', 'evidence', 'context', 'elsewhere')
NAMING = ('stated', 'evidence')
ITEM_KINDS = {'facts': 'fact_key', 'narratives': 'narrative_id', 'pages': 'target'}
WHERE_LIMIT = 120
# A refusal names at most this many open units of one source, each with the start of its text; the count
# of the rest follows.
NAMED_UNITS = 30
SHOWN_TEXT = 120

_FENCE = re.compile(r'\s*(```|~~~)')
_RULE = re.compile(r'[-*_=\s]{3,}|\|?[\s:|-]*-[\s:|-]*\|?')
_MARKER = re.compile(r'\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?')
_HEADING = re.compile(r'\s*#{1,6}\s+')
# A sentence ends at ., ! or ? before whitespace, and at a closing quote or bracket after one, or at a
# semicolon, before a new clause that starts with a capital, a digit, a quote or code. A unit that holds two sentences
# can hide the loss of one; a fragment cut off after a semicolon ("then the scan;") is a claim nobody can
# give a fate, so a lower-case continuation stays with its sentence.
_END = re.compile(r'(?<=[.!?])\s+|(?:(?<=[.!?][)"»”\'\]])|(?<=;))\s+(?=[A-ZА-ЯЁ0-9«"“`(\[])')
# A single letter before a full stop is not on the list: "Option A. Then the backup runs." is two sentences,
# and an initial cut from its surname ("J." / "Doe") costs a fragment, where two sentences in one unit can
# hide the loss of one.
_ABBREVIATION = re.compile(r'(?:^|[\s(])(?:e\.g|i\.e|etc|vs|cf|approx|fig|mr|mrs|dr|st|т\.е|т\.д|т\.п|см|напр)\.$',
                           re.IGNORECASE)
# The version of the cut, kept with every record: a record's unit numbers are read against the cut that
# made them.
CUT = 1
# What a faithful restatement keeps: code spans, anything with a digit, a path (it holds a dot or starts at
# a root; "and/or" is prose) and a file name.
_IDENTIFIER = re.compile(r'`[^`\n]+`|[\w./~-]*\d[\w./:%-]*|(?<![\w/.~])(?:~|\.{1,2})?/(?:[\w.-]+/)*[\w.-]+'
                         r'|(?:[\w-]+/)+[\w-]*\.[\w.-]+'
                         r'|\b[\w-]+\.(?:md|py|sh|toml|json|jsonl|rs|bak|yml|yaml|txt|log|html)\b')
_QUOTED = re.compile(r'«|“|"[^"]{3,}"')
_QUOTATION = re.compile(r'«([^»]{3,})»|“([^”]{3,})”')
_WORD = re.compile(r'[A-Za-z]{5,}')
# A record names at most this many identifiers a unit's items dropped.
DROPPED_SHOWN = 6


def body(text):
    """A note's text without its frontmatter block: a leading `---`, `key: value` lines, a closing `---`.

    A note that merely opens with a rule is no frontmatter, and nothing of it is left out of the units."""
    if re.match(r'---[ \t]*\n[ \t]*[\w-]+[ \t]*:', text):
        end = re.search(r'^---\s*$', text[3:], re.MULTILINE)
        if end:
            return text[3 + end.end():]
    return text


_PAIRS = (('«', '»'), ('“', '”'), ('(', ')'))
_TOGGLES = ('"', '`')


def _unfinished(held, kept=_PAIRS + _TOGGLES):
    """True while a cut here would fall inside a quotation, a bracket or a code span, or right after an
    abbreviation. `kept` names the marks that count."""
    return (any(pair in kept and held.count(pair[0]) > held.count(pair[1]) for pair in _PAIRS)
            or any(mark in kept and held.count(mark) % 2 == 1 for mark in _TOGGLES)
            or _ABBREVIATION.search(held) is not None)


def _sentences(block):
    """Cut one paragraph, list item or heading into sentences; a quotation and a code span stay whole.

    A mark the block never closes (a lone bracket in ":(", an inch sign) holds nothing together: it would
    glue the rest of the paragraph into one unit."""
    kept = (tuple(pair for pair in _PAIRS if block.count(pair[0]) == block.count(pair[1]))
            + tuple(mark for mark in _TOGGLES if block.count(mark) % 2 == 0))
    parts, held = [], ''
    for piece in _END.split(block):
        held = (held + ' ' + piece).strip() if held else piece.strip()
        if held and not _unfinished(held, kept):
            parts.append(held)
            held = ''
    if held:
        parts.append(held)
    return parts


def units(text):
    """The numbered units of a note, in reading order: `units(text)[n - 1]` is unit `n`.

    A fenced block is one unit and so is a table row; a heading, a list item and a paragraph are cut into
    sentences. Rules, table separators and blank lines are not units. The frontmatter is not part of them.
    """
    found, block, fence, fenced = [], [], None, []

    def flush():
        if block:
            found.extend(_sentences(' '.join(block)))
            block.clear()

    for line in body(text).splitlines():
        stripped = line.strip()
        if fence is not None:
            fenced.append(line.rstrip())
            if stripped.startswith(fence) and not stripped.strip(fence[0]):
                found.append('\n'.join(fenced))
                fence, fenced = None, []
            continue
        opened = _FENCE.match(line)
        if opened:
            flush()
            fence, fenced = opened.group(1), [line.rstrip()]
            continue
        if not stripped or _RULE.fullmatch(stripped):
            flush()
            continue
        if stripped.startswith('|'):
            flush()
            found.append(stripped)
            continue
        heading, marker = _HEADING.match(line), _MARKER.match(line)
        if heading or marker:
            flush()
            block.append(line[(heading or marker).end():].strip())
            if heading:
                flush()
            continue
        block.append(stripped)
    flush()
    if fenced:
        found.append('\n'.join(fenced))
    return [unit for unit in found if unit]


def numbered(text):
    """The units of a note as the payload carries them."""
    return [{'n': number, 'text': unit} for number, unit in enumerate(units(text), 1)]


def identifiers(text):
    """The tokens of a text a faithful restatement would keep: numbers, paths, file names and code."""
    return {token.strip('`.,;:()«»"\'').lower() for token in _IDENTIFIER.findall(text)} - {''}


def strip(result):
    """An answer without its accounting, as the proposal checks read it."""
    if not isinstance(result, dict) or not ('left' in result or any(
            isinstance(item, dict) and 'units' in item
            for kind in ITEM_KINDS for item in (result.get(kind) if isinstance(result.get(kind), list) else ()))):
        return result
    plain = {name: value for name, value in result.items() if name != 'left'}
    for kind in ITEM_KINDS:
        if isinstance(plain.get(kind), list):
            plain[kind] = [{name: value for name, value in item.items() if name != 'units'}
                           if isinstance(item, dict) else item for item in plain[kind]]
    return plain


def _numbers(value, total):
    """The unit numbers a list names that exist, and whether every entry of it was one."""
    if not isinstance(value, list):
        return [], False
    good = [number for number in value if type(number) is int and 1 <= number <= total]
    return good, len(good) == len(value)


def _anchored(text, restated):
    """Which identifiers of a unit its restatement keeps: `(kept, named)` counts; a unit that names none
    has nothing to look for and is `(0, 0)`.

    An identifier is kept when the restatement names it too, or holds it inside a longer one (`0.3.0` in
    `v0.3.0`). A short one must be named whole: `11` is not kept by the date `2026-11-14`."""
    named = identifiers(text)
    said = ' '.join(restated).lower()
    whole = identifiers(said)
    return sum(token in whole or (len(token) >= 4 and token in said) for token in named), len(named)


def _echoed(text, restated):
    """Whether a restatement of a unit that names no identifier shares a word with it, or None when the unit
    has no word to look for (five Latin letters or more). A unit in another script is restated in the
    wiki's language, so no word of it can be looked for: it is taken on the model's word and the caller
    records it."""
    words = {word.lower() for word in _WORD.findall(text)}
    if not words:
        return None
    return bool(words & {word.lower() for word in _WORD.findall(' '.join(restated))})


def _dropped(text, restated):
    """The identifiers and quotations of a unit that no item said to state it keeps, in reading order.

    A fact that keeps a note's date and drops its measurement counts as anchored; this names the
    measurement. A quotation is kept only word for word."""
    said = ' '.join(restated).lower()
    whole = identifiers(said)
    named = [token.strip('`.,;:()«»"\'').lower() for token in _IDENTIFIER.findall(text)]
    named = list(dict.fromkeys(token for token in named if token))
    missing = [token for token in named if token not in whole and not (len(token) >= 4 and token in said)]
    missing += ['«' + (first or second)[:40] + '»' for first, second in _QUOTATION.findall(text)
                if ' '.join((first or second).lower().split()) not in ' '.join(said.split())]
    return missing


def ledger(result, sources, shown, single=None, taken=None, place=None):
    """Read an answer's accounting against the units of its sources.

    `sources` maps each source (its operation id; `single` for the one source of a single-note answer) to
    its unit texts. `shown` maps the key of every registered fact and narrative to its text. `taken`, when
    given, holds the keys of the proposed items the proposal checks kept: an item they dropped states no
    unit, unless it restates a registered one. `place`, when given, says why a page named by an `elsewhere`
    entry is no such place (or None when it is one).

    Returns `(books, faults)`: per source its `total`, the units `stated` by its own proposed items, the
    `left` entries that hold (reason, units, items or where), the units still `open`, and three measures that
    refuse nothing: `unanchored` (stated by items that keep none of the unit's identifiers, or a unit with
    nothing the code could look for), `thin` (they keep fewer than half) and `dropped` (each such unit with
    the identifiers and quotations its items do not keep). `faults` says, in words for the model, which
    entries accounted for nothing.
    """
    result = result if isinstance(result, dict) else {}
    books = {source: {'total': len(texts), 'stated': set(), 'left': [], 'open': [], 'unanchored': [], 'thin': [],
                      'dropped': []}
             for source, texts in sources.items()}
    faults = []
    # The facts and narratives each source proposed, by key, and what was said about each of its units.
    proposed, said = {source: {} for source in sources}, {source: {} for source in sources}
    unchecked = {source: set() for source in sources}

    def owner(entry):
        # A single-note answer has one source whatever an entry says; a batch entry names its own.
        return single if single in sources else entry.get('source') if isinstance(entry.get('source'), str) else ''

    for kind, identity in ITEM_KINDS.items():
        for item in result.get(kind) if isinstance(result.get(kind), list) else ():
            if not isinstance(item, dict):
                continue
            key, source = item.get(identity), owner(item)
            words = ' '.join(str(item.get(field) or '') for field in ('text', 'title', 'summary'))
            if source in books and isinstance(key, str) and kind != 'pages':
                # A page holds facts and narratives; its own title is no place to keep a unit.
                proposed[source][key] = words
            if 'units' not in item:
                continue
            label = 'the ' + kind[:-1] + ' ' + str(key)[:80]
            if source not in books or not isinstance(key, str):
                # An item with no key of its own is the proposal checks' to refuse; it states nothing here.
                faults.append(label + ' names units of a source this request did not send')
                continue
            if taken is not None and key not in taken and key not in shown:
                faults.append(label + ' was not taken into the proposal, so it states no unit')
                continue
            good, whole = _numbers(item['units'], books[source]['total'])
            if not whole:
                faults.append(label + ' names units its source does not have; it has '
                              + str(books[source]['total']))
            books[source]['stated'].update(good)
            for number in good:
                said[source].setdefault(number, []).append(words)
    entries = result.get('left', [])
    if not isinstance(entries, list):
        faults.append('"left" must be a list')
        entries = []
    for position, entry in enumerate(entries, 1):
        label = 'left entry ' + str(position)
        if not isinstance(entry, dict) or entry.get('reason') not in REASONS:
            faults.append(label + ' needs a reason: ' + ', '.join(REASONS))
            continue
        source, reason = owner(entry), entry['reason']
        if source not in books:
            faults.append(label + ' names a source this request did not send')
            continue
        texts = sources[source]
        good, whole = _numbers(entry.get('units'), books[source]['total'])
        if not whole or not good:
            faults.append(label + ' must name units its source has, as numbers from 1 to '
                          + str(books[source]['total']))
            if not good:
                continue
        kept = {'reason': reason, 'units': sorted(set(good))}
        if reason in NAMING:
            items = entry.get('items')
            if not isinstance(items, list) or not items or not all(isinstance(key, str) for key in items):
                faults.append(label + ' (' + reason + ') must name in "items" the facts or narratives that '
                              + ('already state these units' if reason == 'stated' else 'these units support'))
                continue
            # Another note's proposal is no place to keep a unit: that note may publish later or not at all.
            unknown = [key for key in items if key not in shown and key not in proposed[source]]
            if unknown:
                faults.append(label + ' names items that are neither registered nor proposed for its own '
                              'source in this answer: ' + ', '.join(key[:80] for key in unknown[:5]))
                continue
            kept['items'] = list(dict.fromkeys(items))
            if reason == 'stated':
                # The text the answer proposes for a key is the one that will be published; the registered
                # one counts only for an item the answer leaves as it is.
                restated = [proposed[source].get(key) or shown.get(key) or '' for key in kept['items']]
                # A fact said to state a unit mentions something the unit names, or at least shares a word
                # with it; one that does neither states something else.
                loose = []
                for number in kept['units']:
                    held, named = _anchored(texts[number - 1], restated)
                    echoed = None if named else _echoed(texts[number - 1], restated)
                    if (named and not held) or echoed is False:
                        loose.append(number)
                    elif not named and echoed is None:
                        # Nothing of it could be looked for: accepted, and listed for a person to read.
                        unchecked[source].add(number)
                if loose:
                    wanted = sorted({token for number in loose for token in identifiers(texts[number - 1])})
                    faults.append(label + ' (stated): the items it names do not mention what unit'
                                  + ('s ' if len(loose) > 1 else ' ') + ', '.join(str(number) for number in loose)
                                  + (' name' if len(loose) > 1 else ' names')
                                  + (' (one of: ' + ', '.join(wanted[:8]) + ')' if wanted else '')
                                  + '; state the unit in an item, or name the fact that does say it')
                    kept['units'] = [number for number in kept['units'] if number not in loose]
                    if not kept['units']:
                        continue
                for number in kept['units']:
                    said[source].setdefault(number, []).extend(restated)
        elif reason == 'context':
            loud = [number for number in kept['units']
                    if identifiers(texts[number - 1]) or _QUOTED.search(texts[number - 1])]
            if loud:
                faults.append(label + ' (context): unit' + ('s ' if len(loud) > 1 else ' ')
                              + ', '.join(str(number) for number in loud) + ' name a number, a path, code or a '
                              'quotation, so they make a claim: state each in an item, or leave it as evidence '
                              'of the items it belongs to')
                kept['units'] = [number for number in kept['units'] if number not in loud]
                if not kept['units']:
                    continue
        else:
            where = ' '.join(entry['where'].split())[:WHERE_LIMIT] if isinstance(entry.get('where'), str) else ''
            wrong = 'names no page or project in "where"' if not where else place(where) if place else None
            if wrong:
                faults.append(label + ' (elsewhere) ' + wrong)
                continue
            kept['where'] = where
        books[source]['left'].append(kept)
    for source, book in books.items():
        left = {number for entry in book['left'] for number in entry['units']}
        book['open'] = [number for number in range(1, book['total'] + 1)
                        if number not in book['stated'] and number not in left]
        for number, restated in sorted(said[source].items()):
            if number in book['open']:
                continue
            kept, named = _anchored(sources[source][number - 1], restated)
            if (named and not kept) or number in unchecked[source]:
                book['unanchored'].append(number)
            elif named > 1 and kept * 2 < named:
                book['thin'].append(number)
            missing = _dropped(sources[source][number - 1], restated)
            if missing:
                book['dropped'].append([number, missing[:DROPPED_SHOWN]])
        book['stated'] = sorted(book['stated'])
    return books, faults


def _listed(numbers, texts):
    """Open units by number, each with the start of its text: the round that repairs them reads them again."""
    def shown(number):
        text = ' '.join(texts[number - 1].split())
        return str(number) + ' ("' + text[:SHOWN_TEXT] + ('…' if len(text) > SHOWN_TEXT else '') + '")'
    return (', '.join(shown(number) for number in numbers[:NAMED_UNITS])
            + (' and ' + str(len(numbers) - NAMED_UNITS) + ' more' if len(numbers) > NAMED_UNITS else ''))


def refusal(books, faults, sources, single=None):
    """The repair for an answer that leaves units unaccounted for, or None when every unit has its fate."""
    gaps = [(source, book['open']) for source, book in books.items() if book['open']]
    if not gaps:
        return None
    where = ' in no item\'s "units" and in no valid "left" entry'
    if single in books:
        head = 'these units of the source are' + where + ': ' + _listed(books[single]['open'], sources[single])
    else:
        head = ('units of ' + str(len(gaps)) + (' sources are' if len(gaps) > 1 else ' source are') + where + ': '
                + '; '.join('source ' + str(source) + ': ' + _listed(numbers, sources[source])
                            for source, numbers in gaps))
    tail = ('. State each of them in an item that names it in "units", or list it in "left" with its reason'
            ' (stated, evidence, context or elsewhere)')
    if faults:
        tail += '. Entries that accounted for nothing: ' + '; '.join(faults[:6])
    return head + tail


def record(book):
    """What an `editor_coverage` event keeps of one source's book."""
    counts = {reason: sum(len(entry['units']) for entry in book['left'] if entry['reason'] == reason)
              for reason in REASONS}
    return {'cut': CUT, 'units': book['total'], 'items': len(book['stated']), **counts,
            'open': list(book['open']), 'unanchored': list(book['unanchored']), 'thin': list(book['thin']),
            'dropped': [list(entry) for entry in book['dropped']],
            'left': [dict(entry) for entry in book['left']]}


def totals(records):
    """The sum of several notes' records, for a pass report."""
    measures = ('open', 'unanchored', 'thin', 'dropped')
    summed = {name: 0 for name in ('notes', 'units', 'items', *REASONS, *measures)}
    for kept in records:
        summed['notes'] += 1
        for name in ('units', 'items', *REASONS):
            summed[name] += kept.get(name) or 0
        for name in measures:
            summed[name] += len(kept.get(name) or ())
    return summed
