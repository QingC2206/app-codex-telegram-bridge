"""Select platform process ownership and launch options for every backend."""
import os
import sys

from .posix.processes import PosixChildProcessJob

if os.name == 'nt':
    from .windows.processes import ChildProcessJob
else:
    ChildProcessJob = PosixChildProcessJob


def spawn_options(platform=None):
    platform = sys.platform if platform is None else platform
    if platform == 'win32':
        from .windows.processes import spawn_options as options
    else:
        from .posix.processes import spawn_options as options
    return options()
