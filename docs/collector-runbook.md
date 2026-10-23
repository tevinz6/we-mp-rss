# 公众号采集工作台：运行说明

本分支基于 We-MP-RSS 提交 `9fd4edb2a9311bffa571db050f53f415705db294`。新增受控采集入口，保留上游代码供追溯，不运行上游默认 `main.py`。

## 已实现

- 管理员初始化/登录、公众号搜索与订阅、单篇链接导入、定时增量和分批历史采集。
- 来源暂停/恢复；认证、频控、验证码、未知接口错误的分类处理。
- 数据库持久任务、单 Worker、来源互斥、租约/心跳、幂等写入、重启恢复。
- 列表与正文分离、正文补采、不可变正文版本、可选图片归档、归档阅读、中文子串搜索和日期筛选。
- JSON/Markdown/RSS 导出、站内异常提醒、SQLite 一致性备份、文件哈希校验与恢复。
- 真实公众平台、微信读书公众号适配器与完全离线的模拟来源；公众平台支持可选 Playwright 渲染。

真实微信登录、列表覆盖和账号限制尚需用户扫码实测。本版本不承诺无封号风险或完整历史覆盖。Word/PDF 与外部消息渠道不在本次默认运行路径；以站内异常记录提供提醒。

## 与原设计的具体调整

上游数据库初始化、登录模块和后台调度存在全局副作用，并有输出凭据的路径。为保证所有采集受同一任务系统控制，本版本在同一仓库内增加独立启动入口和 Vue 管理界面，复用上游确认过的登录/接口流程，并重写为无副作用适配器，不挂载旧 API。

数据库使用独立的 `data/collector/collector.db`，不直接修改历史 We-MP-RSS 数据库；已有上游库必须单独导入，不能把它直接作为新数据库。启动时检测并拒绝未受迁移管理的已有库。这比自动对未知旧库执行迁移更可控，但已提供 `import-upstream` 命令只读导入上游 SQLite 的公众号、文章和已有正文；不会复制凭据，导入公众号默认暂停。MySQL 等来源仍需专项适配。

## 本地开发

要求 Python 3.13、Node.js 22+；实际依赖固定在 Python 哈希锁文件及前端 npm 锁文件。

```sh
python3.13 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-collector.lock
cd web_ui/collection
npm ci --include=optional
npm run build
```

回到仓库根目录，启动网页：

```sh
.venv/bin/python -m core.collection.cli serve
```

另开一个终端启动采集服务：

```sh
.venv/bin/python -m core.collection.cli worker
```

访问 `http://127.0.0.1:8001`，首次设置至少 12 位管理员密码。没有默认管理员密码。进入“来源与授权”扫码；在“公众号”搜索、确认目标并添加，然后“检查新文章”。授权仅保存在本机，不能通过聊天发送 Cookie。

如需要浏览器渲染：先运行 `.venv/bin/python -m playwright install chromium`（Linux 系统依赖可用 `install --with-deps chromium`），再为两个服务设置 `COLLECTOR_BROWSER=true`。只有普通页面缺少正文时尝试浏览器；认证、频控、验证错误不会触发备用方式继续采集。

### 离线演示

演示与真实来源必须使用不同数据目录。以下变量需要在执行 demo、serve、worker 的对应终端中一致设置：

```sh
export COLLECTOR_DATA_DIR=./data/demo
export COLLECTOR_MOCK=true
export COLLECTOR_REQUEST_INTERVAL_SECONDS=0
.venv/bin/python -m core.collection.cli demo
.venv/bin/python -m core.collection.cli serve
```

demo 命令生成两个演示公众号、六篇模拟正文并归档，完全不访问微信。后续在界面触发采集仍需运行 worker。进入真实模式时另选空目录，取消 `COLLECTOR_MOCK`；系统会拒绝混用模拟数据和真实来源。

## Docker 部署

从仓库根目录运行：

```sh
docker compose -f deploy/collector.compose.yaml build
docker compose -f deploy/collector.compose.yaml up -d
docker compose -f deploy/collector.compose.yaml logs --tail=100 web worker
```

默认绑定本机 `127.0.0.1:8001`，命名数据卷持久保存数据库、归档和凭据；容器使用非 root 用户。第一次打开工作台创建密码后再授权。

如需配置，复制 `deploy/collector.env.example` 到本机 `.env`，并用 `docker compose --env-file .env -f deploy/collector.compose.yaml ...` 启动。公网访问不属于默认配置，需自行部署 HTTPS 反向代理并启用安全 Cookie，不要直接开放旧 We-MP-RSS 入口。

