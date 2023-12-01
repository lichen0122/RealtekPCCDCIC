import tkinter as tk
from tkinter import filedialog
from tkinter import ttk
import threading
import requests
import json
import random
import subprocess
from subprocess import CREATE_NO_WINDOW
import zipfile
from pathlib import Path
import os
import pickle


class AutoUpdateGUI():
    url            = ''
    processes      = []
    

    def __init__(self):

        self.ensure_resource_files()

        with open(self.setting_file, "r") as f:
            self.setting = json.loads(f.read())
        
        self.init_window()
        self.root.mainloop()

    def ensure_resource_files(self):
        self.resource           = f'{self.get_install_dir()}/resource'
        self.setting_file       = f"{self.resource}/setting.json"
        self.ico_file           = f'{self.resource}/realtek.ico'
        self.work_dir_list_file = f'{self.resource}/work_dir_list.json'


        if not os.path.exists(self.resource):
            os.mkdir(f'{self.resource}')

        if not os.path.exists(self.setting_file):
            url = 'https://raw.github.com/lichen0122/RealtekPCCDCIC/main/dv_util_resource/setting.json'
            self.download_from_git(url, self.setting_file)

        if not os.path.exists(self.ico_file):
            url = 'https://raw.github.com/lichen0122/RealtekPCCDCIC/main/dv_util_resource/realtek.ico'
            self.download_from_git(url, self.ico_file)

        if not os.path.exists(self.work_dir_list_file):
            with open(self.work_dir_list_file, 'w') as f:
                json.dump([], f)


    def get_install_dir(self):
        from pathlib import Path
        home_path = Path.home() / 'PCDV'
        home_path.mkdir(exist_ok=True)
        return str(home_path)


    def init_window(self):
        self.root = tk.Tk()
        self.root.title('PCCDCIC DV Utility')
        self.root_width  = 800
        self.root_height = 150
        self.root.configure(bg="white")
        self.root.geometry("%dx%d" % (self.root_width, self.root_height))
        self.root.resizable(False, False)
        self.root.iconbitmap(self.ico_file)
        # self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        style = ttk.Style()
        style.configure('TLabel',  background = 'white', foreground = 'black')
        style.configure('TFrame',  background = 'white', foreground = 'black')
        style.configure('TButton', background = 'white', foreground = 'black')
        style.configure('TProgressbar', background = 'white', foreground = 'black')

        ## ------------------------------------ path frame ------------------------------------ ##
        self.path_frame = ttk.Frame(self.root)
        self.path_frame.pack(pady=(5,5))
        self.path_label = ttk.Label(self.path_frame, text="請選擇路徑:", font=("微軟正黑體", 9))
        self.path_label.grid(row=1, column=0, padx=(0, 5))
        self.get_work_dir_list()
        self.choose_work_dir = ttk.Combobox(self.path_frame, values=self.work_dir_list, width=80)
        if self.work_dir_list:
            self.choose_work_dir.current(0)
        self.choose_work_dir.grid(row=1, column=1)
        self.choose_dir_button    = ttk.Button(self.path_frame, text="選擇 project 路徑", command=self.user_choose_work_dir, width=16)
        self.choose_dir_button.grid(row=1, column=2, padx=(5, 0))

        ## ------------------------------------ tool frame ------------------------------------ ##
        self.tool_frame = ttk.Frame(self.root)
        self.tool_frame.pack(pady=(5,5))
        self.tool_label = ttk.Label(self.tool_frame, text="請選擇工具:", font=("微軟正黑體", 9))
        self.tool_label.grid(row=0, column=0, padx=(0, 5))
        self.tool_option_list = list(self.setting.keys())
        self.choose_tool      = ttk.Combobox(self.tool_frame, values=self.tool_option_list, width=80)
        self.choose_tool.current(0)
        self.choose_tool.grid(row=0, column=1)
        self.start_button    = ttk.Button(self.tool_frame, text="開啟", command=self.start_update, width=16)
        self.start_button.grid(row=0, column=2, padx=(5, 0))

        ## ------------------------------------ status frame ------------------------------------ ##
        self.status_frame = ttk.Frame(self.root)
        self.progress = ttk.Progressbar(self.status_frame, orient='horizontal', length=255, mode='determinate')
        self.progress.grid(row=0, column=0)
        self.progress_label = ttk.Label(self.status_frame, text="0%", font=("微軟正黑體", 9))
        self.progress_label.grid(row=1, column=0)

        self.status_label = ttk.Label(self.status_frame, font=('微軟正黑體', 15), text="")
        self.status_label.grid(row=2, column=0)

    def download_from_git(self, url, output):
        with requests.get(url, stream=True) as r:
            try:
                total_length = int(r.headers.get('content-length'))
            except:
                total_length = 36408565

            with open(output, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192): 
                    if chunk:
                        f.write(chunk)

    def user_choose_work_dir(self):
        path = filedialog.askdirectory()
        if path:
            self.add_work_dir_list(path)

    def ensure_work_dir(self):
        self.work_dir = self.choose_work_dir.get()
        while not self.work_dir or not os.path.exists(self.work_dir):
            self.user_choose_work_dir()
            self.work_dir = self.choose_work_dir.get()

    def start_update(self):
        self.status_frame.pack(pady=(5,0))
        self.target   = self.setting[self.choose_tool.get()]
        self.ensure_work_dir()
        daemon_thread = threading.Thread(target=self.check_for_update, daemon=True)
        daemon_thread.start()

    def on_closing(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        self.root.destroy()

    def get_work_dir_list(self):
        if os.path.exists(self.work_dir_list_file):
            with open(self.work_dir_list_file, 'r') as f:
                self.work_dir_list = json.load(f)
        else:
            self.work_dir_list = []

    def remove_duplicates(self, input_list):
        seen = set()
        result = []
        for item in input_list:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    def add_work_dir_list(self, new_dir):
        self.work_dir_list = [new_dir] + self.work_dir_list
        self.work_dir_list = self.remove_duplicates(self.work_dir_list)
        with open('resource/work_dir_list.json', 'w') as f:
            json.dump(self.work_dir_list, f)

        self.choose_work_dir.config(values=self.work_dir_list)
        if self.work_dir_list:
            self.choose_work_dir.current(0)

    def get_newest_version(self):
        version_info = {}
        url = self.target
        r   = requests.get(url)
        version_info = json.loads(r.text)

        self.newest_version_info  = version_info
        self.target_directory     = self.get_install_dir() + "/" + self.newest_version_info["target_directory"]
        self.update_info          = self.newest_version_info['update_info']
        self.current_version      = self.target_directory + "/" + self.newest_version_info["current_version"]
        self.exe_name             = self.newest_version_info["exe_name"]

    def get_current_version(self):
        version_info = {}
        if os.path.isfile(self.current_version):
            with open(self.current_version, "r") as f:
                version_info = json.loads(f.read())
            
        self.current_version_info = version_info

    def set_current_version(self, version_info):
        json_object = json.dumps(version_info)

        with open(self.current_version, "w") as outfile:
            outfile.write(json_object)

    def check_for_update(self):
        self.start_button["state"] = "disabled"
        self.get_newest_version()
        self.get_current_version()
        self.get_extract_info()

        if 'version' in self.current_version_info and 'version' in self.newest_version_info and self.current_version_info['version'] == self.newest_version_info['version']:
            self.start()
        else:
            self.status_label.config(text="下載更新")
            threading.Thread(target=self.download_file).start()

    def update_progress(self, downloaded, total_length):
        percent = (downloaded / total_length) * 100
        self.progress['value'] = percent
        self.progress_label.config(text=f"{percent:.2f}%")
        self.root.update_idletasks()

    def get_zip_file_name(self, url):
        return url.split("/")[-1]

    def get_extract_info(self):
        enable_overwrite  = True
        disable_overwrite = False

        self.extract_info = []
        for info in self.update_info:
            url        = info["url"]
            overwrite  = info["overwrite"]
            extract_to = info["extract_to"]

            extract_path = self.work_dir if extract_to == "work_dir" else self.target_directory

            self.extract_info += [(url, extract_path, overwrite)]

    def download_file(self):
        for url, extract_dir, en_overwrite in self.extract_info:
            print(url, extract_dir, en_overwrite)
            self.status_label.config(text="下載更新")
            zip_file_name = self.get_zip_file_name(url)

            if en_overwrite and os.path.exists(f"{extract_dir}/{zip_file_name}"):
                continue

            with requests.get(url, stream=True) as r:
                try:
                    total_length = int(r.headers.get('content-length'))
                except:
                    total_length = 36408565
                downloaded = 0

                with open(zip_file_name, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192): 
                        if chunk:
                            downloaded += len(chunk)
                            f.write(chunk)
                            self.update_progress(downloaded, total_length)


            self.status_label.config(text="安裝中")
            result = self.extract_zip(zip_file_name, extract_dir)

        if result:
            self.status_label.config(text="安裝完成")
            self.set_current_version({'version': self.newest_version_info['version']})
            self.start()
        else:
            self.status_label.config(text="安裝異常, 請將資料夾全部刪除並重新下載")

    def start(self):
        self.status_label.config(text=f"更新完成, 程式已自動開啟")
        self.update_progress(1, 1)

        self.start_button["state"] = "nogrmal"
        self.cmd = f'start {self.exe_name} {self.work_dir}'
        print(self.cmd)
        self.processes.append(subprocess.Popen(self.cmd, cwd=self.target_directory, shell=True))

    def extract_zip(self, zip_file_name, extract_dir):
        result = True

        try:
            with zipfile.ZipFile(zip_file_name, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
        except:
            result = False

        if os.path.exists(zip_file_name):
            os.remove(zip_file_name)

        return result


if __name__ == "__main__":
    inst = AutoUpdateGUI()
