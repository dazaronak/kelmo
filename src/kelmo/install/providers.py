"""Translate installation choices into the existing executor command contract."""
import json
from pathlib import Path

from ..daemon import validate_argv
from ..errors import KelmoError


def arguments(args, executable, *, check_provider=False):
    from ..executor import provider_path
    provider, model = args.provider, args.model
    pinned = getattr(args, "provider_executable", None)
    custom = getattr(args, "provider_command_json", None)
    if args.executor_config and (provider or model or pinned or custom):
        raise KelmoError("E_CONFIG", "Select provider options or an executor configuration, not both.")
    if provider is None:
        if model or pinned or custom:
            raise KelmoError("E_CONFIG", "Select a provider before its model or command.")
        return None
    if provider not in ("claude", "codex", "custom"):
        raise KelmoError("E_CONFIG", "Select claude, codex or custom.")
    # An editing round over a large context can run for several minutes on a small model; the wrapper
    # in the executor profile gets the daemon's 600 s ceiling and the provider 40 s less for cleanup.
    command = [str(executable), "executor", "--provider", provider, "--timeout", "560"]
    if model:
        command += ["--model", model]
    if provider == "custom":
        if bool(pinned) == bool(custom):
            raise KelmoError("E_CONFIG", "Custom setup needs one provider-executable or provider-command-json.")
        try:
            argv = [str(pinned)] if pinned else json.loads(custom)
        except ValueError as error:
            raise KelmoError("E_CONFIG", "Custom provider command must be a JSON argv array.") from error
        validate_argv(argv, "E_CONFIG")
        if check_provider:
            provider_path(provider, argv[0])
        command += ["--command-json", json.dumps(argv)]
    else:
        if not model or custom:
            raise KelmoError("E_CONFIG", "Native providers need an explicit model and no custom command.")
        if check_provider:
            pinned = provider_path(provider, str(pinned)) if pinned else provider_path(provider)
        if pinned:
            command += ["--provider-executable", str(pinned)]
    home = getattr(args, "provider_home", None)
    if home is not None:
        if provider == "custom":
            raise KelmoError("E_CONFIG", "A custom provider selects its own account; provider-home applies to "
                                         "claude and codex.")
        home = Path(home).expanduser().resolve()
        if not home.is_dir():
            raise KelmoError("E_CONFIG", "The provider home must be an existing directory.")
        command += ["--provider-home", str(home)]
    if getattr(args, "effort", None):
        command += ["--effort", args.effort]
    return command
