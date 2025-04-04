#!/bin/bash

# 设置你的 service 文件路径
SERVICE_FILE="./websocketforward.service"
# 设置软连接目标路径
LINK_PATH="/etc/systemd/system/websocketforward.service"

# 检查参数
if [ $# -ne 1 ]; then
    echo "用法: $0 {install|uninstall}"
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
			echo '安装成功！通过`sudo systemctl start websocketforward.service`启动服务。'
        ;;
    uninstall)
        if [ -L "$LINK_PATH" ]; then
            sudo rm "$LINK_PATH"
            # echo "已删除软连接: $LINK_PATH"
            sudo systemctl daemon-reexec
            sudo systemctl daemon-reload
			echo '卸载成功！'
        else
            echo "软连接不存在: $LINK_PATH"
        fi
        ;;
    *)
        echo "无效指令: $COMMAND"
        echo "用法: $0 {install|uninstall}"
        exit 1
        ;;
esac
