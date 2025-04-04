#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

PROJECT_NAME="websocketforward"
# 设置你的 service 文件路径
SERVICE_FILE="service/${PROJECT_NAME}.service"
# 设置软连接目标路径
LINK_PATH="/etc/systemd/system/${PROJECT_NAME}.service"

# 1. 检查是否存在 venv 目录
if [ ! -d "venv" ]; then
    echo "虚拟环境不存在，正在创建 venv..."
    python -m venv venv
fi

# 2. 激活虚拟环境
source venv/bin/activate

# 3. 安装依赖（已安装的会跳过）
echo "安装依赖..."
python -m pip install -r requirements.txt

# 检查参数
if [ $# -ne 1 ]; then
    echo "用法: $0 {install|uninstall|run}"
    exit 1
fi

COMMAND=$1

case "$COMMAND" in
    install)
        if [ ! -f "$SERVICE_FILE" ]; then
            echo "服务文件 $SERVICE_FILE 不存在。请重新安装程序或联系开发人员。"
            exit 1
        fi
        if [ -L "$LINK_PATH" ]; then
            # echo "软连接已存在: $LINK_PATH"
			echo "已经安装。"
        else
            sudo ln -s "$(realpath "$SERVICE_FILE")" "$LINK_PATH"
            # echo "已创建软连接: $LINK_PATH"
            sudo systemctl daemon-reexec
            sudo systemctl daemon-reload
			echo "安装成功！通过\`sudo systemctl start ${PROJECT_NAME}\`启动服务。"
        fi
        ;;
    uninstall)
        if [ -L "$LINK_PATH" ]; then
            sudo systemctl stop ${PROJECT_NAME}
            sudo rm "$LINK_PATH"
            # echo "已删除软连接: $LINK_PATH"
            sudo systemctl daemon-reexec
            sudo systemctl daemon-reload
			echo '卸载成功！'
        else
            echo "软连接不存在: $LINK_PATH"
        fi
        ;;
	run)
		nohup python server.py --server 127.0.0.1:12228 --cache &
		echo $! > server.pid
		echo 'server.py 已在后台运行。使用 `kill $(cat server.pid)` 停止它。'
        ;;
    *)
        echo "无效指令: $COMMAND"
        echo "用法: $0 {install|uninstall|run}"
        exit 1
        ;;
esac