@echo off
cd /d C:\Users\USER\Desktop\stock_research
python retro_label.py > logs\retro_label_mcp.log 2>&1
echo RETRO_LABEL_RC=%errorlevel% >> logs\retro_label_mcp.log
python retro_forward.py --push > logs\retro_push_mcp.log 2>&1
echo RETRO_PUSH_RC=%errorlevel% >> logs\retro_push_mcp.log
echo DONE > logs\retro_pipeline_mcp.done
