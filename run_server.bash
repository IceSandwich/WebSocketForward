#!/bin/bash

# 1. 检查是否存在 venv 目录
if [ ! -d "venv" ]; then
    echo "虚拟环境不存在，正在创建 venv..."
    python -m venv venv
fi

# 2. 激活虚拟环境
source venv/bin/activate

# 3. 安装依赖（已安装的会跳过）
echo "[1/2] 安装依赖..."
python -m pip install -r requirements.txt

# 4. 运行 server.py 脚本
echo "[2/2] 运行..."
nohup python server.py --server 127.0.0.1:12228 --cache &
echo $! > server.pid
echo 'server.py 已在后台运行。使用 `kill $(cat server.pid)` 停止它。'