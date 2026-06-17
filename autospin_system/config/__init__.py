# AutoSpinmotorSystem/config/__init__.py

from .hardware_config import (
    BASE_DIR,
    LOG_DIR,
    DATA_DIR,
    CONFIG  # 引入我们刚才创建的统一 YAML 配置字典
)

__all__ = [
    "BASE_DIR",
    "LOG_DIR",
    "DATA_DIR",
    "CONFIG"
]