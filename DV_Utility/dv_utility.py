import sys
from ctypes import windll
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QComboBox, QListView, QPushButton,
    QProgressBar, QGridLayout, QVBoxLayout, QHBoxLayout, QFrame, QFileDialog,
    QMessageBox, QTableWidget, QTableWidgetItem, QHeaderView
)
from PySide6.QtCore import Qt, Signal, QObject, QTimer
from PySide6.QtGui import QFont, QIcon, QShortcut, QKeySequence, QPixmap
import threading
import requests
import json
import subprocess
import zipfile
from pathlib import Path
import os
import logging
from logging.handlers import RotatingFileHandler
import update_check


# --------------------------------------------------------------------------- #
#  Logging
#  打包成 exe 後沒有 console, print() 全看不到; 改用檔案 log 協助遠端 debug
#  (例如下載卡死 / 網路逾時 / 解壓失敗 / 子程序啟動失敗)。
#  log 位置: USER_HOME/.PCDV/DVUtility/dv_utility.log (輪替保留數份)。
# --------------------------------------------------------------------------- #

# requests 逾時 (秒): (連線逾時, 讀取逾時)。
# 讀取逾時是「兩次收到資料之間」的上限 —— 對 stream 下載而言, 一旦卡住不再有資料
# 就會丟出 ReadTimeout, 不會無限卡死 (這是「下載卡死」最主要的元兇: 原本沒有逾時)。
_CONNECT_TIMEOUT = 15
_READ_TIMEOUT    = 60
_HTTP_TIMEOUT    = (_CONNECT_TIMEOUT, _READ_TIMEOUT)

# 下載進度每累積這麼多 bytes 就記一次 log, 用來觀察下載卡在哪個進度。
_LOG_EVERY_BYTES = 5 * 1024 * 1024


def _get_log_dir():
    """log 目錄: USER_HOME/.PCDV/DVUtility/。

    建立失敗 (權限 / 唯讀家目錄等) 則退回系統暫存目錄, 仍不讓程式因記 log 而崩潰。
    """
    log_dir = Path.home() / '.PCDV' / 'DVUtility'
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir
    except Exception:
        import tempfile
        fallback = Path(tempfile.gettempdir()) / 'PCDV_DVUtility'
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return fallback


