#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Windows环境下批量运行 SkyAgg_GRID_DB2.py build
遍历所有points文件，分别使用mode1和mode2构建
支持将运行结果保存到日志文件
"""

import subprocess
import sys
import os
from pathlib import Path
import glob
import time
from datetime import datetime
import psutil
import threading

class MemoryMonitor:
    def __init__(self, interval=0.5):
        self.interval = interval
        self.peak_memory = 0
        self.running = False
        self.thread = None
    
    def start(self):
        """启动监控线程"""
        self.running = True
        self.peak_memory = 0
        self.thread = threading.Thread(target=self._monitor)
        self.thread.daemon = True
        self.thread.start()
    
    def _monitor(self):
        while self.running:
            mem = psutil.virtual_memory()
            # 记录物理内存的已用量（MB）
            current = mem.used / 1024**3  # GB
            if current > self.peak_memory:
                self.peak_memory = current
            time.sleep(self.interval)
    
    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=1)
    
    def get_peak(self):
        return self.peak_memory

class Logger:
    """日志记录器"""

    def __init__(self, log_dir="./logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = None
        self.log_path = None

    def init_log(self, suffix=""):
        """初始化日志文件"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_name = f"build_log_{timestamp}{suffix}.txt"
        self.log_path = self.log_dir / log_name
        self.log_file = open(self.log_path, 'w', encoding='utf-8')

        # 写入日志头
        self.log("=" * 80)
        self.log(f"批量构建日志 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.log("=" * 80)
        self.log("")

        return self.log_path

    def log(self, message, console=True):
        """写入日志"""
        if self.log_file:
            self.log_file.write(message + '\n')
            self.log_file.flush()
        if console:
            print(message)

    def close(self):
        """关闭日志文件"""
        if self.log_file:
            self.log_file.close()
            self.log_file = None


def get_txt_files(data_dir, pattern="*.txt"):
    """获取所有txt文件"""
    search_pattern = os.path.join(data_dir, pattern)
    files = glob.glob(search_pattern)
    return sorted(files)  # 排序保证顺序一致


def run_build(txt_path, mode, pi_avg, pi_min, export_prefix="/SkyDB/skyagg_griddb",
              logger=None, verbose=True):
    """
    运行单个build命令

    Args:
        txt_path: txt文件路径
        mode: mode1 或 mode2
        export_prefix: 输出前缀
        logger: 日志记录器
        verbose: 是否打印详细信息
    """
    # 获取文件名（不含扩展名）
    file_name = Path(txt_path).stem

    # 构建输出路径
    output_path = f"{export_prefix}_{file_name}_{mode}_{int(pi_avg*100)}_{int(pi_min*100)}"

    cmd = [
        sys.executable,
        "SkyAgg_GRID_DB2.py",
        "build",
        "--data", txt_path,
        "--mode", mode,
        "--pi-avg", str(pi_avg),
        "--pi-min", str(pi_min),
        "--export-prefix", output_path
    ]

    # 构建日志消息
    log_msgs = []
    log_msgs.append(f"\n{'=' * 80}")
    log_msgs.append(f"文件: {Path(txt_path).name}")
    log_msgs.append(f"模式: {mode}")
    log_msgs.append(f"输出: {output_path}")
    log_msgs.append(f"命令: {' '.join(cmd)}")
    log_msgs.append('=' * 80)

    # 输出到控制台和日志
    for msg in log_msgs:
        if logger:
            logger.log(msg, console=verbose)
        elif verbose:
            print(msg)

    try:
        # Windows下避免弹出窗口
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        start_time = time.time()

        # 捕获输出
        # result = subprocess.run(
        #     cmd,
        #     capture_output=True,
        #     text=True,
        #     check=False,
        #     startupinfo=startupinfo
        # )

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            startupinfo=startupinfo,
            env=os.environ.copy()  # 复制环境变量，隔离内存
        )

        stdout, stderr = process.communicate()  # 等待结束
        returncode = process.returncode

        # 显式删除进程对象，触发垃圾回收
        del process

        elapsed = time.time() - start_time

        # 记录stdout和stderr
        if stdout:
            if logger:
                logger.log(f"\n[STDOUT]\n{stdout}", console=False)
            elif verbose:
                print(f"\n[STDOUT]\n{stdout}")

        if stderr:
            if logger:
                logger.log(f"\n[STDERR]\n{stderr}", console=False)
            elif verbose:
                print(f"\n[STDERR]\n{stderr}")

        if returncode == 0:
            success_msg = f"✅ {Path(txt_path).name} [{mode}] 执行成功 ({elapsed:.2f}s)"
            if logger:
                logger.log(success_msg)
            else:
                print(success_msg)
            return True
        else:
            fail_msg = f"❌ {Path(txt_path).name} [{mode}] 执行失败，返回码: {returncode} ({elapsed:.2f}s)"
            if logger:
                logger.log(fail_msg)
            else:
                print(fail_msg)
            return False

    except FileNotFoundError:
        error_msg = f"❌ 错误: 找不到 SkyAgg_GRID_DB2.py"
        if logger:
            logger.log(error_msg)
        else:
            print(error_msg)
        return False
    except Exception as e:
        error_msg = f"❌ {Path(txt_path).name} [{mode}] 执行异常: {e}"
        if logger:
            logger.log(error_msg)
        else:
            print(error_msg)
        return False


