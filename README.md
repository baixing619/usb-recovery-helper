# U盘恢复助手 · USB Recovery Helper

一个面向 Windows 的免费、开源 U 盘保全与文件恢复工具。**先只读保存镜像，再从镜像提取、检查文件。**

起因很简单：我在网上找到的 U 盘恢复工具都需要付费，于是让 Codex AI 做了这个工具，并把代码和可运行程序公开。市面上也有免费的恢复项目；这里分享的是一次自己的需求和实现。

## 下载与使用

打开 **[Releases 下载页](https://github.com/baixing619/usb-recovery-helper/releases/latest)**，下载 `U盘恢复助手_v0.1.0_Windows_x64.zip`，完整解压后双击 `U盘恢复助手.exe`。不用安装 Python，不要只复制 EXE。

目标系统：**Windows 10/11 x64**。当前版本在 Windows 11 x64 验证；Windows 10 与其他实体电脑尚未实测。当前程序未做数字签名，请从本仓库 Releases 获取，并用同页 `SHA256.txt` 核对文件。

1. 在“① U盘只读保全”页选择源设备，把镜像保存到另一块健康的物理磁盘。软件会检查源和目标，拒绝同盘采集。
2. 在“② 已有镜像恢复”页选择镜像及对应读取记录（如有），识别并选择卷，再指定新的输出文件夹。
3. 核对报告中的“结构检查通过”“修复候选”“残缺”“未确认”和“历史候选”。重要文档仍需用 WPS/Office 打开检查内容。

首次练习可导入随包 `示例/虚构演示U盘.img`，无需接触真实 U 盘。

## 首版包含什么

- USB 整盘/分区只读镜像；ddrescue 读取记录、安全停止，以及可信终止快照的同任务续接。
- 裸卷、MBR、GPT 镜像识别；exFAT 当前与历史目录候选提取。
- Office 新格式的 ZIP、CRC、XML 与关系检查；保守的修复候选及残缺静态可读视图。
- OLE/BIFF 结构检查；连续完整 OOXML 候选深度搜索。
- CSV、HTML、JSON 核对报告与文件 SHA256。
- 本机处理，无账号登录、上传或付费解锁步骤。

主界面不会在启动时自动扫描 U 盘。软件不格式化、不修复原盘、不执行宏，也不重算业务公式。

## 已验证与限制

0.1.0 实际验证包括：最终 ZIP 解压后的 EXE、含中文/空格路径、隔离 PATH、实际引擎在合成文件上的停止/续接、源哈希不变、原生中文窗口的演示恢复和扫描停止、报告内容与证据链接。源码测试 113 项，0 失败、1 项因 Windows 原生符号链接权限不足跳过。此前已保存镜像中 86 份原提取件的 SHA256 在新结果中全部匹配。

这些结果**不代表对所有故障 U 盘的硬件验收，也不代表 100% 恢复率**。本次开发未操作原故障设备；热插拔、UAC 和其他实体电脑尚未实测。文档结构通过不等于内容、金额、版本或历史归属正确。

- 首版不提供 NTFS/FAT32 目录重建；Office 连续候选深搜的结果取决于保存方式。
- ZIP64、加密内容及无法证明的碎片化 ZIP 不判完整。旧 Word/PPT 仅作 OLE 流检查。
- 无读取记录时，镜像读取完整度为未知；未读范围不会当作真实零数据。
- 强制结束/断电后缺少可信闭合快照时，保留镜像和 map，保守拒绝自动续接。
- 镜像分析停止后保留部分结果；继续扫描会新建任务，暂不支持增量续扫。
- 内存与候选数量有安全上限；达到上限会标记部分结果。

详见 [使用说明](docs/使用说明.txt) 与 [版本和范围](docs/版本与范围.txt)。

## 从源码运行或构建

使用带 tkinter 的 64 位 Python 3.12。实际构建使用 3.12.14。

```powershell
py -3.12 -m venv 开发环境
开发环境\Scripts\python.exe -m pip install -r requirements-build.txt
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
开发环境\Scripts\python.exe launcher.py
开发环境\Scripts\python.exe -m unittest discover -s tests -v
开发环境\Scripts\python.exe tools\build.py
```

打包输出位于新的 `构建/时间戳/dist/U盘恢复助手`。`tools/verify_portable.py` 可复制整包到新目录后运行实际 EXE 自检。测试只应使用合成数据或已保全的镜像副本。

仓库保留经校验的 GNU/Cygwin 引擎及许可证。7 份第三方对应源码归档在 Releases 的 `U盘恢复助手_v0.1.0_源码与第三方源码.zip` 中；官方来源、版本和校验值见 [engine-manifest.json](resources/engine-manifest.json)。[第三方说明](docs/third_party.md) 介绍资源准备流程。

## 许可与 AI 制作说明

本项目原创 Python 代码采用 [MIT License](LICENSE)。GNU ddrescue、Cygwin 及其他依赖保留各自许可证，不因本仓库的 MIT 许可而改变；见 [第三方许可说明](THIRD_PARTY_NOTICES.md)。

项目由账号持有人提出需求，Codex AI 完成代码编写、验证与整理。配套介绍视频的脚本、画面、合成配音和剪辑由 Codex AI 制作，并明确标记 AI 制作。仓库、程序包与演示资料不包含真实恢复文件、真实磁盘镜像或账号凭据。

欢迎提交可复现问题。公开 Issue 请使用虚构样本和去敏信息，不要上传真实客户文档或完整故障磁盘镜像。
