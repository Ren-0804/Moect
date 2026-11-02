"""Synthetic WebView2 desktop regression checks."""
import json
import time


def run(window,api,directory):
    directory.mkdir(parents=True,exist_ok=True)
    svc=api._service;svc.config['export_dir']=str(directory/'exports')
    results=[]
    def evaluate(script):return window.gui.evaluate_js('JSON.stringify(('+script+'))',window.uid,True)
    def check(name,script,timeout=15):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            try:
                if evaluate(script):
                    results.append(name)
                    (directory/'progress.json').write_text(json.dumps(results),encoding='utf-8')
                    return
            except Exception:pass
            time.sleep(.25)
        raise AssertionError('timeout: '+name)
    def capture(name):
        from System import Func,Object
        from System.IO import MemoryStream
        from Microsoft.Web.WebView2.Core import CoreWebView2CapturePreviewImageFormat
        stream=MemoryStream()
        task=window.native.Invoke(Func[Object](lambda:window.native.webview.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,stream)))
        deadline=time.monotonic()+15
        while not task.IsCompleted:
            if time.monotonic()>deadline:raise TimeoutError('CapturePreview')
            time.sleep(.05)
        if task.IsFaulted:raise RuntimeError('CapturePreview failed')
        (directory/(name+'.png')).write_bytes(bytes(stream.ToArray()))
        stream.Dispose()
    try:
        check('ready','window.__ready===true')
        check('font-ready',"document.fonts.check('400 14px \"WCE HarmonyOS Sans\"')")
        capture('01-overview')
        check('maximize',"(document.querySelector('[aria-label=最大化]').click(),true)")
        check('maximized',"!!document.querySelector('[aria-label=还原]')")
        check('restore',"(document.querySelector('[aria-label=还原]').click(),true)")
        check('restored',"!!document.querySelector('[aria-label=最大化]')")
        check('sessions',"(window.__wce.setTab('sessions'),true)")
        check('session-list',"[...document.querySelectorAll('button')].filter(b=>b.textContent.trim()==='导出').length===7")
        capture('02-sessions')
        check('preview',"(window.__wce.showPreview('demo_design@chatroom'),true)")
        check('preview-loaded',"document.body.textContent.includes('周末的天气看起来不错')")
        check('close-preview',"(window.__wce.closePreview(),true)")
        check('show-export',"(window.__wce.chooseItems(['demo_design@chatroom','demo_lin']),true)")
        check('export-dialog',"document.querySelector('[role=dialog]')?.textContent.includes('配置就绪')")
        check('start-export',"([...document.querySelectorAll('button')].find(b=>b.textContent==='开始批量导出').click(),true)")
        check('batch-completed',"window.__wce.state.tasks.filter(t=>t.kind==='export'&&t.status==='completed').length===1")
        check('batch-single-job',"window.__wce.state.tasks.find(t=>t.kind==='export').sessions===2")
        capture('03-tasks')
        check('delete',"(document.querySelector('[title=删除记录]').click(),true)")
        check('confirm-delete',"([...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='删除记录').click(),true)")
        check('deleted',"window.__wce.state.tasks.length===0")
        check('overview',"(window.__wce.setTab('overview'),true)")
        time.sleep(.5);capture('04-overview-task')
        check('theme',"(document.querySelector('[title=切换主题]').click(),true)")
        check('dark',"document.documentElement.classList.contains('dark')")
        time.sleep(.5);capture('05-dark')
        from core.window_chrome import corner_preference
        from System import Func,Object
        def cdp(method,params):
            task=window.native.Invoke(Func[Object](lambda:window.native.webview.CoreWebView2.CallDevToolsProtocolMethodAsync(method,json.dumps(params))))
            deadline=time.monotonic()+5
            while not task.IsCompleted:
                if time.monotonic()>deadline:raise TimeoutError('font diagnostics')
                time.sleep(.03)
            return json.loads(task.Result)
        cdp('DOM.enable',{});cdp('CSS.enable',{})
        doc=cdp('DOM.getDocument',{})
        node=cdp('DOM.querySelector',{'nodeId':doc['root']['nodeId'],'selector':'h2'})
        actual=cdp('CSS.getPlatformFontsForNode',{'nodeId':node['nodeId']})
        if not any(f['isCustomFont'] and 'Harmony' in f['familyName'] for f in actual['fonts']):
            raise AssertionError('Harmony font was not used for rendered Chinese glyphs')
        (directory/'edge-result.json').write_text(json.dumps({'ok':True,'steps':results,
            'renderer':'Microsoft Edge WebView2','dpi':api._frame.devicePixelRatioF(),
            'corner_preference':corner_preference(api._frame.hwnd),
            'actual_fonts':actual['fonts'],
            'browser_scale':evaluate('window.devicePixelRatio'),
            'font':evaluate('getComputedStyle(document.body.firstElementChild.firstElementChild).fontFamily')},ensure_ascii=False,indent=2),encoding='utf-8')
    except Exception as exc:
        (directory/'edge-result.json').write_text(json.dumps({'ok':False,'steps':results,'error':str(exc)},ensure_ascii=False,indent=2),encoding='utf-8')
    finally:window.destroy()
