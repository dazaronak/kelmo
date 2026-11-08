"""Seed a test vault with a complete registry in one publication.

Fixtures describe their knowledge in the plain shape tests find easy to write: `facts` and `notes`
maps (a fact names its target, text, status and note), optional `pages` entries kept as earlier
page data, and `session_applications`. `build` turns that description into the registry Kelmo
stores, with explicit page identities and layouts; `seed` publishes it through the ordinary
publisher. Seeding replaces the store's whole registry, which no product path does, so it lives
here and never in `src`.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping

from kelmo.config import Config
from kelmo.contracts import EvidenceRef
from kelmo.wiki import engine, pages

PAGE_SETTINGS = {"page_id", "layout", "aliases", "tags", "repositories", "links", "feature_id", "adopt_header"}
NEW_PAGE_SETTINGS = PAGE_SETTINGS | set(pages.ENTRY_FIELDS)
NO_BINDING = "No exact evidence binding was supplied."


def fact(key, target, text, note_id, archive_id, *, status="active", **extra):
    """One fact in the fixture shape, with the identity and revision the store derives."""
    value = dict(fact_key=key, target=target, text=text, status=status, expected_revision=None, replaces=[],
                 id=engine.digest(key)[:12], note_id=note_id, ark_id=archive_id, **extra)
    value["revision"] = pages.fact_revision(value)
    return value


def _source(note_id, receipt, supplied):
    if supplied is None:
        return {"state": "unresolved", "reason": NO_BINDING}
    try:
        reference = EvidenceRef.from_dict(supplied.to_dict() if isinstance(supplied, EvidenceRef) else supplied)
    except (TypeError, ValueError):
        return {"state": "unresolved", "reason": "The supplied evidence binding has an invalid shape."}
    if reference.archive_id != receipt["ark_id"] or reference.content_sha256 != note_id:
        return {"state": "unresolved", "reason": "The supplied archive identity or source hash differs."}
    return {"state": "exact", "evidence_ref": reference.to_dict()}


def _page(target, settings, original, header=None, enrolled=None):
    if not isinstance(settings, Mapping) or not {"page_id", "layout"} <= settings.keys():
        raise ValueError("page " + target + ": a seeded page needs a page_id and layout")
    lists = {}
    for name in ("aliases", "tags", "repositories", "links"):
        value = settings.get(name, ())
        if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
            raise ValueError("page " + target + ": " + name + " must be a list of strings")
        lists[name] = list(value)
    if lists["repositories"] and (enrolled is None or set(lists["repositories"]) - set(enrolled)):
        raise ValueError("page " + target + ": repository references must name configured enrollment")
    if set(settings) - (PAGE_SETTINGS if original is not None else NEW_PAGE_SETTINGS):
        raise ValueError("page " + target + ": seeding cannot replace existing page fields")
    layout = settings["layout"]
    if layout not in engine.V3_LAYOUTS:
        raise ValueError("page " + target + ": unknown layout")
    if original is not None:
        base = {name: original[name] for name in pages.ENTRY_FIELDS}
    else:
        kind, category = engine.V3_LAYOUTS[layout] or (settings.get("kind"), settings.get("category"))
        base = {"kind": settings.get("kind", kind), "category": settings.get("category", category),
                "parent": settings.get("parent", ""), "title": settings.get("title", ""),
                "summary": settings.get("summary", ""), "priority": settings.get("priority", "")}
    entry = dict(base, page_id=settings["page_id"], layout=layout, **lists,
                 previous_revision=original.get("revision") if original is not None else None,
                 compatibility={"legacy_fields": copy.deepcopy(original)} if original is not None else {})
    if header is not None:
        entry["compatibility"]["header"] = pages.header_record(header)
    if layout == "feature":
        entry["feature_id"] = settings.get("feature_id", settings["page_id"])
    entry["revision"] = engine.entity_revision(entry)
    return entry


def _navigation(entries, facts):
    """Links a page shows by default: index to detail pages, hubs to their project pages, maps to children."""
    ids = {target: entry["page_id"] for target, entry in entries.items()}
    derived = {target: set(entry["links"]) for target, entry in entries.items()}
    for value in facts.values():
        if value["status"] == "retired" or value["target"] not in ids:
            continue
        index = value.get("index_target")
        if index and index != value["target"]:
            derived[index].add(ids[value["target"]])
        parts = value["target"].split("/")
        hub = "projects/" + parts[1] + "/" + parts[1] + ".md" if len(parts) >= 3 and parts[0] == "projects" else None
        if hub in ids and hub != value["target"]:
            derived[hub].add(ids[value["target"]])
    declared = {target: set(links) for target, links in derived.items()}
    for target, entry in entries.items():
        if entry["layout"] == "feature":
            derived[target] |= {ids[other] for other, links in declared.items() if entry["page_id"] in links}
            if entry["parent"] in ids:
                derived[target].add(ids[entry["parent"]])
        elif entry["layout"] == "project-map":
            derived[target] |= {ids[other] for other, value in entries.items() if value["parent"] == target}
        derived[target].discard(entry["page_id"])
    for target, entry in entries.items():
        links = sorted(derived[target])
        if links != entry["links"]:
            entry["links"] = links
            entry["revision"] = engine.entity_revision(entry)


def build(spec, *, authority_id, evidence_bindings=None, pages_settings=None, protected_fact_keys=(),
          headers=None, repositories=None):
    """The stored registry for a fixture description; a supplied binding that matches its note is exact."""
    evidence_bindings = {} if evidence_bindings is None else evidence_bindings
    pages_settings = {} if pages_settings is None else pages_settings
    protected = set(protected_fact_keys)
    if protected - spec["facts"].keys():
        raise ValueError("A protected rule names no stored fact.")
    originals = spec.get("pages", {})
    if set(originals) - pages_settings.keys():
        raise ValueError("Every existing page needs an explicit page_id and layout.")
    headers = {} if headers is None else headers
    entries = {target: _page(target, settings, originals.get(target), headers.get(target), repositories)
               for target, settings in sorted(pages_settings.items())}
    shown = {value["target"] for value in spec["facts"].values()}
    shown |= {value["index_target"] for value in spec["facts"].values() if value.get("index_target")}
    missing = sorted(shown - entries.keys())
    if missing:
        raise ValueError("Every fact target and index page needs a registered page: " + ", ".join(missing[:10]))
    _navigation(entries, spec["facts"])
    resolutions = {note_id: _source(note_id, receipt, evidence_bindings.get(note_id))
                   for note_id, receipt in spec["notes"].items()}

    def evidence(note_id):
        resolution = resolutions[note_id]
        refs = [resolution["evidence_ref"]] if resolution["state"] == "exact" else []
        return {"evidence_resolution": resolution["state"], "evidence_refs": refs}

    notes = {}
    for note_id, receipt in spec["notes"].items():
        compatibility = {"legacy_fields": copy.deepcopy(receipt)}
        if resolutions[note_id]["state"] != "exact":
            compatibility["evidence_reason"] = resolutions[note_id]["reason"]
        notes[note_id] = {"filename": receipt["filename"], "ark_id": receipt["ark_id"], **evidence(note_id),
                          "compatibility": compatibility}
    person = {target for target, entry in entries.items()
              if entry["layout"] == "person" or entry["category"] == "you"} | {"person.md"}
    facts = {}
    for key, original in spec["facts"].items():
        value = {name: copy.deepcopy(original[name]) for name in engine.FIELDS | {"id", "note_id", "ark_id"}}
        for name in ("role", "index_target"):
            if name in original:
                value[name] = copy.deepcopy(original[name])
        value.update(owner="rule-policy" if key in protected else "legacy-import", protected=key in protected,
                     mandatory=key in protected or original["target"] in person, **evidence(original["note_id"]),
                     previous_revision=original["revision"], compatibility={"legacy_fields": copy.deepcopy(original)})
        value["revision"] = engine.entity_revision(value)
        facts[key] = value
    registry = {"version": engine.REGISTRY_VERSION, "authority_id": authority_id, "facts": facts, "notes": notes,
                "pages": entries, "actions": {}, "narratives": {}, "redirects": {}, "compatibility": {}}
    if "session_applications" in spec:
        registry["session_applications"] = copy.deepcopy(spec["session_applications"])
    return engine.validate_registry(registry)


def seed(root, spec=None, *, evidence_bindings=None, pages_settings=None, protected_fact_keys=(), contexts=None,
         title="chore: Seed fixture knowledge"):
    """Publish the fixture registry as the store's whole knowledge; returns the registry written."""
    config = Config.load(root)
    spec = {"facts": {}, "notes": {}} if spec is None else spec
    headers = {}
    for target, settings in (pages_settings or {}).items():
        if isinstance(settings, Mapping) and settings.get("adopt_header"):
            path = config.wiki / target
            headers[target] = pages.leading_header(path.read_bytes().decode("utf-8") if path.is_file() else "")
    repositories = tuple(contexts.repository_remotes) if contexts is not None else None
    registry = build(spec, authority_id=config.vault_id, evidence_bindings=evidence_bindings,
                     pages_settings=pages_settings, protected_fact_keys=protected_fact_keys, headers=headers,
                     repositories=repositories)
    return publish(root, registry, title)


