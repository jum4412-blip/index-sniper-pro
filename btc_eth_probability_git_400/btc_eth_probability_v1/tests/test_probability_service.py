import contextlib,io,json,os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from basic_core import service,runtime
from basic_core.cli import pause
from basic_core.core import SafetyError,atomic,fingerprint

ROOT=Path(__file__).resolve().parents[1]
TEST_UID=os.getuid()+10000
PERSON=SimpleNamespace(pw_name='ubuntu',pw_uid=TEST_UID,pw_gid=TEST_UID,pw_dir='/home/ubuntu')

class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT.parent);self.root=Path(self.temp.name)
        (self.root/'basic_core').mkdir();(self.root/'basic_core/__main__.py').write_text('# test runtime\n')
        (self.root/'config.json').write_text('{}\n');(self.root/'data').mkdir()
        (self.root/'probability_strategy.py').write_text('# synthetic strategy\n');(self.root/'deployment_model.json').write_text('{}\n')
        self.units=self.root/'units';self.units.mkdir()
        self.content=service.render_unit(self.root,'ubuntu','ubuntu','/home/ubuntu','/usr/bin/python3')
        self.spec={'root':self.root,'user':'ubuntu','uid':TEST_UID,'group':'ubuntu','home':Path('/home/ubuntu'),'python':Path('/usr/bin/python3').resolve(),'content':self.content}
    def effective_output(self,**changes):
        fields={'FragmentPath':str((self.units/service.SERVICE).resolve()),'DropInPaths':'','NeedDaemonReload':'no','LoadState':'loaded','User':'ubuntu','Group':'ubuntu','WorkingDirectory':str(self.root)}
        fields.update(changes)
        return ''.join(k+'='+v+'\n' for k,v in fields.items())
    def tearDown(self):self.temp.cleanup()
    def test_unit_has_continuous_recovery_and_security_without_arm_command(self):
        for line in ('User=ubuntu','Group=ubuntu','Wants=network-online.target','After=network-online.target',
                     'Restart=always','RestartSec=15','StartLimitIntervalSec=0','UMask=0077',
                     'NoNewPrivileges=true','PrivateTmp=true','ProtectSystem=full','WantedBy=multi-user.target'):
            self.assertIn(line,self.content.splitlines())
        self.assertIn(' -m basic_core live --root ',self.content)
        self.assertNotIn('start-live',self.content);self.assertNotIn('enable-live',self.content)
        self.assertNotIn('LIVE_ENABLED',self.content)
    def test_render_rejects_root_relative_and_systemd_expansion_paths(self):
        for kwargs in ({'root':'relative/path'},{'root':'/home/ubuntu/%n'},{'root':'/home/ubuntu/$SECRET'},{'user':'root'},{'user':'ubuntu\nExecStart=bad'}):
            params={'root':'/home/ubuntu/bot','user':'ubuntu','group':'ubuntu','home':'/home/ubuntu'};params.update(kwargs)
            with self.subTest(kwargs=kwargs),self.assertRaises(SafetyError):service.render_unit(**params)
    def test_installation_rejects_project_owned_by_another_user(self):
        person=SimpleNamespace(**{**PERSON.__dict__,'pw_dir':str(self.root.parent)})
        with patch('basic_core.service.pwd.getpwnam',return_value=person),patch('basic_core.service.grp.getgrgid',return_value=SimpleNamespace(gr_name='ubuntu')),patch('basic_core.service.os.geteuid',return_value=0):
            with self.assertRaisesRegex(SafetyError,'belong to the service user'):service.installation(self.root,'ubuntu')
    def test_preview_never_invokes_sudo_systemctl_or_handoff(self):
        run=Mock(side_effect=AssertionError('external command'))
        with patch('basic_core.service.installation',return_value=self.spec),patch('basic_core.service.check_handoff',side_effect=AssertionError('handoff during preview')),contextlib.redirect_stdout(io.StringIO()) as output:
            service.install(self.root,preview=True,run=run,unit_dir=self.units)
        self.assertEqual(output.getvalue(),self.content);run.assert_not_called()
    def test_install_enables_but_never_starts_or_stops_any_manager(self):
        run=Mock(return_value=SimpleNamespace(returncode=0,stdout=self.effective_output()))
        with patch('basic_core.service.installation',return_value=self.spec),patch('basic_core.service.check_handoff'),contextlib.redirect_stdout(io.StringIO()):
            service.install(self.root,run=run,unit_dir=self.units)
        commands=[call.args[0] for call in run.call_args_list]
        self.assertTrue(any('enable' in command for command in commands))
        self.assertTrue(any('daemon-reload' in command for command in commands))
        self.assertFalse(any(action in command for command in commands for action in ('start','restart','stop','disable')))
        self.assertFalse((self.root/'data/LIVE_ENABLED.json').exists())
    def test_background_handoff_refuses_until_flat_shutdown(self):
        with patch('basic_core.service.runtime.running',return_value=True):
            with self.assertRaisesRegex(SafetyError,'BACKGROUND_LIVE_MANAGER_RUNNING'):
                service.check_handoff(self.root,self.units/service.SERVICE)
    def test_matching_running_service_is_idempotent_without_file_replacement(self):
        unit=self.units/service.SERVICE;unit.write_text(self.content);before=unit.read_bytes()
        run=Mock(return_value=SimpleNamespace(returncode=0,stdout=self.effective_output()))
        with patch('basic_core.service.installation',return_value=self.spec),patch('basic_core.service.verify_unit',return_value=self.spec),patch('basic_core.service.runtime.running',return_value=True),patch('basic_core.service.service_owns_live',return_value=True),contextlib.redirect_stdout(io.StringIO()):
            service.install(self.root,run=run,unit_dir=self.units)
        self.assertEqual(unit.read_bytes(),before)
        self.assertFalse(any('install' in call.args[0] for call in run.call_args_list))
        self.assertFalse(any('start' in call.args[0] for call in run.call_args_list))
    def test_root_comment_cannot_mask_wrong_working_directory(self):
        unit=self.units/service.SERVICE;unit.write_text(self.content.replace('WorkingDirectory='+str(self.root),'WorkingDirectory="/home/ubuntu/other"'))
        real_stat=Path.stat
        def project_stat(path,*args,**kwargs):
            if path==self.root:return SimpleNamespace(st_uid=TEST_UID)
            return real_stat(path,*args,**kwargs)
        with patch('basic_core.service.pwd.getpwnam',return_value=PERSON),patch('basic_core.service.grp.getgrgid',return_value=SimpleNamespace(gr_name='ubuntu')),patch.object(Path,'stat',project_stat):
            with self.assertRaisesRegex(SafetyError,'working directory mismatch'):service.verify_unit(unit,self.root)
    def test_service_owner_must_match_existing_project_owner(self):
        unit=self.units/service.SERVICE;unit.write_text(self.content)
        with patch('basic_core.service.pwd.getpwnam',return_value=PERSON),patch('basic_core.service.grp.getgrgid',return_value=SimpleNamespace(gr_name='ubuntu')):
            with self.assertRaisesRegex(SafetyError,'owner does not match'):service.verify_unit(unit,self.root)
    def test_service_main_pid_must_have_exact_project_and_command(self):
        proc=self.root/'proc'/'1234';proc.mkdir(parents=True);(proc/'cwd').symlink_to(self.root,target_is_directory=True)
        (proc/'cmdline').write_bytes(('python3\0-m\0basic_core\0live\0--root\0'+str(self.root)+'\0').encode())
        run=Mock(return_value=SimpleNamespace(returncode=0,stdout='1234\n'))
        self.assertTrue(service.service_owns_live(self.root,run=run,proc_root=self.root/'proc'))
        (proc/'cmdline').write_bytes(b'python3\0-m\0basic_core\0live\0--root\0/home/ubuntu/other\0')
        self.assertFalse(service.service_owns_live(self.root,run=run,proc_root=self.root/'proc'))
    def test_same_code_arm_and_position_records_survive_service_install(self):
        digest=fingerprint(self.root);atomic(self.root/'data/LIVE_ENABLED.json',{'fingerprint':digest})
        db=self.root/'data/live.sqlite';db.write_bytes(b'preserved-position-and-pending-state')
        run=Mock(return_value=SimpleNamespace(returncode=0,stdout=self.effective_output()))
        with patch('basic_core.service.installation',return_value=self.spec),patch('basic_core.service.check_handoff'),contextlib.redirect_stdout(io.StringIO()):
            service.install(self.root,run=run,unit_dir=self.units)
        self.assertTrue(runtime.authorized(self.root,digest));self.assertEqual(db.read_bytes(),b'preserved-position-and-pending-state')
    def test_changed_code_disables_old_arm_without_deleting_recovery_state(self):
        digest=fingerprint(self.root);atomic(self.root/'data/LIVE_ENABLED.json',{'fingerprint':digest})
        (self.root/'basic_core/__main__.py').write_text('# changed code\n')
        self.assertFalse(runtime.authorized(self.root,digest));self.assertFalse(runtime.authorized(self.root,fingerprint(self.root)))
        self.assertTrue((self.root/'data/LIVE_ENABLED.json').exists())
    def test_pause_persists_across_supervisor_restarts(self):
        digest=fingerprint(self.root);atomic(self.root/'data/LIVE_ENABLED.json',{'fingerprint':digest})
        with contextlib.redirect_stdout(io.StringIO()):pause(self.root,'live')
        self.assertFalse(runtime.authorized(self.root,digest))
        self.assertNotIn('enable-live',self.content)
        self.assertFalse((self.root/'data/LIVE_ENABLED.json').exists())
    def test_effective_unit_rejects_overrides_wrong_fragment_and_stale_load(self):
        cases=[{'DropInPaths':'/etc/systemd/system/service.d/override.conf'},
               {'FragmentPath':'/run/systemd/system/other.service'},
               {'NeedDaemonReload':'yes'},{'NeedDaemonReload':'unknown'},
               {'User':'root'},{'Group':'other'},{'WorkingDirectory':'/home/ubuntu/other'},
               {'LoadState':'not-found'}]
        for changes in cases:
            run=Mock(return_value=SimpleNamespace(returncode=0,stdout=self.effective_output(**changes)))
            with self.subTest(changes=changes),self.assertRaises(SafetyError):
                service.verify_effective_unit(self.units/service.SERVICE,self.root,self.spec,run)
        run=Mock(return_value=SimpleNamespace(returncode=0,stdout=self.effective_output()))
        self.assertEqual(service.verify_effective_unit(self.units/service.SERVICE,self.root,self.spec,run)['NeedDaemonReload'],'no')
    def test_effective_unit_missing_duplicate_or_unavailable_schema_fails_closed(self):
        for code,output in ((0,'FragmentPath=x\n'),(0,self.effective_output()+'User=ubuntu\n'),(1,'')):
            run=Mock(return_value=SimpleNamespace(returncode=code,stdout=output))
            with self.subTest(code=code,output=output),self.assertRaises(SafetyError):
                service.verify_effective_unit(self.units/service.SERVICE,self.root,self.spec,run)
    def test_unloaded_disk_override_blocks_install_handoff(self):
        directory=self.units/(service.SERVICE+'.d');directory.mkdir();(directory/'override.conf').write_text('[Service]\nUser=root\n')
        with self.assertRaisesRegex(SafetyError,'SERVICE_DROP_INS_PRESENT'):
            service.reject_disk_dropins(self.units/service.SERVICE,search_roots=[self.units])
    def test_global_override_found_after_reload_prevents_enable(self):
        run=Mock(return_value=SimpleNamespace(returncode=0,stdout=self.effective_output(DropInPaths='/etc/systemd/system/service.d/global.conf')))
        with patch('basic_core.service.installation',return_value=self.spec),patch('basic_core.service.check_handoff'),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(SafetyError,'SERVICE_DROP_INS_PRESENT'):
                service.install(self.root,run=run,unit_dir=self.units)
        commands=[call.args[0] for call in run.call_args_list]
        self.assertTrue(any('daemon-reload' in command for command in commands))
        self.assertFalse(any(action in command for command in commands for action in ('enable','start','restart','stop')))
    def test_existing_stale_loaded_unit_blocks_before_any_mutation(self):
        unit=self.units/service.SERVICE;unit.write_text(self.content)
        run=Mock(return_value=SimpleNamespace(returncode=0,stdout=self.effective_output(NeedDaemonReload='yes')))
        with patch('basic_core.service.installation',return_value=self.spec),patch('basic_core.service.verify_unit',return_value=self.spec):
            with self.assertRaisesRegex(SafetyError,'SERVICE_DAEMON_RELOAD_REQUIRED'):
                service.install(self.root,run=run,unit_dir=self.units)
        self.assertTrue(all(call.args[0][:2]==['systemctl','show'] for call in run.call_args_list))
    def test_cli_launch_cannot_start_unit_with_effective_override(self):
        from basic_core.cli import launch
        (self.units/service.SERVICE).write_text(self.content)
        path_factory=lambda path:self.units if str(path)=='/etc/systemd/system' else Path(path)
        with patch('basic_core.cli.Path',side_effect=path_factory),patch('basic_core.service.verify_unit',return_value=self.spec),patch('basic_core.service.verify_effective_unit',side_effect=SafetyError('SERVICE_DROP_INS_PRESENT')),patch('basic_core.cli.subprocess.run') as run:
            with self.assertRaisesRegex(SafetyError,'SERVICE_DROP_INS_PRESENT'):launch(self.root)
        run.assert_not_called()
    def test_selected_python_version_boundary(self):
        for version,allowed in (('3.9\n',False),('3.10\n',True),('3.12\n',True),('not-a-version',False)):
            run=Mock(return_value=SimpleNamespace(returncode=0,stdout=version))
            with self.subTest(version=version):
                if allowed:service.verify_python('/usr/bin/python3',run)
                else:
                    with self.assertRaises(SafetyError):service.verify_python('/usr/bin/python3',run)

if __name__=='__main__':unittest.main()
