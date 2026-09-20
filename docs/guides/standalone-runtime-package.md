# AutoSpin 独立运行包与树莓派部署

## 结论

`autospin` 的维护版运行程序不再读取同级 `AutoSpinmotorSystem`。网页生成和执行的
实验配方统一位于 `recipes/`。顶层 `firmware/`、`hardware/` 和
`autospin_system/` 不需要上传到树莓派运行目录，但建议继续保留在开发仓库中：

- `firmware/` 用于重新编译、烧录和恢复 GRBL 固化参数；
- `hardware/` 保存手册、图纸、照片和采购资料；
- `autospin_system/` 是待退役历史代码；
- `src/hardware/` 是现行运行代码，必须上传。

## 配方目录

默认目录是：

```text
/home/pi/autospin/recipes
```

如需把配方保存在系统盘之外，可在启动服务前设置：

```bash
export AUTOSPIN_RECIPES_DIR=/home/pi/autospin-data/recipes
```

网页“生成并保存多轮 recipe”和“运行已保存 recipe”始终使用同一个解析后的目录。
修改或复制 JSON 配方后无需重新编译代码；下一次提交运行时会重新读取文件。

## 构建最小运行包

在 Windows PowerShell 中进入 `autospin` 后执行：

```powershell
D:\python\python3.11.0\python.exe tools\build_runtime_package.py --output dist\autospin-runtime-20260920
```

输出目录必须不存在。脚本不会覆盖或删除旧包。包含清单见
`deploy/runtime-package.json`。运行包含 `src/hardware/`，但不含顶层
`firmware/`、`hardware/`、`autospin_system/`、测试和迁移快照。

## 上传

首次部署或需要同步完整最小包时：

```powershell
scp -O -r dist\autospin-runtime-20260920\* pi@192.168.50.2:/home/pi/autospin/
```

只修改配方时：

```powershell
scp -O recipes\perovskite_experiment.json pi@192.168.50.2:/home/pi/autospin/recipes/perovskite_experiment.json
```

只修改网页或 Python 源码时仍可按文件逐个上传，例如：

```powershell
scp -O src\webapp\app.py pi@192.168.50.2:/home/pi/autospin/src/webapp/app.py
```

上传 Python 源码或配置后，必须停止正在运行的 Web 进程并重新启动；只刷新浏览器
不能重新加载后端模块。启动命令：

```bash
cd /home/pi/autospin
source .venv/bin/activate
python3 tools/run_webserver.py --host 127.0.0.1 --port 8800
```

部署前应先备份树莓派现有目录；本构建脚本不会替用户执行远程覆盖、删除或重启。
