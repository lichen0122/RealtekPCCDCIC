"""DV_Utility 更新器 (半自動一鍵更新的 helper)。

由 DV_Utility.exe 在使用者按下「一鍵更新並重啟」後啟動; 主程式隨即退出。
本程式負責: 自己從公司 RT NAS 讀已簽章的 version.json (不信任命令列帶來的網址, 也不碰 GCS)
-> 以使用者存好的 NAS 登入下載新版 zip 並驗 sha256 -> 解壓出新 DV_Utility.exe
-> 待舊檔解鎖後就地替換 (舊檔改 .bak) -> 啟動新版 -> 結束。

視窗與主程式同風格 (PySide6, 深色卡片 + Realtek logo + 金黃進度條)。
刻意保持「溫和」以通過 OPSWAT 掃毒: 不改寫自己、不用 DETACHED_PROCESS 旗標、
不改名自己; 只是一個獨立小程式把已不在執行的 exe 換成新版 (等同任何 installer)。
"""

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile

import rtnas
import update_check

# 與主程式 dv_utility.py 同一組主題色 (取自 realtek.png)
_ACCENT = '#DBCB6C'
_ACCENT_INK = '#3A3312'
_ACCENT_HOVER = '#CABA5A'
_ERROR = '#E5534B'
_FONT = 'Microsoft JhengHei'


def parse_args(argv):
    p = argparse.ArgumentParser(description='DV_Utility updater')
    p.add_argument('--target', required=True, help='要被替換並重啟的 DV_Utility.exe 絕對路徑')
    p.add_argument('--version', default='', help='主程式看到的新版本 (只用於顯示)')
    p.add_argument('--parent-pid', type=int, default=0)
    # 舊版主程式會帶這兩個; 一律忽略 —— 下載網址與 sha256 只取自 NAS 上已簽章的 version.json。
    p.add_argument('--zip-url', default='', help=argparse.SUPPRESS)
    p.add_argument('--sha256', default='', help=argparse.SUPPRESS)
    return p.parse_args(argv)


