@echo off
C:\Users\lichen.liu\AppData\Local\anaconda3\envs\tcon_visio\Scripts\pyinstaller.exe -F --noconsole --add-data "C:\Users\lichen.liu\AppData\Local\anaconda3\envs\tcon_visio\Lib\site-packages\customtkinter;customtkinter/" --noconfirm  ./dv_utility.py

copy dist\dv_utility.exe DV_Utility.exe

del dv_utility.spec

rd /s/q dist
rd /s/q build
rd /s/q __pycache__

powershell Compress-Archive -Force DV_Utility.exe DV_Utility.zip