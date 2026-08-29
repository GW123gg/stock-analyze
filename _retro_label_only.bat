@echo off
cd /d C:\Users\USER\Desktop\stock_research
python retro_label.py 1> logs\rl_out.log 2> logs\rl_err.log
echo RETRO_LABEL_RC=%errorlevel% > logs\rl_rc.txt