def _setup_logging():
    """初始化並回傳共用 logger; 重複呼叫不會重覆掛 handler。"""
    logger = logging.getLogger('DVUtility')
    if logger.handlers:            # 已初始化過
        return logger
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    fmt = logging.Formatter(
        '%(asctime)s [%(levelname)s] (%(threadName)s) %(funcName)s:%(lineno)d - %(message)s'
    )

    log_file = _get_log_dir() / 'dv_utility.log'
    try:
        # 單檔 2MB, 保留 5 份, 避免 log 無限膨脹
        fh = RotatingFileHandler(str(log_file), maxBytes=2 * 1024 * 1024,
                                 backupCount=5, encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass

    # 開發模式 (python dv_utility.py) 也同步輸出到 console。
    # 打包成 windowed exe 時 sys.stderr 為 None, 掛上去只會每筆 log 觸發並吞掉 AttributeError,
    # 是無意義的負擔; 故僅在真的有 stderr 時才加。
    if sys.stderr is not None:
        try:
            sh = logging.StreamHandler()
            sh.setLevel(logging.INFO)
            sh.setFormatter(fmt)
            logger.addHandler(sh)
        except Exception:
            pass

    return logger


def _install_excepthook():
    """把未攔截的例外 (主執行緒 + 各 worker thread) 都寫進 log。

    下載 / 檢查工具更新都跑在背景執行緒, 未攔截例外原本會讓該 thread 靜默死掉
    (在打包 exe 裡連 stderr 都看不到), 介面就這樣卡住。攔下來記 log 才有辦法追。
    """
    prev = sys.excepthook

    def _hook(exc_type, exc, tb):
        try:
            log.critical('Uncaught exception on main thread',
                         exc_info=(exc_type, exc, tb))
        except Exception:
            pass
        prev(exc_type, exc, tb)

    sys.excepthook = _hook

    def _thread_hook(args):
        try:
            log.critical('Uncaught exception on thread %s',
                         getattr(args.thread, 'name', '?'),
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        except Exception:
            pass

    try:
        threading.excepthook = _thread_hook   # Python 3.8+
    except Exception:
        pass


log = _setup_logging()
_install_excepthook()


def get_bundled_path(filename):
    """取得打包後或開發時的資源檔路徑"""
    if hasattr(sys, '_MEIPASS'):
        # PyInstaller onefile/standalone
        return os.path.join(sys._MEIPASS, filename)
    if globals().get('__compiled__'):
        # Nuitka standalone/onefile: __file__ points to the actual extraction dir
        return os.path.join(os.path.dirname(__file__), filename)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)


def _read_app_version():
    """App 版本: 由 version.json 定義 (release.py 進版時寫入, 並一起打包進 exe)。"""
    try:
        with open(get_bundled_path('version.json'), encoding='utf-8') as f:
            return json.load(f).get('version', 'vUNKNOWN')
    except Exception:
        log.warning('could not read version.json, using vUNKNOWN', exc_info=True)
        return 'vUNKNOWN'


class WorkerSignals(QObject):
    update_status      = Signal(str)
    update_progress    = Signal(float, float)
    update_release     = Signal(str, str)
    show_status_frame  = Signal()
    hide_status_frame  = Signal()
    set_btn_enabled    = Signal(bool)
    process_added      = Signal(object)   # 攜帶新 process 紀錄, 於 GUI thread append
    update_available   = Signal(object)   # 更新檢查: 發現新版 (攜帶 manifest info dict)


class AutoUpdateGUI(QMainWindow):
    url       = ''
    version   = _read_app_version()

    # 下載/安裝進行中 (download_file worker thread 寫, closeEvent 於 GUI thread 讀)
    _install_busy = False

    # 右側面板寬度 (展開 / 縮合)。展開寬度須 >= 表格 minimumWidth(360) + 內距留白
    PANEL_EXPANDED_W  = 400
    PANEL_COLLAPSED_W = 36

    def __init__(self, app):
        super().__init__()
        self.app       = app
        self.signals   = WorkerSignals()
        # 每筆紀錄為 {'popen': Popen, 'label': str}
        self.processes = []

        log.info('AutoUpdateGUI init (version=%s)', self.version)
        self.ensure_resource_files()

        try:
            self.setting = self.load_setting()
            log.info('loaded setting.json (%d tools)', len(self.setting))
        except Exception:
            log.exception('failed to load setting.json: %s', self.setting_file)
            raise

        self.init_window()
        self.connect_signals()

        # 背景檢查是否有新版 (僅打包執行; 純 JSON 讀取, 無下載/執行行為); 有更新則通知 GUI
        self._update_info = None
        threading.Thread(target=self._check_update, daemon=True).start()

    # ------------------------------------------------------------------ #
    #  Resource / settings
    # ------------------------------------------------------------------ #
    def ensure_resource_files(self):
        self.resource           = f'{self.get_install_dir()}/resource'
        self.setting_file       = f'{self.resource}/setting.json'
        self.ico_file           = get_bundled_path('realtek.png')
        self.work_dir_list_file = f'{self.resource}/work_dir_list.json'
        self.tool_history_file  = f'{self.resource}/tool_history.json'

        log.info('resource dir: %s', self.resource)
        if not os.path.exists(self.resource):
            try:
                os.mkdir(self.resource)
            except Exception:
                log.exception('failed to create resource dir: %s', self.resource)
                raise

        # 產生下拉箭頭 (macOS 風格雙 chevron) 資產, 供 QComboBox QSS 使用
        self._ensure_chevron_asset()

        # 啟動載入畫面 (見 __main__ 的 create_loading_splash) 已覆蓋此處的下載等待,
        # 故同步下載即可, 不再另開進度條 splash。
        url = 'https://raw.github.com/lichen0122/RealtekPCCDCIC/main/dv_util_resource/setting.json'
        try:
            self.download_from_git(url, self.setting_file)
        except Exception:
            log.exception('ensure_resource_files: failed to refresh setting.json (url=%s)', url)
            if not os.path.exists(self.setting_file):
                log.critical('setting.json missing and download failed; startup cannot continue')
                raise
            log.warning('ensure_resource_files: falling back to existing setting.json from a previous run')

        if not os.path.exists(self.work_dir_list_file):
            with open(self.work_dir_list_file, 'w') as f:
                json.dump([], f)

        if not os.path.exists(self.tool_history_file):
            with open(self.tool_history_file, 'w') as f:
                json.dump("", f)

    def load_setting(self):
        # setting.json 由 download_from_git 以 UTF-8 原始 bytes 落地; 讀取須明示編碼 —
        # zh-TW 機器預設 cp950, 內容一旦出現中文就會 UnicodeDecodeError (啟動失敗) 或亂碼
        with open(self.setting_file, "r", encoding="utf-8") as f:
            return json.loads(f.read())

    def get_install_dir(self):
        home_path = Path.home() / 'PCDV'
        home_path.mkdir(exist_ok=True)
        return str(home_path)

    def _ensure_chevron_asset(self):
        """寫出下拉選單指示 icon (macOS 風格圓角實心倒三角) SVG, 供 QComboBox down-arrow 使用。"""
        self.chevron_svg = f'{self.resource}/macos_chevrons.svg'
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="14" viewBox="0 0 12 14">'
            '<path d="M3.6 5.9 L8.4 5.9 L6 9.2 Z" fill="#FFFFFF" stroke="#FFFFFF" '
            'stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"/>'
            '</svg>'
        )
        self._chevron_ok = False
        try:
            with open(self.chevron_svg, 'w', encoding='utf-8') as f:
                f.write(svg)
            self._chevron_ok = True
        except Exception:
            pass
        # QSS url() 一律使用正斜線
        self.chevron_svg_url = self.chevron_svg.replace('\\', '/')

    # ------------------------------------------------------------------ #
    #  Window / UI init
    # ------------------------------------------------------------------ #
    def _apply_macos_popup(self, combo):
        """下拉選單彈出視窗: 一次最多顯示 10 項, 超過則出現 macOS 風格捲軸;
        長路徑以中間省略 (…) 取代水平捲軸, 維持乾淨的 macOS 外觀。
        須搭配 QSS 的 combobox-popup:0 (強制非原生彈窗, 10 項上限才會生效);
        另換上全新的 QListView, 否則 QComboBox 內建容器會把捲軸藏起來。"""
        combo.setMaxVisibleItems(10)
        combo.setView(QListView())
        view = combo.view()
        view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        # 開啟滑鼠追蹤, QSS 的 ::item:hover 才會在游標移過選項時即時觸發
        view.setMouseTracking(True)

    def init_window(self):
        self.setWindowTitle(f'PCDV DV Utility {self.version}')
        icon = QIcon(self.ico_file)
        self.setWindowIcon(icon)
        self.app.setWindowIcon(icon)
        self.resize(820, 230)

        default_font = QFont("Microsoft JhengHei", 11)
        self.default_font = default_font

        central = QWidget()
        self.setCentralWidget(central)
        # 深色主題: 主 panel 與 process 追蹤 panel 共用同一深色底 + 白色字
        # (套在 central 上, 不影響 QMessageBox 等原生對話框)
        # chevron 圖檔寫入成功才注入 down-arrow image 規則; 否則留給 Fusion 畫預設箭頭
        arrow_rule = (
            "QComboBox::down-arrow { image: url(" + self.chevron_svg_url + "); width: 12px; height: 14px; }"
            if getattr(self, "_chevron_ok", False) else ""
        )
        central.setStyleSheet(
            "QWidget { background-color: #2B2B2B; color: #FFFFFF; }"
            "QLabel { background: transparent; color: #FFFFFF; }"
            # ---- macOS 風格下拉選單 (pop-up button): 圓角控制項 + 藍色 chevron 方塊 + 圓角浮層 ----
            # combobox-popup:0 強制非原生彈窗, maxVisibleItems(10) 才會生效 (Fusion 預設為原生彈窗會忽略它)
            "QComboBox { background-color: #3A3A3C; color: #FFFFFF; border: 1px solid #48484A;"
            "           border-radius: 6px; padding: 4px 34px 4px 10px; min-height: 22px;"
            "           combobox-popup: 0; }"
            "QComboBox:hover { background-color: #444446; }"
            "QComboBox:on { border: 1px solid #0A84FF; }"
            "QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right;"
            "           width: 22px; margin: 4px; border-radius: 5px; background-color: #0A84FF; }"
            + arrow_rule +
            "QComboBox QAbstractItemView { background-color: #2E2E30; color: #FFFFFF;"
            "           border: 1px solid #48484A; border-radius: 8px; padding: 4px; outline: none;"
            "           selection-background-color: #0A84FF; selection-color: #FFFFFF; }"
            "QComboBox QAbstractItemView::item { border-radius: 5px; padding: 5px 8px; min-height: 22px; }"
            # 滑鼠移到選項上 / 鍵盤移動的目前項目: 藍底白字, 讓使用者點下前就看得出會選到哪一項
            # (::item 一旦被自訂樣式接管, 上面 view 層的 selection-background-color 就不會生效, 需在此明確指定)
            "QComboBox QAbstractItemView::item:hover { background-color: #0A84FF; color: #FFFFFF; }"
            "QComboBox QAbstractItemView::item:selected { background-color: #0A84FF; color: #FFFFFF; }"
            # ---- macOS 風格浮層捲軸 (僅作用於下拉選單彈出視窗, 不影響右側表格) ----
            "QComboBox QAbstractItemView QScrollBar:vertical {"
            "           background: transparent; width: 10px; margin: 4px 3px 4px 0px; border: none; }"
            "QComboBox QAbstractItemView QScrollBar::handle:vertical {"
            "           background: rgba(255, 255, 255, 0.22); min-height: 28px;"
            "           border: none; border-radius: 3px; margin: 0px 2px 0px 2px; }"
            "QComboBox QAbstractItemView QScrollBar::handle:vertical:hover {"
            "           background: rgba(255, 255, 255, 0.38); }"
            "QComboBox QAbstractItemView QScrollBar::handle:vertical:pressed {"
            "           background: #0A84FF; }"
            "QComboBox QAbstractItemView QScrollBar::add-line:vertical,"
            "QComboBox QAbstractItemView QScrollBar::sub-line:vertical {"
            "           height: 0px; width: 0px; background: none; border: none; }"
            "QComboBox QAbstractItemView QScrollBar::up-arrow:vertical,"
            "QComboBox QAbstractItemView QScrollBar::down-arrow:vertical {"
            "           background: none; border: none; image: none; width: 0px; height: 0px; }"
            "QComboBox QAbstractItemView QScrollBar::add-page:vertical,"
            "QComboBox QAbstractItemView QScrollBar::sub-page:vertical {"
            "           background: none; }"
            # ---- 表格 ----
            "QTableWidget { background-color: #2B2B2B; color: #FFFFFF; gridline-color: #3F3F46; border: none; }"
            "QTableWidget::item:selected { background-color: #0A84FF; color: #FFFFFF; }"
            "QHeaderView::section { background-color: #3C3C3C; color: #FFFFFF; border: none;"
            "           padding: 4px; font-weight: bold; }"
            "QTableCornerButton::section { background-color: #3C3C3C; border: none; }"
            # ---- macOS 風格膠囊進度條 ----
            "QProgressBar { background-color: #48484A; border: none; border-radius: 4px;"
            "           text-align: center; color: #FFFFFF; }"
            "QProgressBar::chunk { background-color: #0A84FF; border-radius: 4px; }"
            "QToolTip { background-color: #1E1E1E; color: #FFFFFF; border: 1px solid #3F3F46; }"
        )
        self.outer_layout = QHBoxLayout(central)
        self._margin = 10
        self._gap    = 8
        self.outer_layout.setContentsMargins(self._margin, self._margin, self._margin, self._margin)
        self.outer_layout.setSpacing(0)
        outer_layout = self.outer_layout

        # 左側主要內容: 固定寬度, 使縮合 / 展開右側面板時其大小永遠不變
        # (右側面板以 stretch 吸收所有寬度變化, 左側被釘死故不會被擠壓或拉伸)
        self.LEFT_W = 820 - 2 * self._margin   # 初始視窗 820 扣掉左右邊距
        left_widget = QWidget()
        left_widget.setFixedWidth(self.LEFT_W)
        main_layout = QVBoxLayout(left_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(6)

        # ---- top frame ----
        self.top_frame = QFrame()
        grid = QGridLayout(self.top_frame)
        grid.setSpacing(5)

        self.path_label = QLabel("請選擇 project 路徑:")
        self.path_label.setFont(default_font)
        grid.addWidget(self.path_label, 0, 0)

        self.get_work_dir_list()
        self.choose_work_dir = QComboBox()
        self.choose_work_dir.setFont(default_font)
        self.choose_work_dir.setMinimumWidth(500)
        self.choose_work_dir.addItems(self.work_dir_list)
        self._apply_macos_popup(self.choose_work_dir)
        # activated fires only on user interaction (like <<ComboboxSelected>>)
        self.choose_work_dir.activated.connect(self._on_work_dir_activated)
        grid.addWidget(self.choose_work_dir, 0, 1)

        self.choose_dir_button = QPushButton("選擇路徑")
        self.choose_dir_button.setFont(default_font)
        self.choose_dir_button.setStyleSheet(
            "QPushButton { background-color: #0078D4; color: white; border: none; border-radius: 4px; padding: 4px 12px; }"
            "QPushButton:hover { background-color: #106EBE; }"
            "QPushButton:pressed { background-color: #005A9E; }"
            "QPushButton:disabled { background-color: #A0A0A0; }"
        )
        self.choose_dir_button.clicked.connect(self.user_choose_work_dir)
        grid.addWidget(self.choose_dir_button, 0, 2)

        self.tool_label = QLabel("請選擇 utility 工具:")
        self.tool_label.setFont(default_font)
        grid.addWidget(self.tool_label, 1, 0)

        self.tool_option_list = list(self.setting.keys())
        self.choose_tool = QComboBox()
        self.choose_tool.setFont(default_font)
        self.choose_tool.setMinimumWidth(500)
        self.choose_tool.addItems(self.tool_option_list)
        self._apply_macos_popup(self.choose_tool)
        self.get_tool_history()
        if self.tool_history and self.tool_history in self.tool_option_list:
            self.choose_tool.setCurrentIndex(self.tool_option_list.index(self.tool_history))
        grid.addWidget(self.choose_tool, 1, 1)

        self.start_button = QPushButton("開啟程式")
        self.start_button.setFont(default_font)
        self.start_button.setStyleSheet(
            "QPushButton { background-color: #0078D4; color: white; border: none; border-radius: 4px; padding: 4px 12px; }"
            "QPushButton:hover { background-color: #106EBE; }"
            "QPushButton:pressed { background-color: #005A9E; }"
            "QPushButton:disabled { background-color: #A0A0A0; }"
        )
        self.start_button.clicked.connect(self.start_update)
        grid.addWidget(self.start_button, 1, 2)

        main_layout.addWidget(self.top_frame)

        # ---- status frame (hidden initially) ----
        self.status_frame = QFrame()
        status_layout = QVBoxLayout(self.status_frame)
        status_layout.setContentsMargins(0, 0, 0, 0)

        self.progress = QProgressBar()
        self.progress.setMinimum(0)
        self.progress.setMaximum(100)
        self.progress.setMinimumWidth(325)
        self.progress.setTextVisible(False)   # macOS 膠囊進度條不顯示百分比文字
        self.progress.setFixedHeight(8)
        status_layout.addWidget(self.progress, alignment=Qt.AlignmentFlag.AlignCenter)

        self.status_label = QLabel("")
        self.status_label.setFont(default_font)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status_layout.addWidget(self.status_label)

        self.status_frame.hide()
        main_layout.addWidget(self.status_frame)

        # ---- release note frame (hidden initially) ----
        self.release_note_frame = QFrame()
        rn_layout = QVBoxLayout(self.release_note_frame)
        rn_layout.setContentsMargins(0, 0, 0, 0)

        self.version_label = QLabel("")
        self.version_label.setFont(default_font)
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rn_layout.addWidget(self.version_label)

        self.release_note_label = QLabel("")
        self.release_note_label.setFont(default_font)
        self.release_note_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rn_layout.addWidget(self.release_note_label)

        self.release_note_frame.hide()
        main_layout.addWidget(self.release_note_frame)

        # ---- 更新提示橫幅 (預設隱藏; 發現新版時由 _slot_update_available 顯示) ----
        self.update_frame = QFrame()
        uf_layout = QVBoxLayout(self.update_frame)
        self.update_label = QLabel("")
        self.update_label.setFont(default_font)
        self.update_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.update_label.setWordWrap(True)
        uf_layout.addWidget(self.update_label)
        self.update_button = QPushButton("一鍵更新並重啟")
        self.update_button.setFont(default_font)
        self.update_button.clicked.connect(self.on_update_clicked)
        uf_layout.addWidget(self.update_button)
        self.update_frame.hide()
        main_layout.addWidget(self.update_frame)

        main_layout.addStretch()

        # 版面: [左側(固定寬)] [固定間距] [彈性 stretch] [右側面板(固定寬)]
        #  - 左側固定寬 -> 縮合/展開面板永遠不改變其大小 (硬性需求)
        #  - 中間 stretch 吸收所有寬度差 (面板縮放、視窗縮放、最大化皆然), 左側不受影響
        #  - 另以 _sync_panel 將視窗縮放成剛好容納內容, 一般情況下不會有空隙
        outer_layout.addWidget(left_widget)
        outer_layout.addSpacing(self._gap)
        outer_layout.addStretch(1)

        # ---- 右側「執行中的程式」面板 (VS Code 風格可縮合側欄) ----
        #  panel_collapsed : 使用者意圖 (展開 / 縮合), 會跨 refresh 保留
        #  _panel_state    : 目前版面狀態 'hidden' / 'collapsed' / 'expanded'
        self.panel_collapsed = False
        self._panel_state    = 'hidden'

        self.process_frame = QFrame()
        self.process_frame.setObjectName("processFrame")
        self.process_frame.setStyleSheet(
            "#processFrame { background-color: #2B2B2B; border: 1px solid #3F3F46; border-radius: 6px; }"
        )
        panel_layout = QVBoxLayout(self.process_frame)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(0)

        # ---- 展開狀態: 標題列 (標題 + 縮合鈕) + 表格 ----
        self.expanded_container = QWidget()
        exp_layout = QVBoxLayout(self.expanded_container)
        exp_layout.setContentsMargins(0, 0, 0, 0)
        exp_layout.setSpacing(0)

        panel_header = QWidget()
        hdr_layout = QHBoxLayout(panel_header)
        hdr_layout.setContentsMargins(10, 6, 6, 6)
        hdr_layout.setSpacing(6)

        self.process_title = QLabel("執行中的程式")
        self.process_title.setFont(default_font)
        hdr_layout.addWidget(self.process_title)
        hdr_layout.addStretch()

        self.collapse_btn = QPushButton("‹")   # 向左收合
        self.collapse_btn.setFont(QFont("Microsoft JhengHei", 13))
        self.collapse_btn.setFixedSize(24, 24)
        self.collapse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.collapse_btn.setToolTip("縮合面板")
        self.collapse_btn.setStyleSheet(
            "QPushButton { background-color: transparent; border: none; border-radius: 4px; color: #FFFFFF; }"
            "QPushButton:hover { background-color: #3E3E42; }"
        )
        self.collapse_btn.clicked.connect(self.toggle_process_panel)
        hdr_layout.addWidget(self.collapse_btn)
        exp_layout.addWidget(panel_header)

        self.process_table = QTableWidget(0, 3)
        self.process_table.setHorizontalHeaderLabels(["Tool Name", "Project 路徑", "操作"])
        self.process_table.setFont(default_font)
        self.process_table.verticalHeader().setVisible(False)
        self.process_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.process_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.process_table.setMinimumWidth(360)
        self.process_table.setStyleSheet("QTableWidget { border: none; background: transparent; }")
        header = self.process_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        exp_layout.addWidget(self.process_table)

        panel_layout.addWidget(self.expanded_container)

        # ---- 縮合狀態: 細長 rail (展開鈕 + 數量徽章) ----
        self.collapsed_rail = QWidget()
        rail_layout = QVBoxLayout(self.collapsed_rail)
        rail_layout.setContentsMargins(2, 6, 2, 6)
        rail_layout.setSpacing(6)

        self.expand_btn = QPushButton("›")   # 向右展開
        self.expand_btn.setFont(QFont("Microsoft JhengHei", 13))
        self.expand_btn.setFixedSize(28, 24)
        self.expand_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.expand_btn.setToolTip("展開執行中的程式面板")
        self.expand_btn.setStyleSheet(
            "QPushButton { background-color: transparent; border: none; border-radius: 4px; color: #FFFFFF; }"
            "QPushButton:hover { background-color: #3E3E42; }"
        )
        self.expand_btn.clicked.connect(self.toggle_process_panel)
        rail_layout.addWidget(self.expand_btn, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.rail_badge = QLabel("0")
        self.rail_badge.setFixedSize(24, 24)
        self.rail_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rail_badge.setToolTip("執行中的程式數量")
        self.rail_badge.setStyleSheet(
            "QLabel { background-color: #0078D4; color: white; border-radius: 12px; font: bold 10pt 'Microsoft JhengHei'; }"
        )
        rail_layout.addWidget(self.rail_badge, alignment=Qt.AlignmentFlag.AlignHCenter)
        rail_layout.addStretch()

        self.collapsed_rail.hide()
        panel_layout.addWidget(self.collapsed_rail)

        self.process_frame.hide()
        outer_layout.addWidget(self.process_frame)

        # 定期輪詢 process 狀態, 自動移除已結束的項目
        self.process_timer = QTimer(self)
        self.process_timer.timeout.connect(self.refresh_process_panel)
        self.process_timer.start(1500)

        # ---- hotkey ----
        shortcut = QShortcut(QKeySequence("Ctrl+M"), self)
        shortcut.activated.connect(self.on_ctrl_m)

    def connect_signals(self):
        self.signals.update_status.connect(self.status_label.setText)
        self.signals.update_progress.connect(self._slot_update_progress)
        self.signals.update_release.connect(self._slot_update_release)
        self.signals.show_status_frame.connect(self._slot_show_status_frame)
        self.signals.hide_status_frame.connect(self.status_frame.hide)
        self.signals.set_btn_enabled.connect(self.start_button.setEnabled)
        # queued connection (worker thread -> GUI thread): append + 刷新都在 GUI thread
        self.signals.process_added.connect(self._add_process)
        # 更新檢查 (worker thread -> GUI thread)
        self.signals.update_available.connect(self._slot_update_available)

    # ------------------------------------------------------------------ #
    #  Signal slots (run on main thread)
    # ------------------------------------------------------------------ #
    def _slot_update_progress(self, downloaded, total):
        percent = int((downloaded / total) * 100)
        self.progress.setValue(percent)

    def _slot_update_release(self, version_text, note_text):
        self.version_label.setText(version_text)
        self.release_note_label.setText(note_text)

    def _slot_show_status_frame(self):
        self.status_frame.show()
        self.release_note_frame.show()

    # ------------------------------------------------------------------ #
    #  Update check (背景 thread) —— 只讀 JSON, 不下載/不執行
    # ------------------------------------------------------------------ #
    def _check_update(self):
        """背景執行緒: 比對 GCS 最新版本, 有新版就通知 GUI thread 顯示更新按鈕。"""
        try:
            info = update_check.check_latest_version(self.version)
            if info:
                log.info('update-check: new version available: %s', info.get('version'))
                self.signals.update_available.emit(info)
            else:
                log.info('update-check: no update (or not packaged)')
        except Exception:
            log.exception('update-check failed')

    def _slot_update_available(self, info):
        """GUI thread: 顯示更新橫幅 (版本 + release note) 與一鍵按鈕。"""
        self._update_info = info
        latest = info.get('version', '')
        note = info.get('release_note', '')
        text = f"發現新版本 {latest} (目前 {self.version})"
        if note:
            text += f"\n{note}"
        self.update_label.setText(text)
        self.update_frame.show()

    def on_update_clicked(self):
        """啟動 dv_updater.exe (下載/換裝/重啟由它負責), 隨即結束本程式讓它替換。"""
        info = self._update_info
        target = update_check.launcher_exe()
        if not info or not target:
            QMessageBox.warning(self, "無法更新",
                                "此為開發模式或找不到可更新的執行檔 (需打包後執行)。")
            return
        updater = os.path.join(os.path.dirname(target), 'dv_updater.exe')
        if not os.path.isfile(updater):
            QMessageBox.warning(self, "無法更新", f"找不到更新器:\n{updater}")
            return
        args = [
            updater,
            '--version', info.get('version', ''),
            '--zip-url', info.get('zip_url', ''),
            '--sha256', info.get('sha256', ''),
            '--target', target,
            '--parent-pid', str(os.getpid()),
        ]
        try:
            # 一般子行程 (無 detached 旗標); Windows 上子行程於父行程退出後仍繼續執行。
            subprocess.Popen(args, cwd=os.path.dirname(target), close_fds=True)
        except Exception as e:
            log.exception('failed to launch updater')
            QMessageBox.warning(self, "無法更新", f"啟動更新器失敗:\n{e}")
            return
        log.info('updater launched (target=%s); quitting for swap', target)
        self.app.quit()

    # ------------------------------------------------------------------ #
    #  Hotkey
    # ------------------------------------------------------------------ #
    def on_ctrl_m(self):
        try:
            subprocess.Popen(['explorer', self.get_install_dir()])
        except Exception:
            log.exception('on_ctrl_m: failed to open install dir in explorer')

    # ------------------------------------------------------------------ #
    #  File helpers
    # ------------------------------------------------------------------ #
    def download_from_git(self, url, output, progress_cb=None):
        log.info('download_from_git start: %s -> %s', url, output)
        try:
            with requests.get(url, stream=True, timeout=_HTTP_TIMEOUT) as r:
                r.raise_for_status()
                try:
                    total_length = int(r.headers.get('content-length'))
                except Exception:
                    total_length = 36408565
                downloaded = 0
                next_mark  = _LOG_EVERY_BYTES
                with open(output, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            downloaded += len(chunk)
                            f.write(chunk)
                            if progress_cb:
                                progress_cb(downloaded, total_length)
                            if downloaded >= next_mark:
                                log.info('download_from_git progress: %d/%d bytes (%s)',
                                         downloaded, total_length, url)
                                next_mark += _LOG_EVERY_BYTES
            log.info('download_from_git done: %d bytes -> %s', downloaded, output)
        except Exception:
            log.exception('download_from_git FAILED: %s -> %s', url, output)
            raise

    def get_work_dir_list(self):
        self.work_dir_list = []
        if os.path.exists(self.work_dir_list_file):
            try:
                with open(self.work_dir_list_file, 'r') as f:
                    self.work_dir_list = json.load(f)
            except Exception:
                # 檔案損毀 / 被中斷寫入 (truncated JSON) 時, 退回空清單讓程式仍可啟動
                log.exception('get_work_dir_list: failed to read %s (fallback to empty list)',
                              self.work_dir_list_file)

    def get_tool_history(self):
        self.tool_history = ""
        if os.path.exists(self.tool_history_file):
            try:
                with open(self.tool_history_file, 'r') as f:
                    self.tool_history = json.load(f)
            except Exception:
                log.exception('get_tool_history: failed to read %s (fallback to empty)',
                              self.tool_history_file)

    def remove_duplicates(self, input_list):
        seen   = set()
        result = []
        for item in input_list:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    # ------------------------------------------------------------------ #
    #  Work-dir management
    # ------------------------------------------------------------------ #
    def _on_work_dir_activated(self, index):
        selected_value = self.choose_work_dir.currentText()
        self.add_work_dir_list(selected_value)

    def user_choose_work_dir(self):
        path = QFileDialog.getExistingDirectory(self, "選擇路徑")
        if path:
            self.add_work_dir_list(path)

    def add_work_dir_list(self, new_dir):
        self.work_dir_list = [new_dir] + self.work_dir_list
        self.work_dir_list = self.remove_duplicates(self.work_dir_list)
        try:
            with open(self.work_dir_list_file, 'w') as f:
                json.dump(self.work_dir_list, f)
        except Exception:
            # 寫入失敗不阻斷操作 (清單此次未持久化), 但要留下紀錄
            log.exception('add_work_dir_list: failed to write %s', self.work_dir_list_file)

        self.choose_work_dir.blockSignals(True)
        self.choose_work_dir.clear()
        self.choose_work_dir.addItems(self.work_dir_list)
        self.choose_work_dir.setCurrentIndex(0)
        self.choose_work_dir.blockSignals(False)

    # ------------------------------------------------------------------ #
    #  Update logic
    # ------------------------------------------------------------------ #
    def start_update(self):
        self.release_note_label.setText("")
        self.version_label.setText("")

        self.tool_history = self.choose_tool.currentText()
        try:
            with open(self.tool_history_file, 'w') as f:
                json.dump(self.tool_history, f)
        except Exception:
            # 記錄工具選擇失敗不應擋住開啟程式的流程
            log.exception('start_update: failed to write tool history %s', self.tool_history_file)

        log.info('start_update: tool=%s', self.tool_history)
        self.target = self.setting[self.choose_tool.currentText()]
        self.ensure_work_dir()

    def check_work_dir(self):
        return bool(self.work_dir and os.path.exists(self.work_dir))

    def ensure_work_dir(self):
        self.work_dir = self.choose_work_dir.currentText()
        if not self.check_work_dir():
            self.user_choose_work_dir()
            self.work_dir = self.choose_work_dir.currentText()

        if self.check_work_dir():
            threading.Thread(target=self.check_for_update, daemon=True).start()

    def check_for_update(self):
        self.signals.show_status_frame.emit()
        self.signals.set_btn_enabled.emit(False)

        try:
            self.get_newest_version()
            self.get_current_version()
            self.get_extract_info()

            if ('version' in self.current_version_info and
                    'version' in self.newest_version_info and
                    self.current_version_info['version'] == self.newest_version_info['version']):
                self.update_required = False
            else:
                self.update_required = True
            log.info('check_for_update: update_required=%s (current=%s newest=%s)',
                     self.update_required,
                     self.current_version_info.get('version'),
                     self.newest_version_info.get('version'))

            self.signals.update_status.emit("下載更新")
            # daemon: 使用者關窗後, 行程不得為了等下載跑完而殘留背景 (非 daemon 會被
            # interpreter shutdown join 住, 之後還會憑空 Popen 工具視窗)
            threading.Thread(target=self.download_file, daemon=True).start()
        except Exception:
            log.exception('check_for_update FAILED (target=%s)', getattr(self, 'target', '?'))
            self.signals.update_status.emit("檢查更新失敗, 請確認網路連線 (詳見 log)")
            self.signals.set_btn_enabled.emit(True)

    def update_progress(self, downloaded, total_length):
        self.signals.update_progress.emit(downloaded, total_length)

    def get_newest_version(self):
        log.info('get_newest_version: fetching %s', self.target)
        r = requests.get(self.target, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        self.newest_version_info = json.loads(r.text)
        self.target_directory    = self.get_install_dir() + "/" + self.newest_version_info["target_directory"]
        self.update_info         = self.newest_version_info['update_info']
        self.current_version     = self.target_directory + "/" + self.newest_version_info["current_version"]
        self.exe_name            = self.newest_version_info["exe_name"]
        self.release_note        = self.newest_version_info.get("release_note", "")
        log.info('get_newest_version: newest=%s exe=%s target_dir=%s',
                 self.newest_version_info.get('version'), self.exe_name, self.target_directory)

    def get_current_version(self):
        version_info = {}
        if os.path.isfile(self.current_version):
            try:
                with open(self.current_version, "r") as f:
                    version_info = json.loads(f.read())
            except Exception:
                log.exception('get_current_version: failed to read %s (treated as no version)',
                              self.current_version)
        self.current_version_info = version_info

    def set_current_version(self, version_info):
        try:
            with open(self.current_version, "w") as f:
                f.write(json.dumps(version_info))
            log.info('set_current_version: wrote %s to %s', version_info, self.current_version)
        except Exception:
            log.exception('set_current_version: failed to write %s', self.current_version)

    def get_zip_file_name(self, url):
        return url.split("/")[-1]

    def get_extract_info(self):
        self.extract_info = []
        for info in self.update_info:
            url        = info["url"]
            overwrite  = info["overwrite"]
            extract_to = info["extract_to"]
            extract_path = self.work_dir if extract_to == "work_dir" else self.target_directory
            self.extract_info.append((url, extract_path, overwrite))
        log.info('get_extract_info: %d item(s) to process', len(self.extract_info))

    def download_file(self):
        # 進行中旗標: closeEvent 據此警告 — daemon 下載執行緒會在關窗時被凍結,
        # 可能留下解壓到一半的目錄 (overwrite=False 項目會被 case2 誤判為已安裝)
        self._install_busy = True
        result   = True
        zip_path = None
        try:
            for url, extract_dir, en_overwrite in self.extract_info:
                log.info('download_file: url=%s extract_dir=%s overwrite=%s',
                         url, extract_dir, en_overwrite)

                zip_file_name = self.get_zip_file_name(url)
                dir_name      = zip_file_name.replace('.zip', '')
                # zip 暫存於 ~/PCDV 下的絕對路徑 + 唯一暫存名:
                #  - 裸檔名會落在行程 CWD (捷徑「開始位置」、唯讀共享、System32...),
                #    部分使用者的 CWD 不可寫, open() 直接 Errno 13 (2026-08 使用者災情)
                #  - 不可暫存在 extract_dir: work_dir 項目會把暫存檔寫進使用者專案
                #    目錄, 同名檔案 (如 input.zip) 會被截斷後刪除
                zip_path = os.path.join(self.get_install_dir(),
                                        f'{zip_file_name}.{os.getpid()}.part')

                case1 = self.update_required and en_overwrite
                case2 = not en_overwrite and not os.path.exists(f"{extract_dir}/{dir_name}")

                if case1 or case2:
                    log.info('download_file: downloading %s -> %s', url, zip_path)
                    self.signals.update_status.emit("下載中 ...")
                    with requests.get(url, stream=True, timeout=_HTTP_TIMEOUT) as r:
                        r.raise_for_status()
                        try:
                            total_length = int(r.headers.get('content-length'))
                        except Exception:
                            total_length = 36408565
                        downloaded = 0
                        next_mark  = _LOG_EVERY_BYTES
                        with open(zip_path, 'wb') as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                if chunk:
                                    downloaded += len(chunk)
                                    f.write(chunk)
                                    self.update_progress(downloaded, total_length)
                                    if downloaded >= next_mark:
                                        log.info('download_file progress: %d/%d bytes (%s)',
                                                 downloaded, total_length, zip_file_name)
                                        next_mark += _LOG_EVERY_BYTES
                    log.info('download_file: download done %d bytes -> %s',
                             downloaded, zip_path)

                    self.signals.update_status.emit("安裝中 ...")
                    # 任一項目失敗都須讓整體 result=False (不可被後面項目的成功蓋掉),
                    # 否則版本檔照寫, 壞掉的安裝永遠不會再被重抓
                    if not self.extract_zip(zip_path, extract_dir):
                        result = False
                else:
                    log.info('download_file: skip %s (already present / no update needed)', url)
        except PermissionError:
            # 寫檔被拒與網路無關, 須分開講清楚, 使用者/IT 才不會往網路方向查
            log.exception('download_file FAILED (寫檔被拒; cwd=%s)', os.getcwd())
            self._discard_temp_zip(zip_path)
            self.signals.update_status.emit("下載失敗: 檔案寫入被拒 (權限不足, 詳見 log)")
            self.signals.set_btn_enabled.emit(True)
            self._install_busy = False
            return
        except Exception:
            log.exception('download_file FAILED (下載或安裝過程發生例外)')
            self._discard_temp_zip(zip_path)
            self.signals.update_status.emit("下載失敗, 請確認網路連線 (詳見 log)")
            self.signals.set_btn_enabled.emit(True)
            self._install_busy = False
            return

        self._install_busy = False
        if result:
            self.signals.update_status.emit("安裝完成")
            self.set_current_version({'version': self.newest_version_info['version']})
            self.start()
        else:
            log.error('download_file: extract failed, asking user to delete folder and re-download')
            self.signals.update_status.emit("安裝異常, 請將資料夾全部刪除並重新下載")
            self.signals.set_btn_enabled.emit(True)

    def start(self):
        self.signals.update_release.emit(
            f"當前版本 {self.newest_version_info['version']}",
            f"{self.release_note}"
        )
        self.signals.update_status.emit("更新完成, 程式已自動開啟")
        self.update_progress(1, 1)
        self.signals.hide_status_frame.emit()
        self.signals.set_btn_enabled.emit(True)

        self.target_directory = self.target_directory.replace('\\', '/')
        exe_path = f"{self.target_directory}/{self.exe_name}"
        log.info('start: launching %s (work_dir=%s)', exe_path, self.work_dir)
        try:
            proc = subprocess.Popen([exe_path, self.work_dir], cwd=self.target_directory)
        except Exception:
            log.exception('start: failed to launch %s', exe_path)
            self.signals.update_status.emit("啟動程式失敗, 請確認檔案是否存在 (詳見 log)")
            return
        # start() 跑在 worker thread; 不直接 append (避免與 GUI thread 的 list 重建競態),
        # 改以 queued signal 把紀錄交給 GUI thread 處理
        record = {
            'popen': proc,
            'tool': self.tool_history,
            'work_dir': self.work_dir,
            'label': f"{self.tool_history}  ({self.work_dir})",
        }
        log.info('start: launched pid=%s tool=%s', getattr(proc, 'pid', '?'), self.tool_history)
        self.signals.process_added.emit(record)

    @staticmethod
    def _discard_temp_zip(zip_path):
        """best-effort 移除下載暫存檔 (半截檔不可殘留; 失敗僅記 log, 不擋錯誤回報)。"""
        if not zip_path:
            return
        try:
            if os.path.exists(zip_path):
                os.remove(zip_path)
        except OSError:
            log.warning('could not remove temp zip %s', zip_path, exc_info=True)

    def extract_zip(self, zip_file_name, extract_dir):
        result = True
        log.info('extract_zip: %s -> %s', zip_file_name, extract_dir)
        try:
            with zipfile.ZipFile(zip_file_name, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
            log.info('extract_zip: done %s', zip_file_name)
        except Exception:
            log.exception('extract_zip FAILED: %s -> %s', zip_file_name, extract_dir)
            result = False

        try:
            if os.path.exists(zip_file_name):
                os.remove(zip_file_name)
        except Exception:
            log.exception('extract_zip: failed to remove temp zip %s', zip_file_name)

        return result

    # ------------------------------------------------------------------ #
    #  Process panel
    # ------------------------------------------------------------------ #
    def toggle_process_panel(self):
        # 只切換使用者意圖, 實際版面交由 _sync_panel 調和
        self.panel_collapsed = not self.panel_collapsed
        self._sync_panel()

    def _add_process(self, record):
        """GUI thread slot: 由 worker thread 的 process_added signal 觸發, 在主執行緒 append。"""
        self.processes.append(record)
        self.refresh_process_panel()

    def refresh_process_panel(self):
        # 移除已結束的 process (僅在 GUI thread 執行)
        self.processes = [r for r in self.processes if r['popen'].poll() is None]

        # 以 Tool Name 排序後填入表格
        records = sorted(self.processes, key=lambda r: r['tool'].lower())

        self.process_table.setRowCount(len(records))
        for row, record in enumerate(records):
            tool_item = QTableWidgetItem(record['tool'])
            tool_item.setToolTip(record['label'])
            self.process_table.setItem(row, 0, tool_item)

            path_item = QTableWidgetItem(record['work_dir'])
            path_item.setToolTip(record['work_dir'])
            self.process_table.setItem(row, 1, path_item)

            close_btn = QPushButton("關閉")
            close_btn.setFont(self.default_font)
            close_btn.setStyleSheet(
                "QPushButton { background-color: #D83B01; color: white; border: none; border-radius: 4px; padding: 2px 12px; }"
                "QPushButton:hover { background-color: #C23501; }"
                "QPushButton:pressed { background-color: #A32D01; }"
            )
            close_btn.clicked.connect(lambda checked=False, r=record: self.close_process(r))
            self.process_table.setCellWidget(row, 2, close_btn)

        self._sync_panel()

    def _can_compensate(self):
        """視窗最大化時 resize() 無效; 此時略過 (左側為固定寬, 正確性不受影響, 僅外觀有空隙)。"""
        return not (self.isMaximized() or bool(self.windowState() & Qt.WindowState.WindowMaximized))

    def _sync_panel(self):
        """唯一的狀態調和處: 依 (是否有 process, panel_collapsed) 決定面板狀態與視窗大小。

        由 refresh_process_panel (timer / process_added) 與 toggle_process_panel 共同呼叫,
        所以週期性刷新不會把使用者手動縮合的面板強制展開。
        """
        count = len(self.processes)
        has   = count > 0

        # 數量顯示 (標題與 rail 徽章)
        self.process_title.setText(f"執行中的程式 ({count})")
        self.rail_badge.setText(str(count))

        if not has:
            state = 'hidden'
        elif self.panel_collapsed:
            state = 'collapsed'
        else:
            state = 'expanded'

        # 切換內容可見性 (idempotent)。縮合時表格隱藏, 其 minimumWidth(360) 不再參與版面
        self.expanded_container.setVisible(state == 'expanded')
        self.collapsed_rail.setVisible(state == 'collapsed')

        if state == self._panel_state:
            return
        self._panel_state = state
        self._apply_panel_geometry(state)

    def _apply_panel_geometry(self, state):
        """設定面板固定寬度, 並把視窗縮放成剛好容納 (左側固定寬 + 間距 + 面板)。

        左側為固定寬, 中間有彈性 stretch, 因此即使 resize 被夾住 (例如最大化), 左側也絕不位移,
        最壞只是中間出現空隙。activate() 先讓版面重算最小寬, 避免縮小時被尚未更新的舊 min 夾住。
        """
        panel_w = {'hidden': 0,
                   'collapsed': self.PANEL_COLLAPSED_W,
                   'expanded':  self.PANEL_EXPANDED_W}[state]
        visible = state != 'hidden'

        if visible:
            self.process_frame.setFixedWidth(panel_w)
            self.process_frame.show()
        else:
            self.process_frame.hide()

        # 強制版面同步重算, 使視窗最小寬反映新的面板寬度 (否則縮小 resize 會被舊 min 夾住)
        self.outer_layout.activate()
        if self.layout() is not None:
            self.layout().activate()

        # 視窗縮放成剛好容納內容 (左側固定 + 間距 + 面板)。純為外觀, 左側固定寬故不受影響
        if self._can_compensate():
            target = self.LEFT_W + 2 * self._margin + self._gap + panel_w
            self.resize(target, self.height())

    def close_process(self, record):
        popen = record['popen']
        if popen.poll() is None:
            log.info('close_process: terminating %s', record.get('label'))
            try:
                popen.terminate()
            except Exception:
                log.exception('close_process: terminate failed for %s', record.get('label'))
        self.refresh_process_panel()

    def closeEvent(self, event):
        # 先停掉輪詢, 避免確認對話框 (內含巢狀事件迴圈) 期間還在重建表格 / 縮放視窗
        self.process_timer.stop()

        # 安裝進行中: daemon 下載執行緒會在關窗時被凍結, overwrite=False 項目
        # 若解壓到一半, 之後會被 case2 誤判為已安裝且永不自我修復 — 先警告
        if self._install_busy:
            reply = QMessageBox.question(
                self,
                "安裝進行中",
                "工具正在下載 / 安裝中, 現在關閉可能造成安裝不完整。\n確定要關閉嗎？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                self.process_timer.start(1500)   # 取消關閉 -> 恢復輪詢
                event.ignore()
                return

        running = [r for r in self.processes if r['popen'].poll() is None]
        if running:
            names = "\n".join(f"  • {r['label']}" for r in running)
            reply = QMessageBox.question(
                self,
                "尚有程式執行中",
                f"以下程式尚未關閉：\n\n{names}\n\n是否要一併關閉這些程式？",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Cancel:
                self.process_timer.start(1500)   # 取消關閉 -> 恢復輪詢
                event.ignore()
                return
            if reply == QMessageBox.StandardButton.Yes:
                for r in running:
                    if r['popen'].poll() is None:
                        log.info('closeEvent: terminating %s', r.get('label'))
                        try:
                            r['popen'].terminate()
                        except Exception:
                            # 單一程序關閉失敗不應擋住視窗關閉
                            log.exception('closeEvent: terminate failed for %s', r.get('label'))
        event.accept()


def create_loading_splash(app):
    """程式啟動瞬間顯示的載入畫面 (macOS 風格卡片)。

    在下載設定檔 / 建立主視窗等耗時工作「之前」先顯示, 主視窗就緒後由呼叫端關閉。
    讓使用者一打開程式就立即看到畫面, 不會有開啟初期的空白延遲感。
    """
    splash = QWidget(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
    splash.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    splash.setFixedSize(360, 170)

    card = QFrame(splash)
    card.setObjectName("splashCard")
    card.setGeometry(0, 0, 360, 170)
    card.setStyleSheet(
        "#splashCard { background-color: #2B2B2B; border: 1px solid #3F3F46; border-radius: 12px; }"
    )
    v = QVBoxLayout(card)
    v.setContentsMargins(24, 24, 24, 24)
    v.setSpacing(12)
    v.addStretch()

    pix = QPixmap(get_bundled_path('realtek.png'))
    if not pix.isNull():
        logo = QLabel()
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet("background: transparent;")
        logo.setPixmap(pix.scaledToHeight(46, Qt.TransformationMode.SmoothTransformation))
        v.addWidget(logo)

    title = QLabel(f'PCDV DV Utility {AutoUpdateGUI.version}')
    title.setFont(QFont("Microsoft JhengHei", 13, QFont.Weight.Bold))
    title.setStyleSheet("color: #FFFFFF; background: transparent;")
    title.setAlignment(Qt.AlignmentFlag.AlignCenter)
    v.addWidget(title)

    sub = QLabel("載入中…")
    sub.setFont(QFont("Microsoft JhengHei", 10))
    sub.setStyleSheet("color: #A0A0A0; background: transparent;")
    sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
    v.addWidget(sub)
    v.addStretch()

    # 置中於主螢幕 (primaryScreen() 可能為 None: 無顯示器 / session-0 / RDP 重連競態)
    screen = app.primaryScreen()
    if screen is not None:
        geo = screen.availableGeometry()
        splash.move(geo.center().x() - splash.width() // 2,
                    geo.center().y() - splash.height() // 2)
    return splash


if __name__ == "__main__":
    log.info('=' * 60)
    # cwd 一併記錄: 相對路徑類災情 (如寫檔 Errno 13) 一眼就能看出啟動情境
    log.info('DV Utility starting (version=%s, log dir=%s, cwd=%s)',
             _read_app_version(), _get_log_dir(), os.getcwd())

    # 清掉上次更新留下的舊 exe 備份 (*.bak); 趁舊行程已退出, 放在最前面
    try:
        update_check.cleanup_stale_backup()
    except Exception:
        log.exception('cleanup_stale_backup failed (non-fatal)')

    # Windows 工作列圖示需要設定 AppUserModelID
    windll.shell32.SetCurrentProcessExplicitAppUserModelID('Realtek.PCDV.DVUtility')

    app = QApplication(sys.argv)
    app.setStyle("Fusion")   # 一致的扁平基底, QSS 子控制項 (下拉箭頭/進度條) 才能穩定渲染

    # 一打開程式就立即顯示載入畫面 (在下載設定檔 / 建立主視窗之前)
    splash = create_loading_splash(app)
    splash.show()
    app.processEvents()      # 立即繪製 splash; 之後的同步初始化期間它會持續顯示

    try:
        inst = AutoUpdateGUI(app)   # 下載設定檔 + 建立主視窗 (期間 splash 持續顯示)
        inst.show()
        app.processEvents()         # 先讓主視窗上屏, 再關閉 splash, 避免中間出現空畫面
    except Exception:
        log.exception('Fatal error during startup')
        try:
            QMessageBox.critical(
                None, "啟動失敗",
                "程式啟動時發生錯誤, 請確認網路連線後重試。\n\n"
                f"詳細記錄檔:\n{_get_log_dir()}\\dv_utility.log",
            )
        except Exception:
            pass
        sys.exit(1)
    finally:
        splash.close()
        splash.deleteLater()

    log.info('DV Utility main window shown, entering event loop')
    sys.exit(app.exec())
