"""Bounded, cancellable export, derived from QCE's batch/writer pipeline."""
import csv
import hashlib
import html
import io
import json
import re
from datetime import datetime
from pathlib import Path
from posixpath import relpath
from urllib.parse import quote
from .streaming import BufferedTextWriter
from .resources import ResourceStore
from .version import VERSION,APP_NAME
from .export_options import export_preferences,is_system_message

FORMATS = {'html', 'json', 'jsonl', 'txt', 'csv', 'md'}


def safe_name(value):
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', value).strip(' .')[:70] or 'chat'
    if text.split('.')[0].upper() in {'CON','PRN','AUX','NUL', *['COM'+str(i) for i in range(1,10)], *['LPT'+str(i) for i in range(1,10)]}:
        text = '_' + text
    return text


def clean(msg):
    if isinstance(msg,list):return [clean(value) for value in msg]
    if isinstance(msg,dict):
        return {k:clean(v) for k,v in msg.items() if not k.startswith('_') and k not in ('packed_hex','content')}
    return msg


def portable_paths(msg,prefix):
    if msg.get('avatar'):msg['avatar']=prefix+msg['avatar']
    for resource in msg.get('resources',[]):resource['path']=prefix+resource['path']
    if msg.get('quote'):portable_paths(msg['quote'],prefix)
    for child in (msg.get('forward') or {}).get('messages',[]):portable_paths(child,prefix)


def readable_text(msg,markdown=False):
    text=msg.get('text','')
    quote=msg.get('quote')
    if quote:
        reference=readable_text(quote,markdown)
        text+='\n'+(('> ' if markdown else '↳ ')+(quote.get('sender') or '')+': '+reference.replace('\n','\n> ' if markdown else '\n  '))
    forward=msg.get('forward')
    if forward:
        text+='\n【'+forward.get('title','转发的聊天记录')+'】'
        for child in forward.get('messages',[]):
            text+='\n['+child.get('time_str','')+'] '+child.get('sender','')+'\n'+readable_text(child,markdown)
        if forward.get('notice'):text+='\n'+forward['notice']
    for resource in msg.get('resources',[]):
        path=resource['path']
        if markdown:
            label='图片' if resource['kind']=='image' else resource['name']
            text+='\n'+('!' if resource['kind']=='image' and resource['status']=='available' else '')+'['+label.replace(']','')+'](<'+path+'>)'
        else:text+='\n'+path
    if msg.get('resource_notice'):text+='\n'+msg['resource_notice']
    return text


