"""Initialize new user vaults with a current registry and one selected project."""
import re
from pathlib import Path

from .config import Config
from .errors import KelmoError


def validate(project, git_name=None, git_email=None):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", project):
        raise KelmoError("E_INPUT", "Project must be a lowercase slug of 3 to 64 characters.")
    if bool(git_name) != bool(git_email):
        raise KelmoError("E_INPUT", "Supply --git-name and --git-email together.")
    if git_name and any("\n" in value or "\0" in value for value in (git_name, git_email)):
        raise KelmoError("E_INPUT", "Git identity must be single-line text.")


def initialize(root, label, *, project="default", wiki_root=None, git_name=None, git_email=None):
    from .service import Service
    from .wiki import engine
    validate(project, git_name, git_email)
    root = Path(root).expanduser().resolve()
    if (root / "kelmo.toml").exists():
        # Answered from the configuration alone: on a shared server this process is not the archive
        # writer, so no Service is opened. A separate wiki is re-activated; a different label is
        # reported as retained, never applied.
        config = Config.load(root)
        if wiki_root is not None and Path(wiki_root).expanduser().resolve() not in (config.wiki,
                                                                                    config.knowledge_git_root):
            raise KelmoError("E_CONFIG", "The initialized vault uses a different wiki directory.")
        if config.wiki != root / "knowledge/wiki":
            engine.activate_wiki(config.wiki, config.vault_id)
        result = dict(initialized=True, existing=True, **config.as_dict())
        if label != config.label:
            result["warnings"] = ["Existing node label retained; initialization does not rename a vault."]
        return result
    identity = {"name": git_name, "email": git_email} if git_name else None
    result = Service.initialize(root, label, wiki_root=wiki_root, git_identity=identity, project=project)
    return dict(result, project=project, project_page=f"projects/{project}/{project}.md", registry="current")
