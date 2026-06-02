@echo off
REM ============================================================
REM quant_trading 每日数据同步 (Windows Task Scheduler)
REM 2026-06-02
REM
REM 用法:
REM   1. 打开 Windows Task Scheduler (taskschd.msc)
REM   2. 创建基本任务 → 名称: quant_live_sync
REM   3. 触发器: 每天, 重复间隔 30 分钟
REM   4. 操作: 启动程序 → 浏览此 .bat 文件
REM   5. 条件: 取消"仅当计算机使用交流电源时启动"
REM   6. 确定
REM ============================================================

REM Python 3.12 路径 (按需修改)
set PYTHON=C:\Users\zhu\AppData\Local\Programs\Python\Python312\python.exe

REM 项目根
cd /d C:\Users\zhu\quant_trading

REM 日志目录
if not exist logs mkdir logs

REM 跑增量同步 (M15 / H1 / D1), 日志输出到文件
%PYTHON% scripts\live_sync.py --mode once --type incremental --timeframes M15,H1,D1 >> logs\live_sync.log 2>&1

REM 退出码 0 = 成功
exit /b %ERRORLEVEL%
