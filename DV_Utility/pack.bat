pyinstaller -F --noconsole --add-data "c:\users\lichen.liu\anaconda3\envs\dv_utility\lib\site-packages/customtkinter;customtkinter/" dv_utility.py

copy dist\dv_utility.exe DV_Utility.exe

del dv_utility.spec

rd /s/q dist
rd /s/q build
rd /s/q __pycache__

powershell Compress-Archive -Force DV_Utility.exe DV_Utility.zip