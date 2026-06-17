# AutoSpinmotorSystem/config/hardware_config.py
import os
import yaml

# 1. 目录管理 (保留在代码中)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_FILE = os.path.join(BASE_DIR, "config", "system_config.yaml")

for _dir in [LOG_DIR, DATA_DIR]:
    if not os.path.exists(_dir):
        os.makedirs(_dir)


# 2. 读取 YAML 配置文件
def load_system_config():
    if not os.path.exists(CONFIG_FILE):
        raise FileNotFoundError(f"配置文件未找到: {CONFIG_FILE}")
    with open(CONFIG_FILE, 'r', encoding='utf-8') as file:
        return yaml.safe_load(file)


# 3. 暴露给系统其他模块使用
CONFIG = load_system_config()


