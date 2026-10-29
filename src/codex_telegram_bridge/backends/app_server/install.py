"""Hook plan for app-server mode; native service lifecycle is shared."""
from ...management.desktop_hook import registration_plan, register
from ...management.transaction import install_files
from ...management import services


def install(*, service=True):
    _command, changes = registration_plan()
    install_files(changes, service=service)
    print('Desktop Stop hook registered. Review and trust it using Codex /hooks; existing notify is preserved.')
    return 0


def uninstall():
    services.manage('uninstall')
    register(remove=True)
    return 0
