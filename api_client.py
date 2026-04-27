"""
Pixiv 本地图库 API 客户端
封装 GET /api/fetch 与 POST /api/upload/image 接口。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlparse


class PixivApiError(Exception):
    """API 请求异常基类"""


class PixivNoMatchError(PixivApiError):
    """无匹配图片（404）"""


class PixivParamError(PixivApiError):
    """参数错误（400）"""


class PixivUploadError(PixivApiError):
    """上传失败"""


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


@dataclass
class UploadSource:
    filename: str
    data: bytes
    content_type: str


@dataclass
class UploadItem:
    filename: str
    filepath: str
    size: int
    status: str


@dataclass
class UploadResult:
    message: str
    saved_count: int
    target_dir: str
    items: list[UploadItem] = field(default_factory=list)


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

    @staticmethod
    def _guess_filename_from_url(url: str) -> str:
        parsed = urlparse(url)
        name = Path(parsed.path).name
        return name or "image"

    @staticmethod
    def _guess_content_type(filename: str, default: str = "application/octet-stream") -> str:
        guessed, _ = mimetypes.guess_type(filename)
        return guessed or default

    @staticmethod
    def _normalize_filesystem_path(file_uri: str) -> str:
        parsed = urlparse(file_uri)
        if parsed.scheme != "file":
            raise PixivUploadError(f"不是有效的 file URI: {file_uri}")

        path = unquote(parsed.path or "")
        if not path:
            raise PixivUploadError("file URI 中缺少文件路径")
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            path = f"//{parsed.netloc}{path}"
        return path

    async def build_upload_source_from_file_uri(self, file_uri: str, filename: str | None = None) -> UploadSource:
        file_path = self._normalize_filesystem_path(file_uri)
        return await self.build_upload_source_from_file(file_path, filename=filename)

    async def build_upload_source_from_base64(self, base64_value: str, filename: str | None = None) -> UploadSource:
        payload = base64_value[len("base64://") :] if base64_value.startswith("base64://") else base64_value
        if not payload:
            raise PixivUploadError("base64 图片数据为空")

        try:
            data = base64.b64decode(payload, validate=True)
        except Exception as e:
            raise PixivUploadError(f"base64 图片数据解码失败: {e}") from e

        if not data:
            raise PixivUploadError("base64 图片数据为空")

        final_name = filename or "image"
        return UploadSource(
            filename=final_name,
            data=data,
            content_type=self._guess_content_type(final_name),
        )

    async def build_upload_source_from_url(self, url: str, filename: str | None = None) -> UploadSource:
        import aiohttp

        final_name = filename or self._guess_filename_from_url(url)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(connect=5, total=30)) as resp:
                    if resp.status != 200:
                        raise PixivUploadError(f"下载图片失败，状态码: {resp.status}")
                    data = await resp.read()
                    if not data:
                        raise PixivUploadError("下载到的图片为空")
                    content_type = resp.headers.get("Content-Type") or self._guess_content_type(final_name)
        except (aiohttp.ClientConnectionError, aiohttp.ClientConnectorError) as e:
            raise PixivUploadError(f"下载图片失败，无法连接到图片地址: {e}") from e
        except aiohttp.ClientError as e:
            raise PixivUploadError(f"下载图片失败: {e}") from e

        return UploadSource(filename=final_name, data=data, content_type=content_type)

    async def build_upload_source_from_file(self, file_path: str, filename: str | None = None) -> UploadSource:
        path = Path(file_path)
        if not path.is_file():
            raise PixivUploadError(f"图片文件不存在: {file_path}")

        data = path.read_bytes()
        if not data:
            raise PixivUploadError(f"图片文件为空: {file_path}")

        final_name = filename or path.name or "image"
        return UploadSource(
            filename=final_name,
            data=data,
            content_type=self._guess_content_type(final_name),
        )

    async def upload_images(self, sources: list[UploadSource]) -> UploadResult:
        import aiohttp

        if not sources:
            raise PixivParamError("至少上传一张图片")

        form = aiohttp.FormData()
        for source in sources:
            form.add_field(
                "files",
                source.data,
                filename=source.filename,
                content_type=source.content_type,
            )

        url = f"{self._base_url}/api/upload/image"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=form, timeout=aiohttp.ClientTimeout(connect=5, total=60)) as resp:
                    if resp.status == 400:
                        data = await resp.json()
                        raise PixivParamError(data.get("detail", "参数错误"))
                    if resp.status != 200:
                        raise PixivUploadError(f"上传接口返回异常状态码: {resp.status}")

                    data = await resp.json()
        except (aiohttp.ClientConnectionError, aiohttp.ClientConnectorError) as e:
            raise PixivUploadError(f"无法连接到图片服务，请检查 api_base_url 配置（注意 http/https 协议和端口）: {e}") from e
        except aiohttp.ClientError as e:
            raise PixivUploadError(f"上传请求失败: {e}") from e

        items = [
            UploadItem(
                filename=item.get("filename", ""),
                filepath=item.get("filepath", ""),
                size=int(item.get("size", 0)),
                status=item.get("status", ""),
            )
            for item in data.get("items", [])
            if isinstance(item, dict)
        ]
        return UploadResult(
            message=data.get("message", "上传成功"),
            saved_count=int(data.get("saved_count", 0)),
            target_dir=data.get("target_dir", "inbox"),
            items=items,
        )

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
