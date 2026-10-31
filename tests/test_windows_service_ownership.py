"""Scheduled-task ownership must bind exact profile/data and direct execution."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from codex_telegram_bridge.platforms.service import ServiceContext
from codex_telegram_bridge.platforms.windows import services


class WindowsServiceOwnershipTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.root=Path(folder.name).resolve()
        self.profile=self.root/'profile with spaces'
        self.data=self.root/'data with spaces'
        self.argv=['pythonw.exe','-m','codex_telegram_bridge','--codex-home',str(self.profile),
                   '--data-dir',str(self.data),'run','--quiet-start']

    def xml(self, argv=None):
        return services.task_definition(argv or self.argv,self.data)

    def test_exact_profile_and_data_are_both_required(self):
        xml=self.xml()
        self.assertTrue(services.is_owned(xml,self.profile,self.data))
        self.assertFalse(services.is_owned(xml,self.profile,self.root/'other-data'))
        self.assertFalse(services.is_owned(xml,self.root/'other-profile',self.data))
        prefix=self.argv.copy();prefix[4]=str(self.profile)+'-other'
        self.assertFalse(services.is_owned(self.xml(prefix),self.profile,self.data))

    def test_missing_duplicate_or_unqualified_scope_is_rejected(self):
        for tail in (['--codex-home',str(self.profile),'run'],
                     ['--codex-home',str(self.profile),'--codex-home',str(self.profile),
                      '--data-dir',str(self.data),'run'],
                     ['--codex-home','relative','--data-dir',str(self.data),'run'],
                     ['--codex-home',str(self.profile),'--data-dir',str(self.data),'notify']):
            with self.subTest(tail=tail):
                self.assertFalse(services.is_owned(self.xml(self.argv[:3]+tail),self.profile,self.data))

    def test_wrappers_other_modules_and_multiple_actions_are_not_owned(self):
        for argv in (['cmd.exe',*self.argv[1:]], ['pythonw.exe','-c','codex_telegram_bridge '+str(self.profile)],
                     ['pythonw.exe','-m','other_module',*self.argv[3:]]):
            with self.subTest(argv=argv):
                self.assertFalse(services.is_owned(self.xml(argv),self.profile,self.data))
        import xml.etree.ElementTree as ET
        root=ET.fromstring(self.xml());actions=next(n for n in root.iter() if n.tag.endswith('Actions'))
        actions.append(ET.fromstring(ET.tostring(actions[0])))
        self.assertFalse(services.is_owned(ET.tostring(root),self.profile,self.data))

    def test_legacy_script_requires_exact_path_and_service_arguments(self):
        argv=['pythonw.exe',str(self.data/'telegram_bot.py'),'--service']
        self.assertTrue(services.is_owned(self.xml(argv),self.profile,self.data))
        self.assertFalse(services.is_owned(self.xml(argv),self.profile,self.root/'other-data'))
        self.assertFalse(services.is_owned(self.xml(argv+['--arbitrary']),self.profile,self.data))

    def test_wrong_data_directory_rejects_every_management_action_before_mutation(self):
        runner=Mock(return_value=Mock(returncode=0,stdout=self.xml()))
        stop=Mock()
        context=ServiceContext(profile=self.profile,data=self.root/'other-data',argv=self.argv,
            label='isolated-task',run=runner,stop_owned_processes=stop,validate_definition=Mock())
        for action in ('status','install','start','stop','uninstall'):
            with self.subTest(action=action):
                runner.reset_mock()
                with self.assertRaisesRegex(RuntimeError,'not owned'):services.manage(context,action)
                self.assertEqual(runner.call_count,1)
                self.assertIn('/Query',runner.call_args.args[0])
                stop.assert_not_called()
        with self.assertRaisesRegex(RuntimeError,'not owned'):services.snapshot(context)


if __name__=='__main__':unittest.main()
