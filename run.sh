#!/bin/bash
# SyncListen 启动脚本

cd "$(dirname "$0")"

# 检查 conda 环境
if command -v conda &> /dev/null; then
    # 尝试激活 synclisten 环境，否则使用 base
    if conda env list | grep -q "synclisten"; then
        source "$(conda info --base)/etc/profile.d/conda.sh"
        conda activate synclisten
    fi
fi

python3 main.py "$@"
