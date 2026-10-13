from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

APP = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('git') and shutil.which('bash'), 'git/bash required')
class DeployTests(unittest.TestCase):
    def test_git_archive_does_not_touch_live_branch_or_dirty_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); repo=root/'repo'; repo.mkdir(); home=root/'home'; home.mkdir()
            env=dict(os.environ,HOME=str(home),GIT_CONFIG_NOSYSTEM='1',GIT_AUTHOR_NAME='Fixture',GIT_AUTHOR_EMAIL='fixture@example.invalid',
                     GIT_COMMITTER_NAME='Fixture',GIT_COMMITTER_EMAIL='fixture@example.invalid')
            def git(*args):
                return subprocess.check_output(['git','-C',str(repo),*args],env=env,stderr=subprocess.DEVNULL,text=True).strip()
            git('init','-b','main')
            (repo/'live_state.json').write_text('{"position":"DO_NOT_TOUCH"}')
            git('add','.'); git('commit','-m','live baseline'); old=git('rev-parse','HEAD')
            folder=repo/'trading_lab_ec2'; folder.mkdir()
            # Fixture installer: verifies extraction only; never invokes sudo/systemd.
            (folder/'install.sh').write_text('#!/usr/bin/env bash\nset -eu\nprintf "%s" "$1" > "$HOME/installed_commit"\n')
            git('add','.'); git('commit','-m','new observer payload'); new=git('rev-parse','HEAD')
            git('update-ref','refs/remotes/origin/main',new)
            git('checkout','-b','live-branch',old)
            (repo/'live_state.json').write_text('{"position":"LATEST_UNCOMMITTED"}')
            before=git('status','--porcelain')
            result=subprocess.run(['bash',str(APP/'deploy.sh'),str(repo),'origin/main'],env=env,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual((home/'installed_commit').read_text(),new)
            self.assertEqual(git('rev-parse','HEAD'),old)
            self.assertEqual(git('branch','--show-current'),'live-branch')
            self.assertEqual(git('status','--porcelain'),before)
            self.assertEqual((repo/'live_state.json').read_text(),'{"position":"LATEST_UNCOMMITTED"}')
            self.assertFalse((repo/'trading_lab_ec2').exists())

    def test_wrong_directory_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as temp:
            result=subprocess.run(['bash',str(APP/'deploy.sh'),temp],text=True,capture_output=True)
            self.assertEqual(result.returncode,2)
            self.assertIn('not a Git repository',result.stderr)

    def test_shell_syntax(self):
        for path in APP.glob('*.sh'):
            subprocess.run(['bash','-n',str(path)],check=True)

    def test_units_only_this_observer(self):
        collector=(APP/'templates/lab1030-observer.service').read_text()
        self.assertIn('Restart=on-failure',collector)
        self.assertIn('WatchdogSec=180',collector)
        self.assertIn('ReadWritePaths=@@ROOT@@/shared',collector)
        self.assertNotIn('cci-chop-live',collector)
        self.assertNotIn('BITGET_API',collector)


if __name__ == '__main__': unittest.main()
