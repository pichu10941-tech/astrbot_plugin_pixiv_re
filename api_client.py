"""
Pixiv 本地图库 API 客户端
封装 GET /api/fetch 接口，处理请求、响应解析和错误。
"""

from __future__ import annotations

from dataclasses import dataclass, field


class PixivApiError(Exception):
    """API 请求异常基类"""


class PixivNoMatchError(PixivApiError):
    """无匹配图片（404）"""


class PixivParamError(PixivApiError):
    """参数错误（400）"""


@dataclass
class ImageItem:
    illust_id: int
    author_id: int
    author_name: str
    page: int
    image_url: str  # 已拼接完整 URL


@dataclass
class FetchResult:
    total_matched: int
    items: list[ImageItem] = field(default_factory=list)


class PixivApiClient:
    def __init__(self, base_url: str, use_thumbnail: bool = False) -> None:
        # 去除末尾斜杠，统一格式
        self._base_url = base_url.rstrip("/")
        self._use_thumbnail = use_thumbnail

    def build_image_url(self, relative_url: str) -> str:
        """将 API 返回的相对路径拼接为完整 URL，按配置决定原图/缩略图。"""
        path = relative_url.lstrip("/")
        if self._use_thumbnail:
            path = path.replace("images/file", "images/thumb", 1)
        return f"{self._base_url}/{path}"

    async def fetch(
        self,
        pid: int | None = None,
        author_id: int | None = None,
        tags: list[str] | None = None,
        exclude_tags: list[str] | None = None,
        page: int | None = None,
        count: int = 1,
        cooldown: str | None = None,
    ) -> FetchResult:
        """
        调用本地 /api/fetch 接口获取图片。

        Raises:
            PixivNoMatchError: 无匹配图片
            PixivParamError: 参数错误
            PixivApiError: 其他 API 错误或网络错误
        """
        import aiohttp

        params: list[tuple[str, str]] = []

        if pid is not None:
            params.append(("pid", str(pid)))
        if author_id is not None:
            params.append(("author_id", str(author_id)))
        if tags:
            for tag in tags:
                params.append(("tags", tag))
        if exclude_tags:
            for tag in exclude_tags:
                params.append(("exclude_tags", tag))
        if page is not None:
            params.append(("page", str(page)))
        params.append(("count", str(max(1, min(count, 10)))))
        if cooldown:
            params.append(("cooldown", cooldown))

        url = f"{self._base_url}/api/fetch"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(connect=3, total=15)) as resp:
                    if resp.status == 404:
                        raise PixivNoMatchError("没有找到匹配的图片")
                    if resp.status == 400:
                        data = await resp.json()
                        raise PixivParamError(data.get("detail", "参数错误"))
                    if resp.status != 200:
                        raise PixivApiError(f"API 返回异常状态码: {resp.status}")

                    data = await resp.json()
        except (aiohttp.ClientConnectionError, aiohttp.ClientConnectorError) as e:
            raise PixivApiError(f"无法连接到图片服务，请检查 api_base_url 配置（注意 http/https 协议和端口）: {e}") from e
        except aiohttp.ClientError as e:
            raise PixivApiError(f"网络请求失败: {e}") from e

        items = [
            ImageItem(
                illust_id=item["illust_id"],
                author_id=item["author_id"],
                author_name=item.get("author_name", ""),
                page=item["page"],
                image_url=self.build_image_url(item["url"]),
            )
            for item in data.get("items", [])
        ]

        return FetchResult(total_matched=data.get("total_matched", 0), items=items)
