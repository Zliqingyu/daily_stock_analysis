@echo off
cd /d G:\fun\dsa-private
if not exist logs mkdir logs
set LOGDATE=%date:~0,4%%date:~5,2%%date:~8,2%
set LOG=logs\morning_briefing_%LOGDATE%.log
echo [%time%] morning briefing start >> %LOG%
set STOCKS=513310,600021,003035,160644,003816
rem sync latest AlphaSift picks (fallback to local cache if proxy down)
set https_proxy=http://127.0.0.1:9910
git pull --rebase origin main >> %LOG% 2>&1
set https_proxy=

if exist data\alpha_latest_picks.txt (
  set /p PICKS=<data\alpha_latest_picks.txt
  if not "%PICKS%"=="" set STOCKS=%STOCKS%,%PICKS%
)
echo [%time%] stocks=%STOCKS% >> %LOG%
python main.py --stocks %STOCKS% --no-market-review >> %LOG% 2>&1
echo [%time%] exit %errorlevel% >> %LOG%
