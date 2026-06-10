#!/bin/bash
# SyncListen 启动脚本

cd "$(dirname "$0")"

# 直接定位 conda 环境里的 python，避免调用 conda CLI。
# 原因：每次 `conda info --base` / `conda env list` / `conda activate` 都会
# 另起一个 Python 进程，合计拖慢启动约 4 秒；而启动本程序并不需要完整
# activate，base 的 python 不激活也能正常 import numpy/torch/funasr。
# 如有依赖装在其它前缀，可用环境变量覆盖：CONDA_BASE=/path ./run.sh
CONDA_BASE="${CONDA_BASE:-$HOME/miniconda3}"

if [ -x "$CONDA_BASE/envs/synclisten/bin/python" ]; then
    PY="$CONDA_BASE/envs/synclisten/bin/python"   # 优先专用环境
elif [ -x "$CONDA_BASE/bin/python" ]; then
    PY="$CONDA_BASE/bin/python"                    # 回退到 base（依赖在此）
else
    PY="python3"                                   # 最终回退到系统 python
fi

exec "$PY" main.py "$@"
