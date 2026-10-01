"""Render/install the opt-in EC2 supervisor without arming or starting trading."""
import argparse,grp,json,os,pwd,re,stat,subprocess,sys,tempfile
from pathlib import Path
from .core import SafetyError
from . import runtime

SERVICE='btc-eth-probability-v1.service'
UNIT_DIR=Path('/etc/systemd/system')

def clean_absolute(path,label):
    path=Path(path).expanduser()
    if not path.is_absolute():raise SafetyError(label+' must be absolute')
    value=str(path)
    if any(ord(ch)<32 or ord(ch)==127 for ch in value) or any(ch in value for ch in ('%','$')):
        raise SafetyError(label+' contains unsupported systemd path characters')
    return path.resolve()

def quoted(value):
    return '"'+str(value).replace('\\','\\\\').replace('"','\\"')+'"'

def verify_python(executable,run=subprocess.run):
    try:
        result=run([str(executable),'-I','-c','import sys; print(str(sys.version_info[0])+"."+str(sys.version_info[1]))'],capture_output=True,text=True,check=False,timeout=10)
    except (OSError,subprocess.TimeoutExpired):raise SafetyError('SERVICE_PYTHON_VERSION_UNAVAILABLE') from None
    version=re.fullmatch(r'(\d+)\.(\d+)\s*',result.stdout or '')
    if result.returncode!=0 or version is None:raise SafetyError('SERVICE_PYTHON_VERSION_UNAVAILABLE')
    if tuple(map(int,version.groups()))<(3,10):raise SafetyError('SERVICE_PYTHON_3_10_OR_NEWER_REQUIRED')

