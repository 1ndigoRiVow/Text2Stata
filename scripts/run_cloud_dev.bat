@echo off
rem Text2Stata 云端（本地开发模式）双击启动
rem 用完直接关窗口；数据落在 cloud_data\，不会丢
cd /d %~dp0
cd ..
.venv\Scripts\python.exe scripts\run_cloud_dev.py
pause