def _restamp(config):
    """Make the committed manifest describe HEAD again after a fixture committed hand-written pages.

    A store's first generation is written at initialization; a fixture that then commits its own Markdown
    changes the tree that manifest's digest covers. The publisher refuses to extend a manifest that does not
    describe the committed tree, so the fixture's pages are folded into the current generation first.
    """
    import json

    root, _, _, relative = pages.generation_paths(config)
    raw = engine.git_blob(root, "HEAD:" + relative)
    if raw is None:
        return
    manifest = json.loads(raw)
    digest = pages._tree_digest(config, pages._knowledge_tree(config, "HEAD"))
    if manifest["managed_digest"] == digest:
        return
    manifest["managed_digest"] = digest
    engine.atomic_write(root / relative, engine.encoded(manifest))
    engine.git(root, "add", "--", relative)
    engine.git(root, "commit", "-q", "--no-verify", "-m", "test: Fold fixture pages into the generation", "--",
               relative)


def publish(root, registry, title="chore: Publish fixture knowledge", **options):
    """Commit a whole fixture registry through the ordinary publisher, bypassing only the ownership guard."""
    config = Config.load(root)
    _restamp(config)
    original = engine._preserve_metadata
    engine._preserve_metadata = lambda previous, proposed, **_: copy.deepcopy(proposed)
    try:
        # A non-None registration skips the claimed-file check: seeded pages are the fixture's own.
        engine._commit_registry(config.root, registry, title, config.wiki, _registration={}, **options)
    finally:
        engine._preserve_metadata = original
    return registry
