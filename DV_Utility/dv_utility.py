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
import self_update


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
        return 'vUNKNOWN'


class WorkerSignals(QObject):
    update_status      = Signal(str)
    update_progress    = Signal(float, float)
    update_release     = Signal(str, str)
    show_status_frame  = Signal()
    hide_status_frame  = Signal()
    set_btn_enabled    = Signal(bool)
    process_added      = Signal(object)   # 攜帶新 process 紀錄, 於 GUI thread append
    update_available   = Signal(object)   # 自我更新: 發現新版 (攜帶 info dict)
    update_done        = Signal(bool, str)  # 自我更新: 結束 (成功?, 版本/錯誤訊息)


class AutoUpdateGUI(QMainWindow):
    url       = ''
    version   = _read_app_version()

    # 右側面板寬度 (展開 / 縮合)。展開寬度須 >= 表格 minimumWidth(360) + 內距留白
    PANEL_EXPANDED_W  = 400
    PANEL_COLLAPSED_W = 36

    def __init__(self, app):
        super().__init__()
        self.app       = app
        self.signals   = WorkerSignals()
        # 每筆紀錄為 {'popen': Popen, 'label': str}
        self.processes = []

        self.ensure_resource_files()

        with open(self.setting_file, "r") as f:
            self.setting = json.loads(f.read())

        self.init_window()
        self.connect_signals()

        # 背景檢查自我更新 (僅打包執行時動作; 無更新 / 失敗皆靜默)
        threading.Thread(target=self._check_self_update, daemon=True).start()

    # ------------------------------------------------------------------ #
    #  Resource / settings
    # ------------------------------------------------------------------ #
    def ensure_resource_files(self):
        self.resource           = f'{self.get_install_dir()}/resource'
        self.setting_file       = f'{self.resource}/setting.json'
        self.ico_file           = get_bundled_path('realtek.png')
        self.work_dir_list_file = f'{self.resource}/work_dir_list.json'
        self.tool_history_file  = f'{self.resource}/tool_history.json'

        if not os.path.exists(self.resource):
            os.mkdir(self.resource)

        # 產生下拉箭頭 (macOS 風格雙 chevron) 資產, 供 QComboBox QSS 使用
        self._ensure_chevron_asset()

        # 啟動載入畫面 (見 __main__ 的 create_loading_splash) 已覆蓋此處的下載等待,
        # 故同步下載即可, 不再另開進度條 splash。
        url = 'https://raw.github.com/lichen0122/RealtekPCCDCIC/main/dv_util_resource/setting.json'
        self.download_from_git(url, self.setting_file)

        if not os.path.exists(self.work_dir_list_file):
            with open(self.work_dir_list_file, 'w') as f:
                json.dump([], f)

        if not os.path.exists(self.tool_history_file):
            with open(self.tool_history_file, 'w') as f:
                json.dump("", f)

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
        # 自我更新 (worker thread -> GUI thread)
        self.signals.update_available.connect(self._slot_update_available)
        self.signals.update_done.connect(self._slot_update_done)

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
    #  Self-update (DV_Utility.exe 本身的線上更新)
    # ------------------------------------------------------------------ #
    def _check_self_update(self):
        """背景執行緒: 比對 GCS 上的最新版本, 有更新就通知 GUI thread。"""
        info = self_update.check_for_new_version(self.version)
        if info:
            self.signals.update_available.emit(info)

    def _slot_update_available(self, info):
        """GUI thread: 詢問使用者是否立即更新。"""
        latest = info.get('version', '')
        ret = QMessageBox.question(
            self, "發現新版本",
            f"目前版本 {self.version}\n最新版本 {latest}\n\n是否立即更新並重新啟動?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if ret == QMessageBox.StandardButton.Yes:
            self.start_button.setEnabled(False)
            self.signals.show_status_frame.emit()
            self.signals.update_status.emit("下載更新中 ...")
            threading.Thread(target=self._run_self_update, args=(info,), daemon=True).start()

    def _run_self_update(self, info):
        """背景執行緒: 下載 + 換檔 + 啟動新版; 結果回報 GUI thread。"""
        try:
            self_update.download_and_swap(
                info,
                progress_cb=lambda d, t: self.signals.update_progress.emit(d, t),
            )
        except Exception as e:
            self.signals.update_done.emit(False, str(e))
            return
        self.signals.update_done.emit(True, info.get('version', ''))

    def _slot_update_done(self, ok, message):
        """GUI thread: 成功 -> 結束本程式讓新版接手; 失敗 -> 提示並恢復。"""
        if ok:
            self.signals.update_status.emit("更新完成, 正在重新啟動 ...")
            self.app.quit()
        else:
            self.start_button.setEnabled(True)
            self.signals.hide_status_frame.emit()
            QMessageBox.warning(self, "更新失敗", f"自我更新失敗:\n{message}")

    # ------------------------------------------------------------------ #
    #  Hotkey
    # ------------------------------------------------------------------ #
    def on_ctrl_m(self):
        subprocess.Popen(['explorer', self.get_install_dir()])

    # ------------------------------------------------------------------ #
    #  File helpers
    # ------------------------------------------------------------------ #
    def download_from_git(self, url, output, progress_cb=None):
        with requests.get(url, stream=True) as r:
            try:
                total_length = int(r.headers.get('content-length'))
            except Exception:
                total_length = 36408565
            downloaded = 0
            with open(output, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        downloaded += len(chunk)
                        f.write(chunk)
                        if progress_cb:
                            progress_cb(downloaded, total_length)

    def get_work_dir_list(self):
        if os.path.exists(self.work_dir_list_file):
            with open(self.work_dir_list_file, 'r') as f:
                self.work_dir_list = json.load(f)
        else:
            self.work_dir_list = []

    def get_tool_history(self):
        if os.path.exists(self.tool_history_file):
            with open(self.tool_history_file, 'r') as f:
                self.tool_history = json.load(f)
        else:
            self.tool_history = ""

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
        with open(self.work_dir_list_file, 'w') as f:
            json.dump(self.work_dir_list, f)

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
        with open(self.tool_history_file, 'w') as f:
            json.dump(self.tool_history, f)

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

        self.get_newest_version()
        self.get_current_version()
        self.get_extract_info()

        if ('version' in self.current_version_info and
                'version' in self.newest_version_info and
                self.current_version_info['version'] == self.newest_version_info['version']):
            self.update_required = False
        else:
            self.update_required = True

        self.signals.update_status.emit("下載更新")
        threading.Thread(target=self.download_file).start()

    def update_progress(self, downloaded, total_length):
        self.signals.update_progress.emit(downloaded, total_length)

    def get_newest_version(self):
        r = requests.get(self.target)
        self.newest_version_info = json.loads(r.text)
        self.target_directory    = self.get_install_dir() + "/" + self.newest_version_info["target_directory"]
        self.update_info         = self.newest_version_info['update_info']
        self.current_version     = self.target_directory + "/" + self.newest_version_info["current_version"]
        self.exe_name            = self.newest_version_info["exe_name"]
        self.release_note        = self.newest_version_info.get("release_note", "")

    def get_current_version(self):
        version_info = {}
        if os.path.isfile(self.current_version):
            with open(self.current_version, "r") as f:
                version_info = json.loads(f.read())
        self.current_version_info = version_info

    def set_current_version(self, version_info):
        with open(self.current_version, "w") as f:
            f.write(json.dumps(version_info))

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

    def download_file(self):
        result = True
        for url, extract_dir, en_overwrite in self.extract_info:
            print(url, extract_dir, en_overwrite)

            zip_file_name = self.get_zip_file_name(url)
            dir_name      = zip_file_name.replace('.zip', '')

            case1 = self.update_required and en_overwrite
            case2 = not en_overwrite and not os.path.exists(f"{extract_dir}/{dir_name}")

            if case1 or case2:
                print("下載中 ...")
                self.signals.update_status.emit("下載中 ...")
                with requests.get(url, stream=True) as r:
                    try:
                        total_length = int(r.headers.get('content-length'))
                    except Exception:
                        total_length = 36408565
                    downloaded = 0
                    with open(zip_file_name, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                downloaded += len(chunk)
                                f.write(chunk)
                                self.update_progress(downloaded, total_length)

                self.signals.update_status.emit("安裝中 ...")
                result = self.extract_zip(zip_file_name, extract_dir)

        if result:
            self.signals.update_status.emit("安裝完成")
            self.set_current_version({'version': self.newest_version_info['version']})
            self.start()
        else:
            self.signals.update_status.emit("安裝異常, 請將資料夾全部刪除並重新下載")

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
        print(self.exe_name, self.work_dir, self.target_directory)
        exe_path = f"{self.target_directory}/{self.exe_name}"
        proc = subprocess.Popen([exe_path, self.work_dir], cwd=self.target_directory)
        # start() 跑在 worker thread; 不直接 append (避免與 GUI thread 的 list 重建競態),
        # 改以 queued signal 把紀錄交給 GUI thread 處理
        record = {
            'popen': proc,
            'tool': self.tool_history,
            'work_dir': self.work_dir,
            'label': f"{self.tool_history}  ({self.work_dir})",
        }
        self.signals.process_added.emit(record)

    def extract_zip(self, zip_file_name, extract_dir):
        result = True
        try:
            with zipfile.ZipFile(zip_file_name, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
        except Exception:
            result = False

        if os.path.exists(zip_file_name):
            os.remove(zip_file_name)

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
            popen.terminate()
        self.refresh_process_panel()

    def closeEvent(self, event):
        # 先停掉輪詢, 避免確認對話框 (內含巢狀事件迴圈) 期間還在重建表格 / 縮放視窗
        self.process_timer.stop()

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
                        r['popen'].terminate()
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
    # 清掉上次自我更新留下的舊 exe (*.old); 趁舊行程已退出, 放在最前面
    self_update.cleanup_old()

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
    finally:
        splash.close()
        splash.deleteLater()

    sys.exit(app.exec())
