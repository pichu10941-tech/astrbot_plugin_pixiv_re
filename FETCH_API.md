# GET /api/fetch — 图片获取接口参考

按条件从本地图库随机获取图片信息，供外部调用。

---

## 请求

```
GET /api/fetch
```

### 参数

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `pid` | integer | 否 | — | 指定 illust_id，精确匹配 |
| `author_id` | integer | 否 | — | 指定画师 ID，精确匹配 |
| `tags` | string（可重复） | 否 | — | 包含标签，多个参数取 AND 逻辑 |
| `exclude_tags` | string（可重复） | 否 | — | 排除标签，命中任意一个即排除 |
| `page` | integer ≥ 0 | 否 | 自动取第一张 | 指定返回页码（0-based） |
| `count` | integer 1–10 | 否 | 1 | 返回图片数量，上限 10 |
| `cooldown` | string | 否 | — | 冷却时长，格式见下方说明 |

#### tags / exclude_tags 匹配规则

- 同时搜索 **Pixiv 官方标签**和 **AI 标签**（wd-tagger 分析结果）
- 使用 **LIKE 模糊匹配**，如 `tags=girl` 可匹配 `1girl`、`2girls` 等
- 多个 `tags` 参数之间为 **AND** 逻辑（图片必须同时命中所有标签）
- 多个 `exclude_tags` 参数之间为 **OR** 逻辑（命中任意一个即被排除）

#### cooldown 格式

| 示例 | 含义 |
|---|---|
| `30m` | 30 分钟 |
| `12h` | 12 小时 |
| `1d` | 1 天 |
| `7d` | 7 天 |

- 冷却记录**全局共享**，所有调用方共用同一冷却池
- 冷却记录持久化到 `user_data/data/fetch.db`，服务重启后仍有效
- 若候选集内**所有图片均在冷却中**，自动忽略冷却限制，从完整候选集随机返回

---

## 响应

### 成功 `200 OK`

```json
{
  "total_matched": 42,
  "items": [
    {
      "illust_id": 12345678,
      "author_id": 111,
      "author_name": "画师名",
      "page": 0,
      "url": "/api/images/file/pixiv/0/12345678_p0.jpg"
    }
  ]
}
```

| 字段 | 说明 |
|---|---|
| `total_matched` | 满足过滤条件的候选图片总数（不受 count 影响） |
| `items` | 本次返回的图片列表 |
| `items[].illust_id` | Pixiv 作品 ID |
| `items[].author_id` | 画师 ID |
| `items[].author_name` | 画师名（DB 中无记录时为空字符串） |
| `items[].page` | 页码（0-based） |
| `items[].url` | 图片文件 URL，可直接拼接 host 访问 |

- 多图作品默认返回第 0 页（第一张）
- 指定 `page` 时，若某作品不存在该页则跳过，实际返回数量可能少于 `count`

### 无匹配 `404 Not Found`

```json
{ "detail": "no_match" }
```

### 参数错误 `400 Bad Request`

```json
{ "detail": "无法解析冷却时长: '2x'，格式应为 30m / 12h / 1d" }
```

---

## 调用示例

### 随机取一张

```
GET /api/fetch
```

### 指定画师随机取一张

```
GET /api/fetch?author_id=12345
```

### 按标签过滤，取 3 张

```
GET /api/fetch?tags=girl&tags=blue_hair&count=3
```

### 包含标签 + 排除标签

```
GET /api/fetch?tags=girl&exclude_tags=explicit&exclude_tags=questionable
```

### 指定 pid，取第 2 页

```
GET /api/fetch?pid=12345678&page=1
```

### 带冷却，24 小时内不重复

```
GET /api/fetch?tags=girl&count=5&cooldown=1d
```

### 综合示例

```
GET /api/fetch?author_id=111&tags=girl&tags=solo&exclude_tags=nsfw&count=3&cooldown=12h
```

---

## 图片访问

`url` 字段为相对路径，拼接服务地址即可直接访问原图：

```
http://localhost:8282/api/images/file/pixiv/0/12345678_p0.jpg
```

如需缩略图，将路径中的 `images/file` 替换为 `images/thumb`：

```
http://localhost:8282/api/images/thumb/pixiv/0/12345678_p0.jpg
```