def _bundled_path(name):
    """打包資源路徑 (Nuitka onefile: __file__ 位於解壓目錄; 開發時 = 腳本同層)。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


# --------------------------------------------------------------------------- #
#  更新流程 (不含 GUI, 可單獨測試)
# --------------------------------------------------------------------------- #

def failure_message(exc):
    """把例外轉成給使用者看的一句話 (log 另記完整例外)。"""
    if isinstance(exc, rtnas.AuthError):
        return ('尚未登入公司 RT NAS, 或 NAS 密碼已變更。\n'
                '請在 DV_Utility 開啟任一 NAS 工具重新登入後, 再按一次更新。')
    if isinstance(exc, rtnas.IntegrityError):
        return '發佈檔簽章或 sha256 驗證失敗, 已中止更新。請通知工具維護者。'
    if isinstance(exc, FileNotFoundError):
        return 'NAS 上找不到新版發佈檔, 請通知工具維護者。'
    if isinstance(exc, rtnas.RtnasError):
        return '連不上公司 RT NAS, 請確認電腦在公司內網後再試。'
    if isinstance(exc, PermissionError):
        return 'DV_Utility.exe 仍被占用, 無法替換。請確認 DV_Utility 已完全關閉後再試。'
    return f'更新失敗: {exc}'


def download_zip(info, dest, progress_cb=None, store=None, connect_url=None):
    """以存好的 NAS 登入下載 info['zip_url'] 到 dest, 並比對 info['sha256'] (不符刪檔丟 IntegrityError)。"""
    session, remote = (connect_url or rtnas.connect_url)(info['zip_url'], store=store)
    with session as sftp:
        rtnas.download_to_file(sftp, remote, dest, expected_sha256=info['sha256'],
                               progress_cb=progress_cb)


def extract_main_exe(zip_path, dest_exe):
    """從 zip 取出 DV_Utility.exe 寫到 dest_exe; 找不到則丟 RuntimeError。"""
    with zipfile.ZipFile(zip_path) as z:
        name = next((n for n in z.namelist()
                     if n.lower().rstrip('/').endswith('dv_utility.exe')), None)
        if name is None:
            raise RuntimeError('zip 內找不到 DV_Utility.exe')
        with z.open(name) as src, open(dest_exe, 'wb') as dst:
            shutil.copyfileobj(src, dst)


def swap_with_backup(target_exe, new_exe, attempts=20, delay=0.3):
    """舊檔改 .bak -> 新檔就位; 帶退避重試 (等主程式退出釋放檔案鎖)。

    失敗會自動回滾 (把 .bak 還原成 target) 並把例外往上丟。
    """
    bak = target_exe + '.bak'
    # 1) 把舊檔挪到 .bak (等鎖釋放; 主程式為 onefile, 退出後才解得了鎖)
    for i in range(attempts):
        try:
            if os.path.exists(bak):
                os.remove(bak)
            os.replace(target_exe, bak)
            break
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (i + 1))
    # 2) 新檔就位; 失敗則回滾
    try:
        os.replace(new_exe, target_exe)
    except Exception:
        os.replace(bak, target_exe)
        raise


def relaunch(exe):
    """啟動 DV_Utility (一般子行程, 無特殊旗標)。"""
    subprocess.Popen([exe], cwd=os.path.dirname(exe), close_fds=True)


def log_path(target):
    return os.path.join(os.path.dirname(target), 'dv_updater.log')


def _log(target, msg):
    """寫一行 log 到 target 同層的 dv_updater.log (best-effort)。"""
    try:
        with open(log_path(target), 'a', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + msg + '\n')
    except Exception:
        pass


def _remove_quiet(*paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def run_update(target, status, progress, *, fetch_manifest=None, download=None):
    """執行一次更新; 成功回新版本字串, 失敗丟例外 (呼叫端負責顯示並重啟舊版)。

    status(text): 不確定時長的階段; progress(done, total): 下載進度。
    失敗時清掉暫存; 若 swap 途中 target 被騰空, 盡力從 .bak 還原, 確保使用者仍有 app 可開。
    """
    work_dir = os.path.dirname(target)
    part = os.path.join(work_dir, 'DV_Utility.update.part')   # 下載暫存 (同磁碟區)
    new_exe = os.path.join(work_dir, 'DV_Utility.update.new')  # 解壓出的新 exe (同磁碟區)
    _remove_quiet(part, new_exe)
    try:
        status('讀取 NAS 版本資訊 …')
        info = (fetch_manifest or update_check.fetch_manifest)()
        version = info.get('version', '')
        _log(target, f'manifest OK: {version} {info.get("zip_url")}')
        status(f'下載中 … ({version})')
        (download or download_zip)(info, part, progress_cb=progress)
        status('安裝中 …')
        extract_main_exe(part, new_exe)
        _remove_quiet(part)
        swap_with_backup(target, new_exe)
    except Exception:
        _remove_quiet(part, new_exe)
        bak = target + '.bak'
        if not os.path.isfile(target) and os.path.isfile(bak):
            try:
                os.replace(bak, target)
                _log(target, 'restored target from .bak')
            except OSError:
                _log(target, 'restore from .bak FAILED')
        raise
    return version


# --------------------------------------------------------------------------- #
#  視窗 (PySide6, 與主程式 splash 同一張深色卡片)
# --------------------------------------------------------------------------- #

def _run_gui(args, target):
    from PySide6.QtCore import QObject, Qt, Signal
    from PySide6.QtGui import QFont, QIcon, QPixmap
    from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                                   QProgressBar, QPushButton, QVBoxLayout, QWidget)

    class Signals(QObject):
        status = Signal(str)
        progress = Signal(int, int)
        done = Signal(str)
        failed = Signal(str)

    try:
        from ctypes import windll
        windll.shell32.SetCurrentProcessExplicitAppUserModelID('Realtek.PCDV.DVUtility.Updater')
    except Exception:
        pass

    app = QApplication(sys.argv[:1])
    app.setStyle('Fusion')
    png = _bundled_path('realtek.png')
    if os.path.isfile(png):
        app.setWindowIcon(QIcon(png))

    win = QWidget(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
    win.setWindowTitle('DV Utility 更新')
    win.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    win.setFixedSize(440, 200)

    card = QFrame(win)
    card.setObjectName('card')
    card.setGeometry(0, 0, 440, 200)
    card.setStyleSheet(
        '#card { background-color: #2B2B2B; border: 1px solid #3F3F46; border-radius: 12px; }'
        'QLabel { background: transparent; color: #FFFFFF; }'
        'QProgressBar { background-color: #48484A; border: none; border-radius: 4px; }'
        'QProgressBar::chunk { background-color: %s; border-radius: 4px; }'
        'QPushButton { background-color: %s; color: %s; border: none; border-radius: 4px;'
        '              padding: 5px 16px; }'
        'QPushButton:hover { background-color: %s; }'
        % (_ACCENT, _ACCENT, _ACCENT_INK, _ACCENT_HOVER))
    v = QVBoxLayout(card)
    v.setContentsMargins(24, 20, 24, 20)
    v.setSpacing(10)

    header = QHBoxLayout()
    pix = QPixmap(png)
    if not pix.isNull():
        logo = QLabel()
        logo.setPixmap(pix.scaledToHeight(36, Qt.TransformationMode.SmoothTransformation))
        header.addWidget(logo)
        header.addSpacing(8)
    title = QLabel('DV Utility 更新')
    title.setFont(QFont(_FONT, 13, QFont.Weight.Bold))
    header.addWidget(title)
    header.addStretch()
    ver = QLabel(args.version)
    ver.setFont(QFont(_FONT, 10))
    ver.setStyleSheet('color: #A0A0A0;')
    header.addWidget(ver)
    v.addLayout(header)

    status = QLabel('準備中 …')
    status.setFont(QFont(_FONT, 10))
    status.setWordWrap(True)
    v.addWidget(status)

    bar = QProgressBar()
    bar.setTextVisible(False)
    bar.setFixedHeight(8)
    bar.setRange(0, 0)          # 跑馬燈, 下載開始後改百分比
    v.addWidget(bar)

    detail = QLabel(' ')
    detail.setFont(QFont(_FONT, 9))
    detail.setStyleSheet('color: #A0A0A0;')
    detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    detail.setWordWrap(True)
    v.addWidget(detail)

    button_row = QHBoxLayout()
    button_row.addStretch()
    close_btn = QPushButton('開啟目前版本')
    close_btn.setFont(QFont(_FONT, 10))
    close_btn.hide()
    button_row.addWidget(close_btn)
    v.addLayout(button_row)
    v.addStretch()

    screen = app.primaryScreen()
    if screen is not None:
        geo = screen.availableGeometry()
        win.move(geo.center().x() - win.width() // 2, geo.center().y() - win.height() // 2)

    sig = Signals()
    last_pct = [-1]

    def on_status(text):
        status.setText(text)
        detail.setText(' ')
        bar.setRange(0, 0)

    def on_progress(done, total):
        if total <= 0:
            return
        pct = min(100, done * 100 // total)
        if pct == last_pct[0]:
            return
        last_pct[0] = pct
        bar.setRange(0, 100)
        bar.setValue(pct)
        mb = 1024 * 1024
        detail.setText(f'{pct}%   {done / mb:.1f} / {total / mb:.1f} MB')

    def on_done(version):
        _log(target, f'update OK -> {version}')
        try:
            relaunch(target)
        except Exception as e:
            _log(target, f'relaunch new exe FAILED: {e!r}')
        app.quit()

    def on_failed(message):
        status.setText(message)
        status.setStyleSheet(f'color: {_ERROR};')
        bar.setRange(0, 100)
        bar.setValue(0)
        detail.setText(f'詳細記錄檔: {log_path(target)}')
        close_btn.show()

    def reopen_old():
        try:
            if os.path.isfile(target):
                relaunch(target)
        except Exception as e:
            _log(target, f'relaunch old exe FAILED: {e!r}')
        app.quit()

    sig.status.connect(on_status)
    sig.progress.connect(on_progress)
    sig.done.connect(on_done)
    sig.failed.connect(on_failed)
    close_btn.clicked.connect(reopen_old)

    def worker():
        time.sleep(0.5)          # 給主程式一點時間完成退出 (釋放 exe 檔案鎖)
        try:
            version = run_update(target, sig.status.emit, sig.progress.emit)
        except Exception as e:
            _log(target, f'update FAILED: {e!r}')
            sig.failed.emit(failure_message(e))
            return
        sig.done.emit(version)

    win.show()
    threading.Thread(target=worker, daemon=True).start()
    return app.exec()


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    target = os.path.realpath(args.target)
    _log(target, f'updater start (target={target}, shown version={args.version})')
    sys.exit(_run_gui(args, target))


if __name__ == '__main__':
    main()
