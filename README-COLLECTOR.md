# 公众号采集工作台

在固定版本 We-MP-RSS 上开发的单机采集工作台：网页管理、定时订阅、持久化任务、正文与可选图片归档、异常暂停、备份恢复。

**本次新增运行入口是 `core.collection.cli`，不要用原项目 `main.py` 启动此版本。**

- [安装、运行、部署和恢复说明](docs/collector-runbook.md)
- [验收报告](docs/collector-acceptance.md)
- [微信读书渠道接入与验证](docs/weread-validation.md)
- [容器配置](deploy/collector.compose.yaml)
- [后端测试](tests/collection/test_collector.py)

本机依赖安装、前端构建完成后，可以同时启动网页和 Worker：

```sh
.venv/bin/python tools/start_collector.py
```

离线演示：

```sh
.venv/bin/python tools/start_collector.py --demo --port 8002
```

访问启动时显示的本地地址，首次设置管理员密码。真实模式需用户本人扫码授权；演示模式明确标记且不请求微信。

数据库、凭据与运行文件保存在忽略提交的 `data/` 中。模拟与真实来源使用不同数据库。未进行真实账号验证之前，不能据此承诺历史覆盖、来源长期可用性或无账号限制风险。
