import hashlib
import json
import os
import shutil
import threading
import uuid
import base64
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .data import Archive, validate_root
from .demo import DemoArchive
from .exporting import FORMATS, export_index, export_session, clean
from .avatars import AvatarStore, image_type, avatar_host
from .version import VERSION,APP_NAME
from .paths import app_data_root
from .export_options import export_preferences
from wx4.forward import build_forward


class Cancelled(Exception):
    pass


# The queue used to stop at ten sessions, which made a full library export
# impossible. Jobs still run one at a time (max_workers=1); these caps only
# guard against runaway task creation and unbounded history files.
QUEUE_LIMIT = 200
TASK_HISTORY = 240


def merge_summaries(records):
    merged = {}
    for record in records:
        for key, value in (record.get('resource_summary') or {}).items():
            if isinstance(value, int):
                merged[key] = merged.get(key, 0) + value
    return merged


def atomic_json(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def options_from(payload):
    result = {'query': str(payload.get('query', ''))[:200], 'kind': payload.get('kind', 'all')}
    result.update(export_preferences(payload))
    if result['kind'] not in ('all','text','image','voice','video','file','link','system'):
        raise ValueError('消息类型不受支持')
    for key, tail in [('start', 'T00:00:00'), ('end', 'T23:59:59')]:
        value = payload.get(key)
        if value:
            value=str(value)
            normalized=value[:16]+(':00' if key=='start' else ':59') if 'T' in value else value[:10]+tail
            result[key] = int(datetime.fromisoformat(normalized).replace(
                tzinfo=timezone(timedelta(hours=8))).timestamp())
    if result.get('start', 0) > result.get('end', 10**12):
        raise ValueError('开始日期不能晚于结束日期')
    return result


class Service:
    def __init__(self, assets, root=None):
        self.assets = Path(assets)
        self.root = Path(root) if root else app_data_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config_path = self.root/'config.json'
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='archive-jobs')
        self.events = {}
        self.accounts = {}
        self.config = {'theme':'light', 'export_dir': str(Path.home()/'Documents'/APP_NAME/'exports'),
                       'library': None, 'tasks': []}
        if self.config_path.exists():
            try:
                self.config.update(json.loads(self.config_path.read_text(encoding='utf-8')))
            except (OSError, ValueError):
                pass
        legacy_export=str(Path.home()/'Documents'/'WeChatExporter'/'exports')
        if os.path.normcase(os.path.normpath(self.config['export_dir']))==os.path.normcase(os.path.normpath(legacy_export)):
            self.config['export_dir']=str(Path.home()/'Documents'/APP_NAME/'exports')
        for task in self.config['tasks']:
            if task['status'] in ('queued','running'):
                task.update(status='interrupted', detail='上次运行已中断，请重新创建任务。')
        self.library = self.config.get('library')

    def save(self):
        with self.lock:
            tasks = self.config['tasks']
            if len(tasks) > TASK_HISTORY:
                # Drop the oldest finished entries first; queued or running work
                # must never disappear from the queue while it is still pending.
                excess = len(tasks) - TASK_HISTORY
                kept = []
                for task in tasks:
                    if excess > 0 and task['status'] not in ('queued', 'running'):
                        excess -= 1
                        continue
                    kept.append(task)
                self.config['tasks'] = kept
            atomic_json(self.config_path, self.config)

    def archive(self, library=None):
        lib = library or self.library
        if not lib:
            raise ValueError('请先连接微信、打开备份，或进入演示模式。')
        return DemoArchive(lib) if lib.get('demo') else Archive(lib)

    def avatar(self,url):
        if not isinstance(url,str) or len(url)>4096 or not avatar_host(url):return {'url':''}
        data=SimpleNamespace(_conns={},avatar_of=lambda user:url)
        blob=AvatarStore(data,self.root/'avatar-cache').blob('',download=True)
        return {'url':'data:'+image_type(blob)[1]+';base64,'+base64.b64encode(blob).decode() if blob else ''}

    def state(self):
        with self.lock:
            lib = self.library
            return {'version':VERSION, 'theme':self.config['theme'],
                    'export_dir':self.config['export_dir'],
                    'library':dict(name=lib.get('profile',{}).get('name') or lib['name'],
                                   profile=lib.get('profile',{}),demo=lib.get('demo', False),
                                   imported_at=lib.get('imported_at','')) if lib else None,
                    'tasks': [self.public_task(t) for t in reversed(self.config['tasks'])]}

    @staticmethod
    def public_task(t):
        result={k:v for k,v in t.items() if k not in ('folder','files','session','queued_sessions','cancel_detail')}
        files=t.get('files') or []
        if files:
            if len(files)==1: result['title']=files[0]['name']
            result.setdefault('format',files[0].get('format',''))
            result.setdefault('start_time',files[0].get('start_time'))
            result.setdefault('end_time',files[-1].get('end_time'))
            if len(files)>1: result['sessions']=len(files)
        return result

    def hydrate_profile(self,archive,lib):
        if isinstance(archive,Archive):
            lib['profile']=archive.profile()
            lib['wxid']=lib['profile']['wxid']
            lib['name']=lib['profile']['name']

    def _new_job(self, kind, title):
        with self.lock:
            if sum(t['status'] in ('queued','running') for t in self.config['tasks']) >= QUEUE_LIMIT:
                raise ValueError('任务队列已满（%d 个待处理任务），请等待当前任务完成。' % QUEUE_LIMIT)
            job = dict(id=uuid.uuid4().hex, kind=kind, title=title, status='queued', progress=0,
                       detail='等待处理', count=0, created_at=datetime.now().strftime('%Y-%m-%d %H:%M'),
                       demo=bool(self.library and self.library.get('demo')))
            self.config['tasks'].append(job)
            self.events[job['id']] = threading.Event()
            self.save()
        return job

    def _update(self, job, **values):
        with self.lock:
            job.update(values)

    def _check(self, job):
        if self.events[job['id']].is_set():
            raise Cancelled()

    def _run(self, job, fn):
        try:
            self._check(job)
            self._update(job, status='running', detail='正在准备…')
            fn()
            self._update(job, status='completed', progress=100)
        except Cancelled:
            self._update(job, status='cancelled',
                         detail=job.get('cancel_detail') or '任务已取消，未完成的导出已清理。')
        except Exception as exc:
            # Do not return tracebacks, key values or chat content to the UI.
            message = str(exc)
            if len(message) > 240 or any(s in message for s in ('enc_key','Traceback')):
                message = '处理失败，请检查数据目录和微信登录状态后重试。'
            self._update(job, status='failed', detail=message or type(exc).__name__)
        finally:
            self.save()

    def demo(self):
        self.library = dict(name='演示归档', demo=True, wxid='demo_me', root='', imported_at='虚构数据')
        return self.state()

    def import_archive(self, path):
        root = validate_root(path)
        lib = dict(name=Path(root).name or '本地归档', root=root, wxid='', imported_at=datetime.now().strftime('%Y-%m-%d %H:%M'))
        archive = Archive(lib)
        try:
            if not archive.sessions():
                raise ValueError('没有找到可浏览的会话。请检查备份是否完整。')
            self.hydrate_profile(archive,lib)
        finally:
            archive.close()
        with self.lock:
            self.library = lib
            self.config['library'] = lib
            self.save()
        return self.state()

    def detect(self):
        from wx4 import key, paths
        found = paths.list_account_dirs()
        self.accounts = {hashlib.sha256(a['db_storage'].encode()).hexdigest()[:16]: a for a in found}
        def label(i,a):
            lib=self.library or {}
            profile=lib.get('profile',{}) if lib.get('account_dir')==a['path'] else {}
            return {'id':i,'name':profile.get('name') or '微信账号',
                    'wxid':profile.get('alias') or a['wxid'], 'avatar_url':profile.get('avatar_url','')}
        return {'running':bool(key.find_weixin_processes()),
                'accounts':[label(i,a) for i,a in self.accounts.items()]}

    def add_storage(self, directory):
        from wx4 import paths
        p = Path(directory).resolve()
        if p.name != 'db_storage':
            p = p/'db_storage'
        if not p.is_dir():
            raise ValueError('请选择账号目录或其中的 db_storage 文件夹。')
        token = hashlib.sha256(str(p).encode()).hexdigest()[:16]
        self.accounts[token] = dict(dir_name=p.parent.name, wxid=paths.split_wxid(p.parent.name),
                                     path=str(p.parent), db_storage=str(p))
        return {'id':token,'name':'微信账号','wxid':self.accounts[token]['wxid']}

    def connect(self, account_id):
        if account_id not in self.accounts:
            raise ValueError('请重新检测并选择微信账号。')
        account = dict(self.accounts[account_id])
        job = self._new_job('connect', '连接微信 · ' + account['dir_name'])
        job['demo'] = False
        def work():
            from wx4 import key, decrypt
            slot = self.root/'archives'/account_id
            slot.mkdir(parents=True, exist_ok=True)
            keys_file = slot/'keys.json'
            self._update(job, progress=8, detail='正在获取本机微信的读取凭据…')
            def key_progress(message):
                self._check(job)
            ex = key.KeyExtractor(account['db_storage'], progress=key_progress,cancel=lambda:self._check(job))
            ex.extract(save_to=str(keys_file))
            self._check(job)
            keys = {k:v for k,v in key.load_keys(str(keys_file)).items() if not k.startswith('_')}
            if not keys:
                raise ValueError('未获取到可用密钥。请确认微信已登录；若权限不足，请以管理员身份重新打开软件。此版本也可能需要进一步适配。')
            destination = slot/('snapshot_'+job['id'][:12])
            self._update(job, progress=45, detail='正在创建本地聊天快照…')
            n = 0
            def dec_progress(message):
                nonlocal n
                self._check(job); n += 1
                self._update(job, progress=min(90, 45+int(45*n/max(1,len(keys)))), detail='正在整理聊天数据库…')
            try:
                res = decrypt.decrypt_all(keys, account['db_storage'], str(destination), progress=dec_progress,cancel=lambda:self._check(job))
                self._check(job)
                validate_root(destination)
                lib = dict(name=account['dir_name'], root=str(destination), wxid=account['wxid'],
                           account_dir=account['path'], imported_at=datetime.now().strftime('%Y-%m-%d %H:%M'))
                archive = Archive(lib)
                try:
                    sessions = archive.sessions()
                    if not sessions: raise ValueError('快照中没有可用会话，请检查账号或重新同步。')
                    self.hydrate_profile(archive,lib)
                    self._update(job,title='连接微信 · '+lib['name'])
                finally: archive.close()
                failed = len(res['failed'])
                with self.lock:
                    self.library = lib; self.config['library'] = lib
                self._update(job, detail='已连接，找到 %d 个会话%s' % (len(sessions), ('；%d 个数据库未能处理' % failed) if failed else ''))
            except BaseException:
                shutil.rmtree(destination, ignore_errors=True)
                raise
        self.pool.submit(self._run, job, work)
        return {'job':job['id']}

    def sessions(self):
        archive = self.archive()
        try:
            self.hydrate_profile(archive,self.library)
            return archive.sessions()
        finally: archive.close()

    def messages(self, payload):
        archive = self.archive()
        try:
            session = payload.get('session', '')
            if session not in {s['username'] for s in archive.sessions()}:
                raise ValueError('会话不存在')
            before=payload.get('before')
            if before is not None and (not isinstance(before,list) or len(before)!=3 or not all(isinstance(x,(int,float)) for x in before)):
                raise ValueError('分页参数无效')
            return archive.page(session, options_from(payload), before=before)
        finally: archive.close()

    def qce_preview(self,payload):
        session=(payload.get('peer') or {}).get('peerUid','')
        page=max(1,min(int(payload.get('page',1)),10000000))
        limit=max(1,min(int(payload.get('limit',50)),200))
        flt=payload.get('filter') or {}
        options={'query':str(flt.get('query',''))[:200],'kind':'all'}
        if flt.get('startTime'):options['start']=int(flt['startTime'])//1000
        if flt.get('endTime'):options['end']=int(flt['endTime'])//1000
        archive=self.archive()
        try:
            if session not in {s['username'] for s in archive.sessions()}:raise ValueError('会话不存在')
            if isinstance(archive,DemoArchive):
                rows=list(archive.messages(session,options,descending=True));total=len(rows)
                rows=rows[(page-1)*limit:page*limit]
            else:rows,total=archive.numbered_page(session,options,page,limit)
            result=[]
            for m in rows:
                rich=dict(m.get('extra',{}))
                if m.get('kind')=='chatrecord' and isinstance(archive,Archive):
                    rich['forward']=clean(build_forward(archive.data,m,limit=1500))
                result.append(dict(msgId='%s_%s_%s'%(m['shard'],m['local_id'],m['server_id']),
                                   msgSeq=m['local_id'],msgTime=m['create_time'],
                                   senderUid=m['sender_wxid'],senderUin='',
                                   senderAvatar=archive.avatars.ui(m['sender_wxid']) if isinstance(archive,Archive) else '',
                                   richContent=rich,
                                   elements=[{'textElement':{'content':m['text']}}],
                                   peerUin=session,chatType=2 if session.endswith('@chatroom') else 1,
                                   sendType=1 if m['is_sender'] else 0,subMsgType=0,
                                   sendMemberName=m['sender'],sendNickName=m['sender']))
            return dict(messages=result,totalCount=total,currentPage=page,totalPages=max(1,(total+limit-1)//limit),hasNext=page*limit<total,fetchedAt=datetime.now().isoformat())
        finally:archive.close()

    def export(self, payload):
        if not self.library: raise ValueError('请先连接数据')
        fmt=payload.get('format','html')
        if fmt not in FORMATS: raise ValueError('不支持此格式')
        library=dict(self.library)
        requested=[str(user) for user in (payload.get('sessions') or [])]
        order={user:index for index,user in enumerate(dict.fromkeys(requested))}
        if not order: raise ValueError('请先选择至少一个会话')
        archive=self.archive(library)
        try:
            sessions=[s for s in archive.sessions() if s['username'] in order]
        finally: archive.close()
        if not sessions: raise ValueError('请先选择至少一个会话')
        # Keep the order the selection was presented in, so an unattended run
        # walks the queue exactly as the user saw it, one session at a time.
        sessions.sort(key=lambda s: order[s['username']])
        options=options_from(payload); options.update(format=fmt,demo=library.get('demo',False))
        output=Path(self.config['export_dir']).resolve()
        if len(sessions)==1: return self._export_single(library,sessions[0],options,fmt,output)
        return self._export_queue(library,sessions,options,fmt,output)

    def _export_single(self, library, session, options, fmt, output):
        job=self._new_job('export', session['display'])
        job.update(format=fmt,session=session['username'],start_time=options.get('start'),end_time=options.get('end'))
        job['demo']=library.get('demo',False)
        folder=output/('%s_%s%s' % (datetime.now().strftime('%Y%m%d_%H%M%S'),job['id'][:6], '_演示' if job['demo'] else ''))
        def work():
            staging=output/('.partial_'+job['id'])
            staging.mkdir(parents=True, exist_ok=False)
            archive=self.archive(library)
            try:
                def progress(n):
                    self._check(job)
                    self._update(job, count=n, detail='正在导出 %s · %s 条' % (session['display'],f'{n:,}'))
                result=export_session(archive,session,options,staging,self.assets,progress,lambda:self._check(job),
                    status=lambda message:self._update(job,detail=message))
                export_index(staging,[result],demo=job['demo'])
                self._check(job)
                os.replace(staging,folder)
                result['path']=str(folder/Path(result['path']).relative_to(staging))
                self._update(job, folder=str(folder), files=[result], count=result['count'],
                             start_time=result.get('start_time'),end_time=result.get('end_time'),
                             resource_summary=result.get('resource_summary',{}),
                             detail='已保存 1 个会话，共 %s 条消息' % f"{result['count']:,}")
            finally:
                archive.close()
                if staging.exists(): shutil.rmtree(staging,ignore_errors=True)
        self.pool.submit(self._run, job, work)
        return {'job':job['id']}

    def _export_queue(self, library, sessions, options, fmt, output):
        """One cancellable job that exports the whole selection one by one.

        Each session is written to its own directory inside a single archive
        folder, so a failure or an interruption keeps everything finished so
        far and the folder stays browsable through its index page.
        """
        total_sessions=len(sessions)
        job=self._new_job('export', '批量导出 · %d 个会话' % total_sessions)
        job.update(format=fmt,queued_sessions=total_sessions,session='',
                   start_time=options.get('start'),end_time=options.get('end'))
        job['demo']=library.get('demo',False)
        folder=output/('%s_%s%s' % (datetime.now().strftime('%Y%m%d_%H%M%S'),job['id'][:6], '_演示' if job['demo'] else ''))
        def work():
            folder.mkdir(parents=True, exist_ok=True)
            archive=self.archive(library)
            records=[]; failures=[]; total=0
            def snapshot(incomplete=False):
                try: export_index(folder, records, demo=job['demo'], listing=True, failures=failures,
                                  incomplete=incomplete, total_sessions=total_sessions)
                except Exception: pass
            try:
                for index, session in enumerate(sessions):
                    self._check(job)
                    name=session['display']
                    self._update(job, progress=int(100*index/total_sessions), count=total,
                                 detail='第 %d/%d 个 · %s · 正在读取' % (index+1,total_sessions,name))
                    def progress(n, name=name, position=index):
                        self._check(job)
                        self._update(job, count=total+n,
                                     detail='第 %d/%d 个 · %s · 已读取 %s 条' % (position+1,total_sessions,name,f'{n:,}'))
                    def status(message, name=name, position=index):
                        self._update(job, detail='第 %d/%d 个 · %s · %s' % (position+1,total_sessions,name,message))
                    staging=folder/('.partial_%04d' % index)
                    shutil.rmtree(staging,ignore_errors=True)
                    staging.mkdir(parents=True, exist_ok=True)
                    try:
                        result=export_session(archive,session,options,staging,self.assets,progress,
                                              lambda:self._check(job),status=status)
                        renames={}
                        for item in list(staging.iterdir()):
                            target=folder/item.name
                            if target.exists():
                                target=folder/('%04d_%s' % (index,item.name))
                                renames[item.name]=target.name
                            os.replace(item,target)
                        relative=Path(result['path']).relative_to(staging)
                        result['path']=str(folder.joinpath(renames.get(relative.parts[0],relative.parts[0]),
                                                           *relative.parts[1:]))
                    except Cancelled:
                        raise
                    except Exception as exc:
                        failures.append({'name':name,'error':str(exc)[:200] or type(exc).__name__})
                    else:
                        records.append(result); total+=result['count']
                    finally:
                        shutil.rmtree(staging,ignore_errors=True)
                    if index % 25 == 24: snapshot()
                self._check(job)
                summary=merge_summaries(records); summary['failed_sessions']=len(failures)
                snapshot()
                self._update(job, folder=str(folder), files=records, count=total,
                             start_time=records[0].get('start_time') if records else None,
                             end_time=records[-1].get('end_time') if records else None,
                             resource_summary=summary,
                             detail='已保存 %d/%d 个会话，共 %s 条消息%s' % (
                                 len(records),total_sessions,f'{total:,}',
                                 ('；%d 个会话失败，详见导出目录的 index.html' % len(failures)) if failures else ''))
            except BaseException:
                snapshot(incomplete=True)
                job['cancel_detail']=('已停止：已完成的 %d 个会话保留在导出目录，其余尚未处理。'
                                      % len(records))
                raise
            finally:
                archive.close()
        self.pool.submit(self._run, job, work)
        return {'job':job['id']}

    def cancel(self, job_id):
        if job_id in self.events: self.events[job_id].set()
        return {'ok':True}

    def delete_task(self,job_id):
        with self.lock:
            task=next((t for t in self.config['tasks'] if t['id']==job_id),None)
            if not task:raise ValueError('任务不存在')
            if task['status'] in ('running','queued'):raise ValueError('请先停止任务')
            self.config['tasks'].remove(task)
            self.events.pop(job_id,None)
            self.save()
        return self.state()

    def task_file(self,job_id):
        folder=self.task_folder(job_id)
        with self.lock:
            task=next(t for t in self.config['tasks'] if t['id']==job_id)
            files=task.get('files') or []
            path=Path(files[0]['path']).resolve() if len(files)==1 else folder/'index.html'
            if path.is_relative_to(folder) and path.is_file():return path
        raise ValueError('导出文件不存在')

    def task_folder(self, job_id):
        with self.lock:
            for t in self.config['tasks']:
                if t['id']==job_id and t.get('folder') and t['status']=='completed':
                    p=Path(t['folder']).resolve()
                    if p.is_dir(): return p
        raise ValueError('导出目录不存在或任务尚未完成')

    def settings(self, payload):
        theme=payload.get('theme')
        if theme in ('light','dark'): self.config['theme']=theme
        self.save()
        return self.state()

    def call(self, action, payload):
        handlers={'state':self.state, 'demo':self.demo, 'detect':self.detect,
                  'avatar':lambda:self.avatar(payload.get('url')),
                  'sessions':self.sessions, 'messages':lambda:self.messages(payload),
                  'qce_preview':lambda:self.qce_preview(payload),
                  'connect':lambda:self.connect(payload.get('accountId')),
                  'export':lambda:self.export(payload),
                  'cancel':lambda:self.cancel(payload.get('id')),
                  'delete_task':lambda:self.delete_task(payload.get('id')),
                  'settings':lambda:self.settings(payload)}
        if action not in handlers: raise ValueError('不支持的操作')
        return handlers[action]()

    def shutdown(self):
        for event in self.events.values(): event.set()
        self.pool.shutdown(wait=False,cancel_futures=True)
        self.save()
