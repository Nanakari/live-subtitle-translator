首个 Windows x64 便携版，包含本轮 review 的全部已确认问题修复。

## 下载与运行

下载 `LiveSubtitleTranslator-v0.1.0-windows-x64.zip`，完整解压，在程序旁将 `config.local.example.yaml` 复制为 `config.local.yaml` 并填写自己的 Gemini API key，然后双击 `LiveSubtitleTranslator.exe`。无需安装 Python。也可以通过 `GEMINI_API_KEY` 环境变量提供密钥。

支持 Windows 10/11 x64，需要立体声播放设备、网络和可使用所配置 Live 模型的 Gemini API key。系统播放音频会发送至 Gemini，API 使用可能产生费用。程序未签名，Windows 可能显示未知发布者提示。

## 修复

- 使用播放端点 ID 选择回环设备，多声道采集后下混，避免单声道 WASAPI 问题。
- 为连接、发送和清理设置超时，暂停及停止可取消在途网络操作。
- 永久 HTTP 错误停止重试；被拒绝的恢复句柄回退到新会话。
- 正常无译文不再误触发输出 watchdog；新会话隔离字幕，成功恢复保留原会话状态。
- Original only 独立提交原文，保留新增句子、重复表达和英文分片边界。
- 设置保存失败不阻止退出；日志异常链和堆栈脱敏，正文日志默认关闭。

## 验证与说明

- 109 项自动测试通过，编译及依赖检查通过。
- 在干净 Windows x64 / Python 3.12 环境构建，便携版通过离线独立可执行文件测试，覆盖运行依赖、默认配置、SDK 配置构建和 Tk 初始化。
- 未进行真实 Gemini 请求、音频采集或长时间运行联调。
- 发布包不包含 API key、私人配置、桌面状态或日志。
- `SHA256SUMS.txt` 提供 ZIP 校验值。升级时将旧版的私人配置和可选桌面状态复制到新解压目录。