def render_unit(root,user,group,home,python_executable='/usr/bin/python3'):
    """Pure rendering for review. Filesystem/user checks happen before install."""
    root=clean_absolute(root,'project root');home=clean_absolute(home,'service home')
    python_executable=clean_absolute(python_executable,'python executable')
    if not all(re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',x) for x in (user,group)) or user=='root' or group=='root':
        raise SafetyError('a named non-root service user and group are required')
    return '\n'.join([
        '# Managed by BTC/ETH Probability installer; installation never starts trading.',
        '# probability-root: '+str(root),
        '[Unit]',
        'Description=BTC ETH Price Volume Probability manager (5x)',
        'Wants=network-online.target',
        'After=network-online.target',
        'StartLimitIntervalSec=0',
        '',
        '[Service]',
        'Type=simple',
        'User='+user,
        'Group='+group,
        'WorkingDirectory='+str(root),
        'Environment='+quoted('HOME='+str(home)),
        'Environment=PYTHONUNBUFFERED=1',
        'Environment=PYTHONDONTWRITEBYTECODE=1',
        'ExecStart='+quoted(python_executable)+' -m basic_core live --root '+quoted(root),
        'Restart=always',
        'RestartSec=15',
        'UMask=0077',
        'NoNewPrivileges=true',
        'PrivateTmp=true',
        'ProtectSystem=full',
        'KillSignal=SIGTERM',
        'KillMode=control-group',
        'TimeoutStopSec=120',
        '',
        '[Install]',
        'WantedBy=multi-user.target',
        '',
    ])

def installation(root,user=None,python_executable='/usr/bin/python3'):
    root=clean_absolute(root,'project root')
    if root==Path('/') or root.is_relative_to('/tmp') or root.is_relative_to('/var/tmp'):
        raise SafetyError('install under the service user home, outside temporary directories')
    if user is None:
        user=os.environ.get('SUDO_USER') or pwd.getpwuid(os.getuid()).pw_name
    try:person=pwd.getpwnam(user);group=grp.getgrgid(person.pw_gid).gr_name
    except KeyError:raise SafetyError('service user/group does not exist') from None
    if person.pw_uid==0:raise SafetyError('root service execution is prohibited; use --user ubuntu')
    if os.geteuid() not in (0,person.pw_uid):raise SafetyError('run installation as the service owner or root')
    home=clean_absolute(person.pw_dir,'service home')
    if not root.is_relative_to(home):raise SafetyError('project must be inside the service user home')
    required=[root,root/'config.json',root/'basic_core',root/'basic_core/__main__.py']
    for path in required:
        if not path.exists() or not path.resolve().is_relative_to(root):raise SafetyError('incomplete or escaping project installation')
        st=path.stat()
        if st.st_uid!=person.pw_uid:raise SafetyError('project files must belong to the service user')
        if st.st_mode & stat.S_IWOTH:raise SafetyError('world-writable project path is not supported')
    executable=clean_absolute(python_executable,'python executable')
    if not executable.is_file() or not os.access(executable,os.X_OK):raise SafetyError('python executable is unavailable')
    verify_python(executable)
    content=render_unit(root,user,group,home,executable)
    return {'root':root,'user':user,'uid':person.pw_uid,'group':group,'home':home,'python':executable,'content':content}

def verify_unit(path,root):
    """The root comment alone is not authority to operate an unrelated unit."""
    path=Path(path);root=Path(root).resolve()
    if path.is_symlink():raise SafetyError('service unit symlink requires manual review')
    content=path.read_text();lines=content.splitlines()
    if '# probability-root: '+str(root) not in lines:raise SafetyError('service belongs to another installation')
    values={}
    for line in lines:
        if line and not line.startswith(('#','[')) and '=' in line:
            k,v=line.split('=',1);values.setdefault(k,[]).append(v)
    if len(values.get('User',[]))!=1 or len(values.get('Group',[]))!=1 or values['User'][0]=='root':raise SafetyError('invalid service owner')
    try:
        person=pwd.getpwnam(values['User'][0]);group=grp.getgrgid(person.pw_gid).gr_name
    except KeyError:raise SafetyError('unknown service owner') from None
    if person.pw_uid==0 or values['Group'][0]!=group or root.stat().st_uid!=person.pw_uid:
        raise SafetyError('service owner does not match project owner')
    if values.get('WorkingDirectory')!=[str(root)]:raise SafetyError('service working directory mismatch')
    command=values.get('ExecStart',[])
    if len(command)!=1:raise SafetyError('invalid service command')
    suffix=' -m basic_core live --root '+quoted(root)
    if not command[0].endswith(suffix):raise SafetyError('service command mismatch')
    executable=command[0][:-len(suffix)]
    if not executable.startswith('"') or not executable.endswith('"'):raise SafetyError('unrecognized service interpreter')
    executable=executable[1:-1]
    if '\\' in executable or '"' in executable:raise SafetyError('unrecognized service interpreter')
    expected=render_unit(root,person.pw_name,group,person.pw_dir,executable)
    if content!=expected:raise SafetyError('service unit differs from the reviewed template')
    return {'user':person.pw_name,'uid':person.pw_uid,'group':group,'content':content}

def reject_disk_dropins(path,search_roots=None):
    path=Path(path)
    roots=search_roots if search_roots is not None else [path.parent,Path('/etc/systemd/system'),Path('/run/systemd/system'),Path('/usr/local/lib/systemd/system'),Path('/usr/lib/systemd/system'),Path('/lib/systemd/system')]
    for root in dict.fromkeys(Path(p) for p in roots):
        directory=root/(SERVICE+'.d')
        if directory.is_symlink():raise SafetyError('SERVICE_DROP_INS_PRESENT: review existing overrides before installation')
        if directory.exists():
            if not directory.is_dir() or any(p.name.endswith('.conf') for p in directory.iterdir()):
                raise SafetyError('SERVICE_DROP_INS_PRESENT: review existing overrides before installation')

def verify_effective_unit(path,root,expected,run=subprocess.run):
    """Inspect the loaded definition: disk text cannot reveal every override."""
    keys={'FragmentPath','DropInPaths','NeedDaemonReload','LoadState','User','Group','WorkingDirectory'}
    result=run(['systemctl','show',SERVICE,'--property='+','.join(sorted(keys)),'--no-pager'],capture_output=True,text=True,check=False)
    if result.returncode!=0 or not isinstance(result.stdout,str):raise SafetyError('SERVICE_EFFECTIVE_STATE_UNAVAILABLE')
    values={}
    for line in result.stdout.splitlines():
        if not line:continue
        key,separator,value=line.partition('=')
        if not separator or key not in keys or key in values:raise SafetyError('SERVICE_EFFECTIVE_STATE_INVALID')
        values[key]=value
    if set(values)!=keys:raise SafetyError('SERVICE_EFFECTIVE_STATE_INCOMPLETE')
    if values['LoadState']!='loaded':raise SafetyError('SERVICE_NOT_LOADED')
    if values['FragmentPath']!=str(Path(path).resolve()):raise SafetyError('SERVICE_FRAGMENT_MISMATCH')
    if values['DropInPaths']:raise SafetyError('SERVICE_DROP_INS_PRESENT: effective overrides require manual review')
    if values['NeedDaemonReload']!='no':raise SafetyError('SERVICE_DAEMON_RELOAD_REQUIRED: review and reload the unit before operating it')
    if values['User']!=expected['user'] or values['Group']!=expected['group'] or values['WorkingDirectory']!=str(Path(root).resolve()):
        raise SafetyError('SERVICE_EFFECTIVE_OWNER_OR_DIRECTORY_MISMATCH')
    return values

def service_owns_live(root,run=subprocess.run,proc_root='/proc'):
    result=run(['systemctl','show',SERVICE,'--property=MainPID','--value'],capture_output=True,text=True,check=False)
    try:pid=int(result.stdout.strip())
    except (TypeError,ValueError):return False
    if result.returncode or pid<=0:return False
    proc=Path(proc_root)/str(pid)
    try:
        args=[x.decode() for x in (proc/'cmdline').read_bytes().split(b'\0') if x]
        idx=args.index('-m')
        if args[idx+1:idx+3]!=['basic_core','live']:return False
        if args[idx+3:]!=['--root',str(Path(root).resolve())]:return False
        return (proc/'cwd').resolve()==Path(root).resolve()
    except (OSError,UnicodeError,ValueError,IndexError):return False

def check_handoff(root,path,run=subprocess.run):
    path=Path(path)
    reject_disk_dropins(path)
    if path.exists() or path.is_symlink():
        expected=verify_unit(path,root)
        verify_effective_unit(path,root,expected,run)
    if runtime.running(root,'live') and not (path.exists() and service_owns_live(root,run)):
        raise SafetyError('BACKGROUND_LIVE_MANAGER_RUNNING: pause entries, wait flat, then ./prob stop --mode live before installing the service')

def install(root,user=None,python_executable='/usr/bin/python3',preview=False,run=subprocess.run,unit_dir=UNIT_DIR):
    spec=installation(root,user,python_executable)
    if preview:
        print(spec['content'],end='');return spec['content']
    path=Path(unit_dir)/SERVICE
    check_handoff(spec['root'],path,run)
    if path.exists() and path.read_text()!=spec['content']:raise SafetyError('existing unit configuration differs; review it before replacing')
    prefix=[] if os.geteuid()==0 else ['sudo','--']
    if not path.exists():
        # The temporary file contains only the public unit definition, no keys.
        fd,name=tempfile.mkstemp(prefix='probability-service-',suffix='.service')
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as file:file.write(spec['content'])
            run(prefix+['install','-o','root','-g','root','-m','0644',name,str(path)],check=True)
        finally:Path(name).unlink(missing_ok=True)
    run(prefix+['systemctl','daemon-reload'],check=True)
    verify_effective_unit(path,spec['root'],spec,run)
    run(prefix+['systemctl','enable',SERVICE],check=True)
    print('자동 재시작·부팅 시작 등록 완료. 지금 관리 프로세스나 거래를 시작하지 않았습니다.')
    print('최초 실전 시작: ./prob doctor → ./prob setup(필요할 때) → ./prob start-live')
    print('기존 포지션 복구만 시작: ./prob resume-live (신규 진입 중지 상태)')
    return spec['content']

def main(argv=None):
    parser=argparse.ArgumentParser(description='Install EC2 systemd supervision; never starts or arms trading.')
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--user',default=None)
    parser.add_argument('--python',dest='python_executable',default='/usr/bin/python3')
    parser.add_argument('--print',dest='preview',action='store_true',help='print the validated unit; do not use sudo or systemctl')
    args=parser.parse_args(argv)
    try:install(args.root,args.user,args.python_executable,args.preview);return 0
    except (SafetyError,OSError,ValueError,subprocess.CalledProcessError) as exc:
        print('BLOCKED: '+(str(exc) if isinstance(exc,SafetyError) else type(exc).__name__),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
