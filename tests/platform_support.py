"""Whether this host can build the runtime's isolation at all, asked of the runtime's own policy builder."""
import sys

from kelmo.runtime import IsolationError


def linux_isolation_unsupported():
    """Why the Linux runtime cannot isolate on this host's architecture (the policy builder's own refusal), or None:
    off Linux, and on a Linux host whose syscall policy builds. A case that needs a healthy runtime skips itself with
    this reason on such a host (an arm64 container on an arm Mac) and runs everywhere the runtime is deployed."""
    if not sys.platform.startswith('linux'):
        return None
    from kelmo.linux_runtime import seccomp_filter
    try:
        seccomp_filter()
    except IsolationError as error:
        return str(error)
    return None


UNSUPPORTED = linux_isolation_unsupported()