def main():
    """主函数"""
    # 设置控制台编码为UTF-8（Windows）
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    # 创建日志记录器
    logger = Logger(log_dir="./logs")
    log_path = logger.init_log()

    logger.log("=" * 80)
    logger.log("批量构建 SkyAgg_GRID_DB2 (支持 mode1 和 mode2)")
    logger.log("=" * 80)
    logger.log("")

    # 1. 检查脚本文件
    script_path = Path("SkyAgg_GRID_DB2.py")
    if not script_path.exists():
        logger.log(f"❌ 错误: 找不到 {script_path}")
        logger.log(f"当前目录: {os.getcwd()}")
        logger.close()
        input("按回车键退出...")
        sys.exit(1)

    # 2. 获取所有txt文件
    data_dir = "./data/dataset/"
    txt_files = get_txt_files(data_dir)

    if not txt_files:
        logger.log(f"❌ 错误: 在 {data_dir} 目录下找不到任何 .txt 文件")
        logger.log(f"当前目录: {os.getcwd()}")
        logger.close()
        input("按回车键退出...")
        sys.exit(1)

    logger.log(f"📁 找到 {len(txt_files)} 个txt文件:")
    for f in txt_files:
        logger.log(f"  - {Path(f).name}")
    logger.log("")

    # 3. 检查输出目录
    out_dir = Path("./out")
    if not out_dir.exists():
        logger.log(f"📁 创建输出目录: {out_dir}")
        out_dir.mkdir(parents=True, exist_ok=True)

    # 4. 准备运行参数
    modes = ["mode1", "mode2"]
    param_sets = [
        # {"name": "Default", "pi_avg": 0.75, "pi_min": 0.50},
        # {"name": "Loose", "pi_avg": 0.80, "pi_min": 0.60},
        # {"name": "Medium", "pi_avg": 0.90, "pi_min": 0.80},
        {"name": "Strict", "pi_avg": 0.95, "pi_min": 0.90},
    ]
    total_tasks = len(txt_files) * (1 + 1)

    logger.log(f"📊 总任务数: {total_tasks} ({len(txt_files)} 个文件 × {len(modes)} 个模式 (共 1 + 1 个参数))")
    logger.log("")

    # 5. 执行所有任务
    success_count = 0
    failed_tasks = []

    task_id = 0
    for txt_path in txt_files:
        file_name = Path(txt_path).stem
        for mode in modes:
            for param_set in param_sets:
                if mode == "mode2" and param_set["name"] == "Default":
                    continue
                task_id += 1
                logger.log(f"\n[{task_id}/{total_tasks}] 处理: {file_name} [{mode}]")

                monitor = MemoryMonitor(interval=0.5)
                monitor.start()
                if run_build(txt_path, mode, param_set["pi_avg"], param_set["pi_min"], export_prefix="/SkyDB/skyagg_griddb", logger=logger):
                    success_count += 1
                else:
                    failed_tasks.append((file_name, mode))
                mem = psutil.virtual_memory()
                swap = psutil.swap_memory()
                logger.log(f"💾 系统内存: 已用 {mem.used / 1024 ** 3:.1f}GB / 总计 {mem.total / 1024 ** 3:.1f}GB")
                logger.log(f"🔄 交换内存: 已用 {swap.used / 1024 ** 3:.1f}GB / 总计 {swap.total / 1024 ** 3:.1f}GB")
                monitor.stop()
                logger.log(f"📊 峰值内存: {monitor.get_peak():.1f}GB")
                if mode == "mode1":
                    break
                

    # 6. 输出统计
    logger.log("")
    logger.log("=" * 80)
    logger.log("批量构建完成!")
    logger.log("=" * 80)
    logger.log(f"总任务数: {total_tasks}")
    logger.log(f"成功: {success_count}")
    logger.log(f"失败: {len(failed_tasks)}")

    if failed_tasks:
        logger.log("\n❌ 失败的任务:")
        for file_name, mode in failed_tasks:
            logger.log(f"  ❌ {file_name} [{mode}]")

    # 7. 列出生成的文件
    logger.log("\n📁 生成的文件:")
    output_files = glob.glob("/SkyDB/skyagg_griddb_*")
    if output_files:
        for f in sorted(output_files):
            # 显示文件大小
            size = Path(f).stat().st_size
            if size < 1024:
                size_str = f"{size} B"
            elif size < 1024 * 1024:
                size_str = f"{size / 1024:.1f} KB"
            else:
                size_str = f"{size / (1024 * 1024):.1f} MB"
            logger.log(f"  - {Path(f).name} ({size_str})")
    else:
        logger.log("  (没有生成任何文件)")

    logger.log("=" * 80)
    logger.log(f"📝 日志文件: {log_path}")
    logger.close()

    print(f"\n📝 日志已保存到: {log_path}")

    sys.exit(0 if len(failed_tasks) == 0 else 1)


if __name__ == "__main__":
    main()
