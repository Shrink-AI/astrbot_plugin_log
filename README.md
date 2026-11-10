# astrbot_plugin_log

AstrBot 插件：把群聊 / 私聊消息按 **会话 + 天 + 大小** 分片落成结构化 JSONL，图片当场下载做内容寻址去重，供后续清洗、构造数据集、训练模型使用。

## 设计取舍

- **文件 = 会话，行 = 消息**。群聊一个群一个目录，私聊一个「QQ 对」一个目录（`a-b` 与 `b-a` 归到一起），与消息实际收发方向无关。
- **双条件滚动**：跨天必换文件；同一天内单文件超过 128MB 换 `part` 序号。
- **图片必须当场抓**。QQ 的媒体链接几小时到几天就失效，只留 URL 等于没存。落地文件按 `sha256` 命名，重复图片只存一份。
- **bot 进程不做磁盘 IO**。回调里只做一次入队，下载与写盘在后台，队列满了丢消息并计数——绝不阻塞消息处理。

## 安装

把 `astrbot_plugin_log/` 整个目录放进 AstrBot 的 `data/plugins/`：

```
AstrBot/
  data/
    plugins/
      astrbot_plugin_log/
        main.py
        metadata.yaml
        _conf_schema.json
        log_collector/
```

重启 AstrBot（或在 WebUI 插件管理里重载）。无需额外 pip 依赖，`httpx` / `aiohttp` 有其一即可（AstrBot 自带）。

## 落盘结构

```
<data_dir>/
  g/123456789/2025-09-01.000.jsonl          群 123456789 当天的消息
  g/123456789/2025-09-01.001.jsonl          同日超过 128MB 后的第 2 个 part
  p/222_10001/2025-09-01.000.jsonl          私聊（两个 QQ 号按数值排序，无方向）
  t/123456789/222_10001/2025-09-01.000.jsonl 群临时会话（与普通私聊隔离）
  media/ab/abcdef...9f.jpg                  媒体，按 sha256 内容寻址
  _manifest.jsonl                           每次新建文件的记录
  .writer.lock                              独占锁（防止两个实例同时写同一个目录）
```

默认 `data_dir` 为 `data/plugin_data/astrbot_plugin_log/logs`。

## 单行格式（schema v1）

```json
{"v":1,"type":"message","day":"2025-09-01","ts":1756700000000,"recv_ts":1756700000123,
 "self_id":"10001","msg_id":"1234","seq":567,"local_seq":42,
 "conv":{"kind":"group","id":"123456789","key":"g/123456789"},
 "sender":{"id":"111","nickname":"昵称快照","card":"群名片快照","role":"member","is_self":false},
 "msg":{"reply_to":"1233","at":["222"],"kinds":["reply","text","image"],"chars":9,
        "text":"行，那家新开的",
        "segments":[
          {"t":"reply","id":"1233"},
          {"t":"text","v":"行，那家新开的"},
          {"t":"image","url":"https://...","sha256":"ab12...","path":"media/ab/ab12...jpg",
           "bytes":34567,"mime":"image/png","err":null}
        ]}}
```

| 字段 | 说明 |
|---|---|
| `v` | schema 版本，将来演进时靠它做兼容 |
| `type` | `message` 或 `notice`（`notice` 是撤回/入群/禁言等事件） |
| `day` | 分片用的日期（按配置时区） |
| `ts` / `recv_ts` | 平台时间戳 / 本地收到时间（毫秒），用于测延迟与时钟漂移 |
| `self_id` | 哪个 bot 账号收到的，多账号部署时必须参与去重 |
| `msg_id` | 平台消息 id，去重与引用定位 |
| `seq` | 平台侧消息序号（适配器提供时才有，可能为 null） |
| `local_seq` | 本地单调序号，排序兜底 |
| `conv` | 会话：`kind` ∈ group/private/temp，`key` 即目录 |
| `sender` | `nickname`/`card` 是**当时快照**，不会随改名变化 |
| `msg.reply_to` | 引用回复指向的 `msg_id` |
| `msg.at` | 被 @ 的对象（`all` 表示 @全体） |
| `msg.kinds` | 组件类型列表，便于下游快速过滤 |
| `msg.text` | 派生纯文本（可重算），仅拼接 Plain 段 |
| `msg.segments` | 原始组件序列，富媒体信息都在这里 |

`segments` 支持的 `t`：`text`、`at`、`reply`、`face`、`image`、`record`、`video`、`file`、`forward`、`json`/`xml`、`poke`、`unknown`（未知组件保留类名与标量字段，不丢信息）。

废弃/更强的信息可以开 `store_raw_event`，把适配器原始事件整段带上。

## 配置

在 AstrBot WebUI 的插件配置页调整，或编辑 `data/config/astrbot_plugin_log_config.json`。

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 总开关 |
| `data_dir` | 空 | 数据根目录，空则用 plugin_data 约定路径 |
| `max_file_mb` | 128 | 单文件大小上限，到了就滚动 |
| `collect_group` / `collect_private` / `collect_temp_session` | true | 按会话类型开关 |
| `group_whitelist` / `group_blacklist` | `[]` | 群号白/黑名单 |
| `ignore_self` | false | 不记录 bot 自己的发言 |
| `store_raw_event` | false | 附带原始事件（体积变大） |
| `timezone` | Asia/Shanghai | 按天分片时区 |
| `download_media` | true | 是否落盘媒体 |
| `media_kinds` | `["image"]` | 需要落盘的媒体类型 |
| `media_max_mb` / `media_timeout_s` / `media_concurrency` | 20 / 10 / 4 | 媒体下载控制 |
| `queue_size` | 5000 | 内存队列，满了丢弃并计数 |
| `dedup_cache_size` | 200000 | 进程内 `(self_id, msg_id)` 去重缓存 |
| `fsync_interval_s` | 5 | 强制刷盘间隔，0 表示不 fsync |
| `log_stats_interval_s` | 300 | 统计日志间隔 |

