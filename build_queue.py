"""Build the Windows onedir bundle without the Qt-only build dependencies.

`build.py` renders the icon and the version resource through PySide6, which the
shipped WebView2 host does not need. This variant reuses the committed
`icon.ico` so the queue patch can be rebuilt with only the runtime deps.
"""
import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).parent
icon = root / 'icon.ico'
if not icon.is_file():
    raise SystemExit('icon.ico is missing; run build.py once on a machine with PySide6.')

# Dependency scanning must not pick unrelated ICU/OpenSSL DLLs from other
# applications on PATH. Qt uses Windows' system ICU API on this platform.
env = os.environ.copy()
win = Path(env.get('SystemRoot', r'C:\Windows'))
env['PATH'] = os.pathsep.join([str(Path(sys.executable).parent), str(Path(sys.base_prefix)),
                               str(win / 'System32'), str(win)])
command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
           '--windowed', '--onedir', '--name', 'Moect', '--icon', str(icon),
           '--manifest', str(root / 'windows.manifest'),
           '--add-data', 'ui-qce;ui-qce', '--add-data', 'ui/viewer.html;ui',
           '--add-data', 'LICENSE;.', '--add-data', 'THIRD_PARTY_NOTICES.md;.',
           '--add-data', 'THIRD_PARTY_LICENSES;THIRD_PARTY_LICENSES',
           '--exclude-module', 'PyQt5', '--exclude-module', 'PyQt6',
           '--exclude-module', 'PySide6',
           '--exclude-module', 'tkinter', '--exclude-module', 'wx4.gui',
           '--exclude-module', 'wx4.cli', '--exclude-module', 'wx4.export',
           '--exclude-module', 'pytest',
           '--collect-all', 'webview', '--collect-all', 'pythonnet',
           '--collect-all', 'clr_loader', '--collect-all', 'imageio_ffmpeg',
           '--hidden-import', 'webview.platforms.edgechromium',
           '--hidden-import', 'webview.platforms.winforms',
           'edge_app.py']
subprocess.run(command, cwd=root, env=env, check=True)
print('built:', root / 'dist' / 'Moect')
