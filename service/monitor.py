import time
import subprocess
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# 设置你的监控目录和需要启动的程序
WATCH_DIRECTORY = "D:/test_folder"  # 请替换为你要监控的文件夹路径
COMMAND_TO_RUN = ["notepad.exe"]    # 示例：打开记事本（可以替换为你自己的程序）

class ChangeHandler(FileSystemEventHandler):
    def on_any_event(self, event):
        print(f"检测到文件夹变化: {event.event_type} - {event.src_path}")
        print("正在启动指定程序...")
        subprocess.Popen(COMMAND_TO_RUN)

def start_watching(path):
    event_handler = ChangeHandler()
    observer = Observer()
    observer.schedule(event_handler, path, recursive=True)
    observer.start()
    print(f"开始监控文件夹：{path}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        print("监控已停止。")
    observer.join()

if __name__ == "__main__":
    start_watching(WATCH_DIRECTORY)
