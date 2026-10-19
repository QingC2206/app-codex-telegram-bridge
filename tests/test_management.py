"""Installer tests use temporary profiles and never modify OS services."""
import json
import os
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch,Mock
import xml.etree.ElementTree as ET
import tomlkit

from codex_telegram_bridge.management import install,services


class InstallTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        self.profile=self.root/'profile';self.profile.mkdir();self.data=self.root/'data';self.data.mkdir()
        env=patch.dict(os.environ,CODEX_HOME=str(self.profile),CODEX_TELEGRAM_DATA_DIR=str(self.data));env.start();self.addCleanup(env.stop)
        from codex_telegram_bridge.codex import desktop
        desktop_patch=patch.object(desktop,'discover_desktop',return_value=Mock());desktop_patch.start();self.addCleanup(desktop_patch.stop)
        service=patch.object(services,'manage');self.service=service.start();self.addCleanup(service.stop)

    def test_install_and_uninstall_preserve_unrelated_settings_and_data(self):
        config=self.profile/'config.toml';config.write_text('# keep\nmodel = "example"\n')
        hooks=self.profile/'hooks.json';other={'matcher':'other','hooks':[{'type':'command','command':'other-tool'}]}
        hooks.write_text(json.dumps({'hooks':{'PreToolUse':[other]},'custom':True}))
        credential=self.data/'telegram_notify.credentials.json';credential.write_text('{}')
        self.assertEqual(install.install(),0)
        self.assertEqual(tomlkit.parse(config.read_text())['model'],'example')
        self.assertIn('# keep',config.read_text())
        self.assertEqual(json.loads(hooks.read_text())['hooks']['PreToolUse'][0],other)
        self.assertEqual(install.uninstall(),0)
        self.assertNotIn('notify',tomlkit.parse(config.read_text()))
        self.assertEqual(json.loads(hooks.read_text())['hooks']['PreToolUse'],[other])
        self.assertTrue(credential.exists())

    def test_foreign_notify_requires_explicit_replacement_and_restores(self):
        config=self.profile/'config.toml';config.write_text('notify = ["another-tool", "arg"]\n')
        with self.assertRaisesRegex(RuntimeError,'Another notify'):install.install()
        self.service.assert_not_called()
        install.install(replace_notify=True);install.uninstall()
        self.assertEqual(tomlkit.parse(config.read_text())['notify'],['another-tool','arg'])

    def test_reinstall_does_not_duplicate_hooks(self):
        install.install();install.install()
        hooks=json.loads((self.profile/'hooks.json').read_text())
        self.assertEqual(len(hooks['hooks']['PreToolUse']),1)

    def test_corrupt_hooks_do_not_modify_config(self):
        config=self.profile/'config.toml';config.write_text('# untouched\n')
        (self.profile/'hooks.json').write_text('bad json')
        with self.assertRaises(ValueError):install.install()
        self.assertEqual(config.read_text(),'# untouched\n')

    def test_failed_registration_restores_original_config(self):
        config=self.profile/'config.toml';config.write_text('# original\n')
        self.service.side_effect=RuntimeError('registration failed')
        with self.assertRaisesRegex(RuntimeError,'registration failed'):install.install()
        self.assertEqual(config.read_text(),'# original\n')
        self.assertFalse((self.profile/'hooks.json').exists())

    def test_uninstall_respects_new_notify_config_changed_by_user(self):
        install.install();config=self.profile/'config.toml';config.write_text('notify=["new-user-tool"]\n')
        install.uninstall()
        self.assertEqual(tomlkit.parse(config.read_text())['notify'],['new-user-tool'])


class ServiceDefinitionTests(unittest.TestCase):
    def test_launch_agent_has_exact_argument_boundaries(self):
        argv=['/a space/python','-m','codex_telegram_bridge','--data-dir','/a "quoted" dir','run']
        definition=plistlib.loads(services.macos_agent(argv,'label',Path('/tmp/data')))
        self.assertEqual(definition['ProgramArguments'],argv)
        self.assertEqual(definition['LimitLoadToSessionType'],'Aqua')

    def test_systemd_quotes_percent_dollar_and_spaces(self):
        unit=services.linux_unit(['/a space/python','100%','$HOME','run'])
        self.assertIn('"/a space/python" "100%%" "$$HOME" "run"',unit)
        self.assertIn('KillMode=control-group',unit)
        with self.assertRaises(ValueError):services.linux_unit(['x\nExecStart=bad'])

    def test_windows_task_is_xml_not_a_shell_script(self):
        root=ET.fromstring(services.windows_task(['C:\\a space\\pythonw.exe','-m','codex_telegram_bridge','run']))
        values={x.tag.split('}')[-1]:x.text for x in root.iter()}
        self.assertEqual(values['Command'],'C:\\a space\\pythonw.exe')
        self.assertEqual(values['LogonType'],'InteractiveToken')
        self.assertEqual(values['MultipleInstancesPolicy'],'IgnoreNew')
