"""Plan command-handler ownership without deleting a mixed matcher group."""
from copy import deepcopy
import os
import shlex


def direct_command_arguments(command, *, windows=None):
    """Parse installer-generated direct commands; unknown shell forms stay foreign.

    Windows commands use list2cmdline quoting, whose path backslashes must not
    be treated as POSIX escapes. Embedded quotes/shell wrappers are not emitted
    by either installer and are deliberately not inferred as owned commands.
    """
    windows = os.name == 'nt' if windows is None else windows
    try:
        args = shlex.split(command, posix=not windows)
    except ValueError:
        return None
    if windows:
        args = [arg[1:-1] if arg.startswith('"') and arg.endswith('"') else arg for arg in args]
        if any('"' in arg for arg in args):
            return None
    return args


def partition_command_handlers(entries, owns_command):
    """Return retained and owned groups, preserving each group's metadata.

    Ownership applies to an individual command handler. Other handlers, including
    MCP hooks, never inherit ownership from a sibling in the same matcher group.
    The input is unchanged so installers can keep an exact rollback snapshot.
    """
    retained, owned = [], []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('hooks', []), list):
            raise ValueError('Each hook group must contain a hooks array.')
        keep, remove = [], []
        for handler in entry.get('hooks', []):
            if not isinstance(handler, dict):
                raise ValueError('Each hook handler must be an object.')
            command = handler.get('command')
            ours = (handler.get('type', 'command') == 'command'
                    and isinstance(command, str) and owns_command(command))
            (remove if ours else keep).append(deepcopy(handler))
        if keep or not remove:
            retained.append({**deepcopy(entry), 'hooks': keep})
        if remove:
            owned.append({**deepcopy(entry), 'hooks': remove})
    return retained, owned
