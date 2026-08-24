# 安全政策

FaceLink 仍是 Alpha 软件。Blender 桥接只应绑定 `127.0.0.1`，不要通过端口转发、代理或
修改代码把它暴露到其他网络接口。实例发现记录含有本次桥接的 Bearer Token，应按凭据
保护。回环地址和 Token 不能隔离同一操作系统账户下的恶意进程，也不能隔离与 FaceLink
处于同一 Blender Python 进程的任意 Add-on；完整假设和残余风险见
[威胁模型](docs/THREAT_MODEL.md)。

## 支持版本

只有最新 GitHub Release 获得安全修复。当前最新发布版是 0.3.9，早于 0.3.9 的版本不
受支持；`Unreleased` 中的改动尚不等于已发布支持。Blender 4.2 LTS 及以上在目标范围，
当前发布版仅完整验证 Windows x64。协议和数据迁移范围见
[兼容性与迁移政策](docs/COMPATIBILITY.md)。

当前 Windows 安装 EXE 尚未代码签名。请只从 FaceLink 官方 GitHub Release 下载，并用
同一发布中的 `SHA256SUMS.txt` 核验文件；校验和能发现文件不一致，但若发布账户或整个
下载渠道同时被攻破，它不能替代发布者代码签名和可信来源验证。

## 报告漏洞

请使用 [GitHub 私密漏洞报告](https://github.com/xuebazhou-blip/FaceLink/security/advisories/new)，
不要先开公开 Issue。报告应包含受影响的 FaceLink/Blender 版本、最小复现步骤，以及
是否需要不受信任的本地进程、Add-on、`.blend` 或外部服务。我们计划在七天内首次
确认；修复时间取决于严重性和可复现性。

不要在报告中放入真实 API Key、Bearer Token、`auth.json`、未脱敏的 `.blend`，也不要
直接附上可能含本机路径的 `facelink doctor` 原始输出。需要支持信息时先运行：

```powershell
facelink diagnostics --out facelink-diagnostics.json
```

该命令只在本地生成严格允许列表的 JSON，不自动上传；它不读取 `auth.json`，也不把
原始日志、任务日志、发现记录或场景文件复制进诊断包。为了连接本机桥接，进程最多
读取 32 个发现记录、每个最多 16 KiB；导出会排除其中的 Token、路径和场景名。即使
如此，发送前也请人工打开检查。FaceLink 从参数或进程环境读取提供商 Key，不会有意
把它写入 `.blend`、任务日志或安全诊断包。