def csv_cell(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value


def materialize_forward(msg,folder,template,exported_at,cancel):
    """Write independent offline forward pages, plus complete JSON/TXT copies.

    Pages live beside the main HTML so media paths stay portable and cannot
    escape via '..'. Only explicitly referenced media are in the export folder.
    """
    if msg.get('quote'):materialize_forward(msg['quote'],folder,template,exported_at,cancel)
    forward=msg.get('forward')
    if not forward:return
    cancel();messages=forward.get('messages') or []
    identity=hashlib.sha256(json.dumps(forward,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:24]
    stem='forward_'+identity
    title=forward.get('title') or '转发的聊天记录'
    preview=[{'sender':m.get('sender',''),'text':m.get('text','')[:100]} for m in messages[:3]]
    meta={'application':APP_NAME,'version':VERSION,'name':title,
        'exported_at':exported_at,'count':len(messages),'forward':True,'notice':forward.get('notice','')}
    if not (folder/(stem+'.html')).exists():
        # Save full nested content before replacing child trees with page cards.
        with BufferedTextWriter(folder/(stem+'.json')) as writer:
            writer.write(json.dumps({'metadata':meta,'messages':messages},ensure_ascii=False))
        with BufferedTextWriter(folder/(stem+'.txt')) as writer:
            writer.write(title+'\n\n')
            for child in messages:
                cancel();writer.write('['+child.get('time_str','')+'] '+child.get('sender','')+'\n'+readable_text(child)+'\n\n')
            if forward.get('notice'):writer.write(forward['notice'])
        for child in messages:materialize_forward(child,folder,template,exported_at,cancel)
        chunks=[];batch=[];batch_bytes=0
        def flush():
            if not batch:return
            filename=stem+'_c%06d.js'%(len(chunks)+1)
            with BufferedTextWriter(folder/filename) as writer:
                writer.write('window.__wxChunk='+json.dumps(batch,ensure_ascii=False)+';')
            stamps=[int(child.get('create_time') or 0) for child in batch]
            chunks.append({'file':filename,'count':len(batch),'start_time':min(stamps),'end_time':max(stamps)})
            batch.clear()
        for child in messages:
            cancel();length=len(json.dumps(child,ensure_ascii=False).encode())
            if batch and (batch_bytes+length>4*1024*1024 or len(batch)>=500):flush();batch_bytes=0
            batch.append(child);batch_bytes+=length
        flush()
        manifest={**meta,'chunks':chunks,'downloads':{'json':stem+'.json','txt':stem+'.txt'}}
        config=json.dumps(manifest,ensure_ascii=False).replace('<','\\u003c')
        (folder/(stem+'.html')).write_text(template.replace('__MANIFEST__',config),encoding='utf-8')
    msg['forward']={k:forward[k] for k in ('title','count','total','truncated','notice') if k in forward}
    msg['forward'].update(title=title,count=len(messages),preview=preview,
        html=stem+'.html',json=stem+'.json',txt=stem+'.txt')


def export_session(archive, session, options, folder, viewer_assets, progress, cancel, status=None):
    fmt = options.get('format', 'html')
    if fmt not in FORMATS:
        raise ValueError('不支持的导出格式')
    title = session['display']
    stem = safe_name(title) + '_' + hashlib.sha256(session['username'].encode()).hexdigest()[:8]
    count = 0
    first_time = last_time = None
    out = folder / stem if fmt=='html' else folder / (stem+'_assets')
    out.mkdir(exist_ok=True)
    preferences=export_preferences(options)
    resources=ResourceStore(archive.data,session['username'],out,cancel,status,preferences) if hasattr(archive,'data') else None
    def records():
        nonlocal first_time,last_time
        try:
            for msg in archive.messages(session['username'],options,cancel=cancel):
                if not preferences['include_system_messages'] and is_system_message(msg):continue
                first_time=msg['create_time'] if first_time is None else first_time
                last_time=msg['create_time']
                yield resources.enrich(msg) if resources else msg
        finally:
            if resources:resources.close()
    meta = dict(application=APP_NAME, version=VERSION, session=session['username'],
                name=title, exported_at=datetime.now().isoformat(timespec='seconds'),
                time_zone='Asia/Shanghai',
                export_options=preferences,
                demo=bool(options.get('demo')), filters={k: options[k] for k in ('query','kind','start','end') if k in options})
    # Like QCE's text exporter, source iteration is separate from rendering; each
    # record is released immediately after writing. HTML rotates bounded chunks.
    if fmt == 'html':
        template = (viewer_assets / 'viewer.html').read_text(encoding='utf-8')
        chunks, batch = [], []
        batch_bytes = 0
        def flush():
            if not batch: return
            filename = 'c%06d.js' % (len(chunks)+1)
            with BufferedTextWriter(out / filename) as writer:
                writer.write('window.__wxChunk=' + json.dumps(batch, ensure_ascii=False) + ';')
            stamps=[int(child.get('create_time') or 0) for child in batch]
            chunks.append(dict(file=filename,count=len(batch),start_time=min(stamps),end_time=max(stamps)))
            batch.clear()
        for msg in records():
            record=clean(msg)
            materialize_forward(record,out,template,meta['exported_at'],cancel)
            record_bytes=len(json.dumps(record,ensure_ascii=False).encode('utf-8'))
            if batch and batch_bytes+record_bytes>4*1024*1024:
                flush();batch_bytes=0
            batch.append(record);batch_bytes+=record_bytes;count+=1
            if len(batch) >= 500:flush();batch_bytes=0
            if count % 200 == 0: progress(count)
        flush()
        manifest = dict(**meta, count=count, chunks=chunks,
                        resource_summary=resources.stats if resources else {})
        config = json.dumps(manifest, ensure_ascii=False).replace('<', '\\u003c')
        (out / 'index.html').write_text(template.replace('__MANIFEST__', config), encoding='utf-8')
        path = out / 'index.html'
    else:
        path = folder / (stem + '.' + fmt)
        with BufferedTextWriter(path, encoding='utf-8-sig' if fmt=='csv' else 'utf-8') as writer:
            if fmt=='json': writer.write('{"metadata":' + json.dumps(meta, ensure_ascii=False) + ',"messages":[')
            if fmt=='jsonl': writer.write(json.dumps({'metadata':meta}, ensure_ascii=False)+'\n')
            if fmt in ('txt','md'):
                writer.write(('# ' if fmt=='md' else '') + title + '\n' + ('演示数据 · ' if meta['demo'] else '') + '由 '+APP_NAME+' 导出\n\n')
            if fmt=='csv': writer.write('时间,会话,发送者,类型,内容,消息ID\r\n')
            for msg in records():
                # Non-HTML exports keep resources next to the document. JSON
                # paths are relative to the document, not the assets directory.
                prefix=out.name+'/'
                portable_paths(msg,prefix)
                text=readable_text(msg,fmt=='md')
                if fmt=='json':
                    writer.write((',' if count else '') + json.dumps(clean(msg), ensure_ascii=False))
                elif fmt=='jsonl': writer.write(json.dumps(clean(msg), ensure_ascii=False)+'\n')
                elif fmt=='csv':
                    buffer=io.StringIO(newline=''); row=csv.writer(buffer)
                    row.writerow([csv_cell(v) for v in [msg['time_str'], title, msg['sender'], msg['type_name'], text, msg['server_id']]])
                    writer.write(buffer.getvalue())
                else:
                    writer.write(f"[{msg['time_str']}] {msg['sender']}\n{text}\n\n")
                count += 1
                if count % 200 == 0: progress(count)
            if fmt=='json': writer.write('],"count":'+str(count)+'}')
    cancel()
    progress(count)
    return dict(name=title, count=count, path=str(path), format=fmt,
                start_time=first_time,end_time=last_time,
                resource_summary=resources.stats if resources else {})


def _listing_page(folder, records, demo=False, failures=None, incomplete=False, total_sessions=None):
    """Entry point for a multi-session export.

    The per-page session switcher of `export_index` rewrites every HTML file
    with one <option> per session, which is unusable for hundreds of chats.
    A searchable landing page keeps a full-library export browsable.
    """
    failures=list(failures or [])
    exported=len(records)
    messages=sum(int(record.get('count') or 0) for record in records)
    expected=total_sessions if isinstance(total_sessions,int) and total_sessions>0 else exported+len(failures)
    formats={str(record.get('format') or '').upper() for record in records if record.get('format')}
    rows=[]
    for record in records:
        try: href=quote(Path(record['path']).relative_to(folder).as_posix(),safe='/')
        except ValueError: continue
        name=str(record.get('name') or '')
        rows.append('<li data-search="%s"><a href="%s">%s</a><span class="count">%s 条</span></li>' % (
            html.escape(name.casefold(),quote=True),html.escape(href,quote=True),
            html.escape(name),format(int(record.get('count') or 0),',')))
    for failure in failures:
        name=str(failure.get('name') or '')
        rows.append('<li class="failed" data-search="%s"><span>%s</span>'
                    '<span class="count">失败 · %s</span></li>' % (
                        html.escape(name.casefold(),quote=True),html.escape(name),
                        html.escape(str(failure.get('error') or '未知错误'))))
    meta=' · '.join([APP_NAME+' · 本地归档','导出于 '+datetime.now().strftime('%Y-%m-%d %H:%M'),
                     '共 %s 个会话' % format(expected,','),'%s 条消息' % format(messages,',')]
                    +(['格式 '+' / '.join(sorted(formats))] if formats else [])
                    +(['演示数据'] if demo else []))
    notices=''
    if incomplete:
        notices+='<p class="warn">本次导出未完成（已完成 %s / %s 个会话），下面是已经保存的部分。</p>' % (
            format(exported,','),format(expected,','))
    if failures:
        notices+='<p class="warn">%s 个会话未能导出，列表中标红显示原因；其余会话已完成。</p>' % format(len(failures),',')
    body=('\n'.join(rows) or '<li class="empty">没有可显示的会话</li>')
    script=('const q=document.getElementById("q"),label=document.getElementById("count"),'
            'rows=[...document.querySelectorAll("#list li")];'
            'function refresh(){const v=q.value.trim().toLowerCase();let n=0;'
            'for(const row of rows){const hit=!v||(row.dataset.search||"").includes(v);'
            'row.hidden=!hit;if(hit)n++;}'
            'label.textContent="显示 "+n+" / "+rows.length+" 个会话";}'
            'q.addEventListener("input",refresh);refresh();')
    content=('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width,initial-scale=1">'
             '<title>聊天记录归档 · '+html.escape(APP_NAME)+'</title><style>'
             'body{margin:0;background:#f7f5fb;color:#26212f;font:15px/1.6 "Microsoft YaHei",system-ui,sans-serif}'
             'header{max-width:1100px;margin:0 auto;padding:44px 32px 20px}h1{margin:0 0 6px;font-size:26px}'
             '.meta{margin:0;color:#7e718f;font-size:13px}'
             '.warn{margin:16px 0 0;padding:12px 16px;border-radius:12px;background:#fff4e5;color:#8a5200;font-size:13px}'
             '.tools{max-width:1100px;margin:0 auto;padding:8px 32px}'
             'input{width:100%;box-sizing:border-box;padding:11px 18px;border-radius:999px;border:1px solid #e3daef;'
             'font:inherit;background:#fff;outline:none}input:focus{border-color:#8771cf}'
             'ul{list-style:none;padding:0;max-width:1100px;margin:14px auto 64px}'
             'li{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 18px;'
             'background:#fff;border-radius:14px;margin:8px 32px;box-shadow:0 1px 2px rgba(28,20,45,.05)}'
             'li a{color:#5b46a8;text-decoration:none;font-weight:600;overflow-wrap:anywhere}'
             'li a:hover{text-decoration:underline}'
             '.count{color:#7e718f;font-size:12px;white-space:nowrap}'
             'li.failed{background:#fff5f5;color:#9b2c2c}li.failed .count{color:#c53030}'
             '.empty{padding:36px;text-align:center;color:#7e718f}'
             '</style><header><h1>聊天记录归档</h1><p class="meta">'+meta+'</p>'+notices+
             '</header><div class="tools"><input id="q" type="search" placeholder="输入名称筛选会话" '
             'autocomplete="off" aria-label="筛选会话"><p class="meta" id="count"></p></div>'
             '<ul id="list">'+body+'</ul><script>'+script+'</script></html>')
    (folder/'index.html').write_text(content,encoding='utf-8')


def export_index(folder, records, demo=False, listing=False, failures=None, incomplete=False, total_sessions=None):
    if listing or not (records and all(r['format']=='html' for r in records)):
        _listing_page(folder,records,demo,failures,incomplete,total_sessions)
        return
    # Open HTML archives in the viewer; batch navigation stays inside it.
    if records and all(r['format']=='html' for r in records):
        paths=[Path(r['path']).relative_to(folder).as_posix() for r in records]
        if len(records)>1:
            for record,current in zip(records,paths):
                choices=''.join('<option value="%s"%s>%s</option>' % (
                    html.escape(quote(relpath(target,Path(current).parent.as_posix()),safe='/'),quote=True),
                    ' selected' if target==current else '',html.escape(item['name']))
                    for item,target in zip(records,paths))
                nav=('<nav aria-label="归档会话" style="margin-bottom:14px;font-size:12px;color:#7e718f">'
                     '<label>切换会话 <select aria-label="切换会话" '
                     'style="font:inherit;color:#292137;background:white;border:1px solid #e3daef;'
                     'border-radius:8px;padding:7px 10px;max-width:100%" '
                     'onchange="location.href=this.value">'+choices+'</select></label></nav>')
                path=Path(record['path'])
                content=path.read_text(encoding='utf-8')
                anchor='<div id="header-content">' if '<div id="header-content">' in content else '<header>'
                path.write_text(content.replace(anchor,anchor+nav,1),encoding='utf-8')
        target=quote(paths[0],safe='/')
        # Replace before paint, with a no-JS refresh and blocked-navigation link.
        content=('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                 '<title>'+html.escape(records[0]['name'])+' · Moect</title>'
                 '<meta http-equiv="refresh" content="0;url='+html.escape(target,quote=True)+'">'
                 '<script>location.replace('+json.dumps(target).replace('<','\\u003c')+');</script>'
                 '<a href="'+html.escape(target,quote=True)+'">打开聊天记录</a></html>')
        (folder/'index.html').write_text(content,encoding='utf-8')
        return
