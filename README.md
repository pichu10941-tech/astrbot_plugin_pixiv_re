# astrbot_plugin_pixiv_re

AstrBot 插件，从本地 Pixiv 图库 API 获取图片，支持按标签、画师、作品 ID 等条件筛选，以及定时自动推送订阅。

## 依赖

本插件需要配合本地 Pixiv 图库服务使用，该服务需提供 `GET /api/fetch` 接口，接口规范参见 [FETCH_API.md](./FETCH_API.md)。

## 安装

在 AstrBot WebUI 的插件市场中搜索安装，或手动克隆到 `data/plugins/` 目录：

```bash
cd AstrBot/data/plugins
git clone <仓库地址> astrbot_plugin_pixiv_re
```

## 配置

安装后在 WebUI 插件配置页面设置以下参数：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `api_base_url` | `http://localhost:8282` | 本地图库服务地址 |
| `default_count` | `1` | 默认每次返回图片数量（1-10） |
| `default_cooldown` | `1d` | 默认防重复冷却时长，留空不启用 |
| `use_thumbnail` | `false` | 是否使用缩略图 |
| `show_info` | `true` | 是否在图片下方附带作品信息 |
| `subscriptions` | `[]` | 定时推送订阅列表（可在 WebUI 直接管理） |

## 指令

所有指令以 `/pixiv` 开头。

### 图片获取

| 指令 | 说明 |
|---|---|
| `/pixiv random [数量]` | 随机获取图片 |
| `/pixiv tag <标签...> [数量]` | 按标签搜索，多个标签取 AND，最后一个纯数字视为数量 |
| `/pixiv author <画师ID> [数量]` | 按画师 ID 搜索 |
| `/pixiv pid <作品ID> [页码]` | 按作品 ID 精确获取，页码从 0 开始 |
| `/pixiv search [参数]` | 高级搜索，支持完整参数组合 |

`/pixiv search` 参数：

```
-t  <标签>      筛选标签（可重复）
-e  <标签>      排除标签（可重复）
-a  <画师ID>    指定画师
-n  <数量>      返回数量
-p  <页码>      指定页码
-c  <冷却>      冷却时长，如 12h / 1d
```

示例：
```
/pixiv search -t girl -t solo -e nsfw -n 3 -c 12h
```

### 定时推送订阅

| 指令 | 权限 | 说明 |
|---|---|---|
| `/pixiv sub origin` | 所有人 | 查看当前会话标识，用于在 WebUI 填写推送目标 |
| `/pixiv sub list` | 所有人 | 列出当前会话的所有订阅及下次推送时间 |
| `/pixiv sub add <间隔> [参数]` | 管理员 | 创建定时推送订阅 |
| `/pixiv sub del <订阅ID>` | 管理员 | 删除指定订阅 |
| `/pixiv sub clear` | 管理员 | 清空当前会话所有订阅 |

`/pixiv sub add` 间隔格式：`30m` / `2h` / `1d` / `7d` / `2w`（最小 10 分钟）

`/pixiv sub add` 参数与 `/pixiv search` 相同（`-t` `-e` `-a` `-n` `-c`），无需 `-p`。

示例：
```
/pixiv sub add 6h                          每 6 小时内随机推送 1 张
/pixiv sub add 1d -t girl -t solo -n 2     每天随机推送 2 张含 girl+solo 标签的图
/pixiv sub add 12h -a 12345 -c 7d          按画师推送，7 天冷却防重复
```

### 在 WebUI 中管理订阅

1. 在目标群/私聊中发送 `/pixiv sub origin`，获取会话标识字符串
2. 进入 AstrBot WebUI → 插件配置 → 本插件 → `subscriptions`
3. 点击添加，填写会话标识和推送参数
4. 保存后立即生效，无需重启

通过指令创建的订阅也会同步显示在 WebUI 配置中，可在 WebUI 直接修改或删除。

## 推送机制

- 每个订阅设置一个时间间隔，插件在该间隔内**随机选择一个时刻**触发推送
- 触发后立即重新随机计算下次触发时间，循环往复
- 推送状态持久化，重启 AstrBot 后自动恢复，不会因重启导致立即全量触发
- 单个订阅推送失败不影响其他订阅

## 冷却说明

冷却由本地图库服务端维护，全局共享。同一张图片在冷却期内不会被重复返回。若候选集内所有图片均在冷却中，服务端会自动忽略冷却限制随机返回。