依赖锁固定，但 Docker 基础镜像使用精确版本标签而非内容摘要；首次成功构建后可记录镜像摘要用于部署和回滚。当前开发机器 Docker daemon 未运行，因此本次不能声称容器构建和容器内浏览器已验证。

## 任务行为

- 默认每公众号 6 小时检查；每轮 2 页；应用层请求至少间隔 20 秒；全局一个采集任务。
- “补采近 30 天”每次只处理 2 页并保存位置。列表仍有更多内容时标记“部分完成”，继续补采会重叠上一页后推进。不是一次性承诺回溯完所有历史。
- 认证过期：等待重新扫码。频率受限：至少冷却 6 小时，再由管理员恢复；验证要求和未知来源变化需要人工检查。
- 暂态网络失败：5 分钟、30 分钟重试；连续网络失败会暂停来源。
- Worker 重启：待办仍在数据库；正在执行但失去心跳的任务在租约过期（180 秒）后重排。
- 正文“已保存”不代表图片已归档。图片开关默认关闭；开启后生成独立图片任务。音视频不做离线归档。
- 上游新增/置顶/删除会影响分页，历史覆盖以实际发现记录为准。日期缺失时不伪造发布时间。

## 备份与恢复

```sh
.venv/bin/python -m core.collection.cli backup --target /path/to/new-backup-directory
.venv/bin/python -m core.collection.cli restore --source /path/to/backup-directory --target /path/to/new-data-directory
```

目标必须是新目录，避免覆盖现有数据。备份包含一致性数据库快照及引用的正文/图片，manifest 记录 SHA-256。恢复时校验每个文件；恢复后在新目录启动并重新扫码。

备份默认不包含微信凭据和加密密钥，避免把有效会话复制到其他环境。管理员密码的不可逆哈希在数据库备份中，因此还原后仍用原管理员密码登录，但原浏览器会话失效。忘记密码时在服务器本地运行 `reset-password`，按隐藏输入提示重设。

真实模式的 Worker 每天北京时间 03:30 自动备份，默认写入数据目录的 backups；可用 COLLECTOR_BACKUP_DIR 指定其他本机挂载位置。自动备份保留最近 7 份日备份及 4 个周快照，仅清理带有效备份 manifest 的日期目录。请将备份同步到另一设备；同盘备份不能防磁盘损坏。手动指定目录的备份不会被默认自动清理。

## 测试

```sh
.venv/bin/pip install pytest==8.3.5 pytest-cov==6.1.1
.venv/bin/python -m pytest tests/collection -q
cd web_ui/collection
npm run build
```

真实验收需要：3～5 个目标公众号及文章链接；用户本人扫码；连续观察新增文章、正文完整性、重新授权恢复与频率限制。未完成这些步骤前，仅能判定工程机制通过本地测试，不能判定微信来源长期稳定。

## 故障处理

| 现象 | 处理 |
|---|---|
| 任务一直等待 | 确认 worker 已启动、来源处于“可采集”、公众号已启用 |
| 登录过期 | 在来源页重新扫码，成功后自动释放等待授权的任务 |
| 访问频繁 | 不重复点击采集；等待冷却并检查请求量，再手动恢复 |
| 未找到正文 | 查看任务错误；页面结构可能变化，不要把登录页当正文 |
| UI 未出现 | 执行前端构建；新入口使用 `static/collector` |
| SQLite 锁冲突 | 确认数据库在本地盘、仅一个 worker；不要在 NFS/SMB 上运行 |
| 端口冲突 | `serve --port 其他端口`；Docker 使用 COLLECTOR_PORT |

## 数据安全边界

新入口不输出 Cookie/Token；凭据加密写入本机权限受限目录。密钥与数据同机保存只能防止意外文本泄露，不能抵御整台主机被控制；备份不包含密钥。文章 HTML 经过白名单清理，图片只允许指定微信 CDN，导入仅接受微信 HTTPS 地址，重定向重新校验。

不要把真实配置、data、密钥或浏览器 profile 提交到 Git。新入口关闭请求访问日志，避免 RSS 等只读链接凭据被写入访问日志。

## 从原 We-MP-RSS SQLite 导入

先对原库做一致性备份，再导入该备份文件：

```sh
.venv/bin/python -m core.collection.cli import-upstream --source /path/to/upstream-snapshot.db
```

