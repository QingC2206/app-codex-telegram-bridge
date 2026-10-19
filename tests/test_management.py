"""Installer tests use temporary profiles and never modify OS services."""
import json
import importlib.util
import os
from pathlib import Path
import plistlib
import tempfile
import subprocess
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
        snapshot=patch.object(services,'snapshot',return_value={'original':'service'});snapshot.start();self.addCleanup(snapshot.stop)
        restore=patch.object(services,'restore');self.restore=restore.start();self.addCleanup(restore.stop)

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
        self.restore.assert_called_once_with({'original':'service'})

    def test_failed_restart_restores_existing_installation_record(self):
        install.install()
        manifest=self.data/'bridge-install.json';original=manifest.read_bytes()
        self.service.side_effect=[0,RuntimeError('restart failed')]
        with patch.object(install.sys,'platform','linux'):
            with self.assertRaisesRegex(RuntimeError,'restart failed'):install.install()
        self.assertEqual(manifest.read_bytes(),original)
        self.restore.assert_called_once()

    def test_hook_only_install_does_not_touch_service(self):
        install.install(service=False)
        self.service.assert_not_called()
        self.restore.assert_not_called()

    def test_uninstall_respects_new_notify_config_changed_by_user(self):
        install.install();config=self.profile/'config.toml';config.write_text('notify=["new-user-tool"]\n')
        install.uninstall()
        self.assertEqual(tomlkit.parse(config.read_text())['notify'],['new-user-tool'])


class ServiceDefinitionTests(unittest.TestCase):
    def test_schtasks_text_with_utf16_declaration_is_normalized(self):
        data=Path(tempfile.gettempdir()).resolve()/'robot-test'
        xml=services.windows_task(['python','-m','codex_telegram_bridge','--codex-home',str(data)])
        emitted=xml.decode('utf-16').encode('utf-8')
        with patch.object(services,'codex_home',return_value=data):
            self.assertTrue(services._legacy_owned(emitted))
        self.assertEqual(ET.tostring(ET.fromstring(services._task_xml(emitted))),ET.tostring(ET.fromstring(xml)))

    def test_task_xml_preserves_unicode_and_rejects_malformed_definitions(self):
        xml=services.windows_task(['C:/测试/python.exe','run'])
        for value in (xml,xml.decode('utf-16'),xml.decode('utf-16').encode('utf-8')):
            root=ET.fromstring(services._task_xml(value))
            self.assertEqual(next(x.text for x in root.iter() if x.tag.endswith('Command')),'C:/测试/python.exe')
        self.assertFalse(services._legacy_owned(b'<Task>broken'))

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


class ServiceRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        env=patch.dict(os.environ,CODEX_HOME=str(self.root/'codex'),CODEX_TELEGRAM_DATA_DIR=str(self.root/'data'),XDG_CONFIG_HOME=str(self.root/'config'))
        env.start();self.addCleanup(env.stop)
        home=patch.object(Path,'home',return_value=self.root);home.start();self.addCleanup(home.stop)

    def test_other_profiles_launch_agent_is_never_removed(self):
        path=self.root/'Library/LaunchAgents'/(services.identity()+'.plist')
        path.parent.mkdir(parents=True)
        path.write_bytes(plistlib.dumps({'ProgramArguments':['python','-m','codex_telegram_bridge','--data-dir','other']}))
        with patch.object(services.sys,'platform','darwin'),patch.object(services,'_run') as run:
            with self.assertRaisesRegex(RuntimeError,'not owned'):services.manage('uninstall')
        run.assert_not_called();self.assertTrue(path.exists())

    def test_linux_service_rollback_restores_content_and_prior_state(self):
        path=self.root/'config/systemd/user'/(services.identity()+'.service')
        path.parent.mkdir(parents=True)
        original=services.linux_unit(services.command()).encode();path.write_bytes(original)
        with patch.object(services.sys,'platform','linux'),patch.object(services,'_run',return_value=Mock(returncode=0)) as run:
            previous=services.snapshot();path.write_text('failed replacement')
            services.restore(previous)
        self.assertEqual(path.read_bytes(),original)
        commands=[call.args[0] for call in run.call_args_list]
        self.assertIn(['systemctl','--user','enable',path.name],commands)
        self.assertEqual(commands[-1],['systemctl','--user','start',path.name])

    def test_failed_first_install_removes_only_created_agent(self):
        path=self.root/'Library/LaunchAgents'/(services.identity()+'.plist')
        previous={'platform':'darwin','path':path,'content':None,'active':False,'domain':'gui/123','label':services.identity()}
        path.parent.mkdir(parents=True);path.write_bytes(b'partial')
        with patch.object(services,'_run') as run:services.restore(previous)
        self.assertFalse(path.exists());self.assertEqual(run.call_count,1)

    def test_windows_service_rollback_reimports_original_xml(self):
        original=services.windows_task(services.command()); imported=[]
        def run(argv,**kwargs):
            if '/XML' in argv:imported.append(Path(argv[argv.index('/XML')+1]).read_bytes())
            return Mock(returncode=0)
        with patch.object(services,'_run',side_effect=run),patch.object(services,'_stop_owned_processes'):
            services.restore({'platform':'win32','name':'test-owned-task','content':original})
        self.assertEqual(imported,[original])


class BootstrapTests(unittest.TestCase):
    def test_running_service_stops_before_pip_and_recovers_on_failure(self):
        spec=importlib.util.spec_from_file_location('bridge_bootstrap',Path(__file__).resolve().parents[1]/'install.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        calls=[]
        def run(argv,**kwargs):
            calls.append(argv)
            if 'pip' in argv:raise subprocess.CalledProcessError(1,argv)
            return Mock(returncode=0)
        with tempfile.TemporaryDirectory() as folder,patch.object(module.sys,'argv',['install.py','--codex-home',folder]), \
                patch.object(module.venv,'EnvBuilder'),patch.object(module.subprocess,'run',side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError):module.main()
        pip_index=next(i for i,c in enumerate(calls) if 'pip' in c)
        self.assertEqual(calls[pip_index-1][-2:],['service','stop'])
        self.assertEqual(calls[pip_index+1][-2:],['service','start'])