## 预处理示例

```python
import glob, pandas as pd

# 某群某天的全部消息
df = pd.concat(pd.read_json(f, lines=True) for f in glob.glob("logs/g/123456789/2025-09-*.jsonl"))
df = df[df.type == "message"].sort_values(["ts", "local_seq"])

# 剔除 bot 自己的发言、纯表情、超短消息
df = df[~df.sender.map(lambda s: s["is_self"])]
df = df[df["msg"].map(lambda m: m["chars"] >= 4 and "face" not in m["kinds"])]

# 某人的风格语料（做风格模仿 / 人设微调）
mine = df[df.sender.map(lambda s: s["id"] == "111")]["msg"].map(lambda m: m["text"])
```

```sql
-- DuckDB 直接扫 jsonl，不用先导入
SELECT conv.id, count(*) AS n
FROM read_json_auto('logs/g/*/*.jsonl')
WHERE type = 'message'
GROUP BY 1 ORDER BY n DESC;
```

构造对话样本时，按 `(ts, local_seq)` 排序后用「时间间隔 + reply_to」切窗口：

```python
GAP_MS = 5 * 60 * 1000
turns, buf, last = [], [], None
for row in df.itertuples():
    if last is not None and row.ts - last > GAP_MS:
        if len(buf) >= 2: turns.append(buf)
        buf = []
    buf.append(row.msg["text"]); last = row.ts
```

体检工具：

```bash
python tools/inspect.py <data_dir>
```

## 注意事项

- **撤回事件依赖适配器上报**。AstrBot 的 aiocqhttp 适配器默认只派发消息事件，若你的版本不上报 `notice`，撤回就抓不到；此时仍可用 `msg_id` 在离线阶段按平台接口补删。
- **图片链接会过期**，所以 `download_media` 建议保持开启。下载失败不会丢记录，segment 里会带 `err` 字段。
- **私聊目录名本身是社交关系图**（`p/111_222/` 一眼看出谁跟谁在聊）。若在意，可在采集后离线重命名目录为 hash，并维护映射表。
- **合规**：群内采集他人发言用于训练，属于处理个人信息。建议在群内置顶声明采集行为、设置保留期、提供删除通道，原始数据不要进 git、不要放公开可访问目录。
- 数据量大时 `tools/inspect.py` 会扫全量，加 `--no-time` 可以只数行数。
- **一个 `data_dir` 只能由一个实例写**。插件启动时会在该目录拿 `.writer.lock` 独占锁；第二个实例拿不到锁时会打一条 warning（含目录路径）并停止写盘，所有记录计入 `dropped_writer`。不要为了"多开"把两个 bot 指向同一个目录——实测两个写入者交替 append 同一文件时，大块写入会被操作系统拆开后互相穿插，1%~3% 概率静默丢记录。要采集两个账号，请分别配置不同的 `data_dir`。如果数据目录在不支持字节锁的文件系统上（部分网络盘），会记一条 warning 后照常采集。热重载时若旧实例还没退干净，新实例会每 30 秒重试一次，旧实例一退出就自动接管，不需要重启 bot。

## 自测

单元测试只用标准库（`unittest`），不依赖 pytest，也不需要装 AstrBot：

```bash
python tests/run_all.py           # 全部 247 个用例
python tests/run_all.py -q        # 只打点，不逐一列用例名
python -m unittest discover -s tests -t tests   # 等价写法
python tests/smoke_test.py        # 端到端：分目录/跨天/滚动/去重/图片/撤回/半行修复/续写
python tests/sample_dump.py       # 生成小样本并打印落盘结果
```

| 文件 | 覆盖内容 |
| --- | --- |
| `test_session.py` | 会话键（群/私聊/temp）、QQ 号归一化、配对顺序、时区与跨天 |
| `test_config.py` | 配置默认值、类型容错、白/黑名单、目录解析 |
| `test_record.py` | 各类消息组件、撤回通知、原始事件、脏数据降级 |
| `test_writer.py` | 分片滚动、续写、半行修复、并发写入、队列满、关闭语义 |
| `test_media.py` | 三种下载后端、去重、超限、超时、404、魔数嗅探 |
| `test_ingestor.py` | 过滤、去重、背压、媒体开关、生命周期 |
| `test_integration.py` | 多会话混流、跨天分文件、重启续写、manifest 一致性 |
| `test_plugin.py` | 插件外壳：开关、回调不抛异常、失败闩锁、重复 terminate |
| `test_edge_cases.py` | 异常注入：写盘失败、fsync 失败、句柄打开失败、媒体异常 |
| `test_deep_branches.py` | 只剩故障才会走到的兜底分支（降级、日志自身报错等） |
| `test_robustness.py` | 随机事件 fuzz（4500 条）、2 万条长跑、多实例争锁、编码边界、`tools/inspect.py` 实测、配置/元数据一致性 |

覆盖率（存根环境，行覆盖 100%；POSIX 的 `fcntl` 分支在 Windows 上跑不到，已标注跳过）：

```bash
pip install coverage
python -m coverage run --source=astrbot_plugin_log tests/run_all.py -q
python -m coverage report -m
```

`tests/stub/` 是一个最小 AstrBot 桩，用于在没有 AstrBot 的机器上跑通内核。

> 覆盖率 100% 只说明每行都跑过，不说明线上不会出问题。真机仍需验证的两件事：aiocqhttp 适配器是否上报 `notice`（撤回）、以及一次真实的 128MB 滚动。建议上线先小范围跑一天，再用 `tools/inspect.py` 对账。