该命令只读原文件；按来源标识去重，清洗已有正文并建立归档版本。缺少归属公众号、无效文章地址会计入 skipped；已导入记录再次执行不会重复。导入的订阅默认暂停，请确认采集来源已授权、标识可用，再手动启用。

RSS 订阅地址可以在公众号卡片复制，包含仅能读取该公众号归档的随机令牌；普通文章 API 仍需要登录。通过 POST /api/v1/collection/feeds/{id}/rss-link?rotate=true 可撤销旧链接并生成新链接。


## 微信读书公众号渠道（2026-10-22）

1. 在「来源与授权」选择「微信读书」，点击「扫码授权」，用微信确认登录微信读书。二维码有效期为 4 分钟；页面串行检查登录结果，不重叠长轮询。
2. 登录成功必须同时通过书架接口验证。仅扫码成功、但书架返回错误时，不会覆盖已有凭据。原始错误码与接口路径显示在异常信息中；不记录 Cookie、Token、二维码 UID 或完整响应。
3. 在「公众号」选择「微信读书书架」，按公众号名称查找并添加。搜索范围仅限当前登录账号的书架，不是全网搜索。若没有结果，请先在微信读书内确认是否收录、关注目标公众号。也可以展开手动添加，填写 `MP_WXS_数字` 形式 bookId。对已有公众平台订阅，可按固定上游 `apis/mps.py` 的规则将 fakeid 作 Base64 解码得到数字后加 `MP_WXS_` 前缀；这是标识映射，不证明微信读书实际收录，仍需验证返回。
4. 添加后可点「检查新文章」。列表和正文均通过微信读书获取；订阅名称旁显示来源。分页 offset 按发布组递增，不能套用公众平台每页 5 组的断点规则。未返回明确结束标记时，继续到空页才能确认当前列表结束。
5. 自动检查默认每 6 小时；每轮默认 2 页。新增文章的正文分别排队，至少间隔 20 秒。正文、版本、搜索、JSON/Markdown/RSS 导出沿用现有归档流程。

两个来源分别保存状态、凭据、请求间隔和冷却截止时间；仍使用单 Worker 与互斥锁。公众平台受限不会暂停微信读书，微信读书受限也不会修改公众平台状态。没有账号轮换或自动换渠道。列表失败时不会自动改用最新文章模式，需在订阅设置明确选择「仅最新一篇」并保存，再手动恢复来源。微信读书订阅与同名公众平台订阅是两条独立记录，不自动合并归档。

微信读书使用独立加密凭据 `auth/weread.enc`；待扫码信息也加密保存。不需要在聊天或配置文件中粘贴 Cookie。新依赖 `qrcode==8.2` 在本机生成 SVG 二维码，不调用第三方二维码服务。

`-2012`、`-2010` 会暂停并要求检查登录状态或访问权限，不能单凭这些错误断言封号或配额耗尽；`-2041` 与未知错误会暂停待排查。HTTP 429 冷却至少 6 小时。不会将错误或缺失的 reviews 字段当作空列表成功。

单篇 `mp.weixin.qq.com` 链接导入请归属公众平台订阅。微信读书正文需要该渠道返回的 reviewId，不能只凭微信短链可靠推导。默认不改为请求微信原文作为回退。

真实验收步骤与当前结果见 [微信读书接入验收](weread-validation.md)。


### 仅最新一篇模式与本机实测结果

2026-10-22 实测「猫笔刀」：列表接口 `/web/mp/articles` 返回 `-2041`；用户授权后 `/api/mp/cover` 能返回最新文章，`/web/mp/content` 成功取得正文。已将本机该微信读书订阅设置为「仅最新一篇」，仍每 360 分钟检查一次。公众平台保持原来的频率受限状态。

此模式只读取当次接口返回的一篇文章，保存正文并去重，不能补采历史、不能保证两次检查之间没有遗漏，也不代表接口返回的一定是微信端最新发表的文章。来源未给出发布时间时保留空值，不用采集时间冒充发布时间。图片默认保留远程链接，并未下载归档。

数据库迁移 `002` 增加 `feeds.collection_mode`，已有订阅默认保持列表模式。切换模式会取消旧的列表任务、清零历史断点；正在运行的任务结束前不允许切换。历史补采在最新一篇模式下由 UI 和 API 同时禁用。`coverage=latest_only` 是覆盖范围标识；任务中的 `exhausted=true` 仅表示该模式本轮已结束，不代表完整历史已经取完。
