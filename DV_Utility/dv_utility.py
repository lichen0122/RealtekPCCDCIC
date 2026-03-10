import sys
from ctypes import windll
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QComboBox, QPushButton,
    QProgressBar, QGridLayout, QVBoxLayout, QFrame, QFileDialog
)
from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QFont, QIcon, QShortcut, QKeySequence
import threading
import requests
import json
import subprocess
import zipfile
from pathlib import Path
import os


def get_bundled_path(filename):
    """取得打包後或開發時的資源檔路徑"""
    if hasattr(sys, '_MEIPASS'):
        # PyInstaller onefile/standalone
        return os.path.join(sys._MEIPASS, filename)
    if globals().get('__compiled__'):
        # Nuitka standalone/onefile: __file__ points to the actual extraction dir
        return os.path.join(os.path.dirname(__file__), filename)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)


class WorkerSignals(QObject):
    update_status      = Signal(str)
    update_progress    = Signal(float, float)
    update_release     = Signal(str, str)
    show_status_frame  = Signal()
    hide_status_frame  = Signal()
    set_btn_enabled    = Signal(bool)


class AutoUpdateGUI(QMainWindow):
    url       = ''
    processes = []
    version   = 'v20260310'

    def __init__(self, app):
        super().__init__()
        self.app     = app
        self.signals = WorkerSignals()

        self.ensure_resource_files()

        with open(self.setting_file, "r") as f:
            self.setting = json.loads(f.read())

        self.init_window()
        self.connect_signals()

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

    # ------------------------------------------------------------------ #
    #  Window / UI init
    # ------------------------------------------------------------------ #
    def init_window(self):
        self.setWindowTitle(f'PCDV DV Utility {self.version}')
        icon = QIcon(self.ico_file)
        self.setWindowIcon(icon)
        self.app.setWindowIcon(icon)
        self.resize(820, 230)

        default_font = QFont("Microsoft JhengHei", 11)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(10, 10, 10, 10)
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
    #  Hotkey
    # ------------------------------------------------------------------ #
    def on_ctrl_m(self):
        subprocess.Popen(['explorer', self.get_install_dir()])

    # ------------------------------------------------------------------ #
    #  File helpers
    # ------------------------------------------------------------------ #
    def download_from_git(self, url, output):
        with requests.get(url, stream=True) as r:
            try:
                total_length = int(r.headers.get('content-length'))
            except Exception:
                total_length = 36408565
            with open(output, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

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
        self.processes.append(subprocess.Popen([self.exe_name, self.work_dir], cwd=self.target_directory))

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

    def closeEvent(self, event):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        event.accept()


if __name__ == "__main__":
    # Windows 工作列圖示需要設定 AppUserModelID
    windll.shell32.SetCurrentProcessExplicitAppUserModelID('Realtek.PCDV.DVUtility')

    app  = QApplication(sys.argv)
    inst = AutoUpdateGUI(app)
    inst.show()
    sys.exit(app.exec())
