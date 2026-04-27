"""
AstrBot 插件：Pixiv 本地图库
从本地 Pixiv 图库 API 获取图片，支持随机、标签、画师、作品ID等多种查询方式，
以及定时推送订阅功能。
"""

from __future__ import annotations

import time

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

from .api_client import (
    FetchResult,
    PixivApiClient,
    PixivApiError,
    PixivNoMatchError,
    PixivParamError,
    PixivUploadError,
    UploadResult,
)
from .scheduler import Scheduler, Subscription, SubscriptionManager, _short_id, parse_interval


_SAVE_IMAGE_TIMEOUT_SECONDS = 60


class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._client = PixivApiClient(
            base_url=config.get("api_base_url", "http://localhost:8282"),
            use_thumbnail=config.get("use_thumbnail", False),
        )
        self._default_count: int = int(config.get("default_count", 1))
        self._default_cooldown: str = config.get("default_cooldown", "1d")
        self._show_info: bool = bool(config.get("show_info", True))
        self._use_forward: bool = bool(config.get("use_forward", True))
        self._exclude_r18: bool = bool(config.get("exclude_r18", True))

        self._sub_manager = SubscriptionManager(
            config=config,
            kv_get=self.get_kv_data,
            kv_put=self.put_kv_data,
        )
        self._scheduler = Scheduler(
            manager=self._sub_manager,
            push_cb=self._scheduled_push,
        )
        self._save_image_sessions: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    @filter.on_astrbot_loaded()
    async def on_loaded(self) -> None:
        self._scheduler.start()
        logger.info("[pixiv] 定时推送调度器已启动")

    async def terminate(self) -> None:
        self._scheduler.stop()
        logger.info("[pixiv] 定时推送调度器已停止")

    # ------------------------------------------------------------------ #
    # 普通消息：存图状态
    # ------------------------------------------------------------------ #

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        origin = event.unified_msg_origin
        active, expired = self._get_save_image_session(origin)

        if text == "存图":
            self._set_save_image_session(origin)
            yield event.plain_result("已进入存图状态，请在 1 分钟内发送图片；发送“结束”可退出。")
            event.stop_event()
            return

        if expired:
            if text == "结束":
                yield event.plain_result("存图状态已超时退出，如需继续请重新发送“存图”。")
                event.stop_event()
                return
            if self._has_image_segments(event):
                yield event.plain_result("存图状态已超时退出，请先发送“存图”再上传图片。")
                event.stop_event()
                return

        if not active:
            return

        if text == "结束":
            self._clear_save_image_session(origin)
            yield event.plain_result("已退出存图状态。")
            event.stop_event()
            return

        image_segments = self._collect_image_segments(event)
        if not image_segments:
            yield event.plain_result("当前处于存图状态，请发送图片；发送“结束”可退出。")
            event.stop_event()
            return

        self._set_save_image_session(origin)
        handled = await self._handle_save_images(event, image_segments)
        if handled:
            event.stop_event()

    # ------------------------------------------------------------------ #
    # 指令组：pixivr
    # ------------------------------------------------------------------ #

    @filter.command_group("pixivr")
    def pixivr(self):
        """Pixiv 本地图库指令组"""

    # ------------------------------------------------------------------ #
    # /pixivr random [count]
    # ------------------------------------------------------------------ #

    @pixivr.command("random")
    async def cmd_random(self, event: AstrMessageEvent, count: int = 0, merge: str = ""):
        """随机获取图片。用法：/pixivr random [数量] [-m]"""
        n = count if count > 0 else self._default_count
        use_merge = True if merge in ("-m", "--merge") else (False if merge else None)
        await self._send_fetch(event, count=n, use_merge=use_merge)

    # ------------------------------------------------------------------ #
    # /pixivr tag <tag1> [tag2 ...] [count]
    # ------------------------------------------------------------------ #

    @pixivr.command("tag")
    async def cmd_tag(self, event: AstrMessageEvent):
        """按标签搜索图片。用法：/pixivr tag <标签1> [标签2 ...] [数量] [-m]"""
        args = event.message_str.strip().split()
        args = args[2:] if len(args) > 2 else []

        if not args:
            yield event.plain_result("请提供至少一个标签。用法：/pixivr tag <标签1> [标签2 ...] [数量] [-m]")
            return

        # 提取 -m 标志
        use_merge: bool | None = None
        if "-m" in args or "--merge" in args:
            use_merge = True
            args = [a for a in args if a not in ("-m", "--merge")]

        count = self._default_count
        if args and args[-1].isdigit():
            count = int(args[-1])
            args = args[:-1]

        if not args:
            yield event.plain_result("请提供至少一个标签。")
            return

        await self._send_fetch(event, tags=args, count=count, use_merge=use_merge)

    # ------------------------------------------------------------------ #
    # /pixivr author <author_id> [count]
    # ------------------------------------------------------------------ #

    @pixivr.command("author")
    async def cmd_author(self, event: AstrMessageEvent, author_id: int, count: int = 0, merge: str = ""):
        """按画师 ID 搜索图片。用法：/pixivr author <画师ID> [数量] [-m]"""
        n = count if count > 0 else self._default_count
        use_merge = True if merge in ("-m", "--merge") else (False if merge else None)
        await self._send_fetch(event, author_id=author_id, count=n, use_merge=use_merge)

    # ------------------------------------------------------------------ #
    # /pixivr pid <pid> [page]
    # ------------------------------------------------------------------ #

    @pixivr.command("pid")
    async def cmd_pid(self, event: AstrMessageEvent, pid: int, page: int = 0):
        """按作品 ID 获取图片。用法：/pixivr pid <作品ID> [页码(0起)]"""
        await self._send_fetch(event, pid=pid, page=page, count=1)

    # ------------------------------------------------------------------ #
    # /pixivr search
    # ------------------------------------------------------------------ #

    @pixivr.command("search")
    async def cmd_search(self, event: AstrMessageEvent):
        """高级搜索。用法：/pixivr search [-t 标签] [-e 排除标签] [-a 画师ID] [-n 数量] [-p 页码] [-c 冷却] [-m]"""
        args = event.message_str.strip().split()[2:]

        tags: list[str] = []
        exclude_tags: list[str] = []
        author_id: int | None = None
        count = self._default_count
        page: int | None = None
        cooldown: str | None = self._default_cooldown or None
        use_merge: bool | None = None

        i = 0
        while i < len(args):
            flag = args[i]
            if flag in ("-m", "--merge"):
                use_merge = True; i += 1
            elif flag in ("-t", "--tag") and i + 1 < len(args):
                tags.append(args[i + 1]); i += 2
            elif flag in ("-e", "--exclude") and i + 1 < len(args):
                exclude_tags.append(args[i + 1]); i += 2
            elif flag in ("-a", "--author") and i + 1 < len(args):
                try:
                    author_id = int(args[i + 1])
                except ValueError:
                    yield event.plain_result(f"画师 ID 必须为整数，收到: {args[i + 1]}")
                    return
                i += 2
            elif flag in ("-n", "--count") and i + 1 < len(args):
                try:
                    count = int(args[i + 1])
                except ValueError:
                    yield event.plain_result(f"数量必须为整数，收到: {args[i + 1]}")
                    return
                i += 2
            elif flag in ("-p", "--page") and i + 1 < len(args):
                try:
                    page = int(args[i + 1])
                except ValueError:
                    yield event.plain_result(f"页码必须为整数，收到: {args[i + 1]}")
                    return
                i += 2
            elif flag in ("-c", "--cooldown") and i + 1 < len(args):
                cooldown = args[i + 1]; i += 2
            else:
                i += 1

        await self._send_fetch(
            event,
            tags=tags or None,
            exclude_tags=exclude_tags or None,
            author_id=author_id,
            page=page,
            count=count,
            cooldown=cooldown,
            use_merge=use_merge,
        )

    # ------------------------------------------------------------------ #
    # 指令组：pixivr sub
    # ------------------------------------------------------------------ #

    @pixivr.group("sub")
    def sub(self):
        """定时推送订阅管理"""

    # /pixivr sub origin
    @sub.command("origin")
    async def cmd_sub_origin(self, event: AstrMessageEvent):
        """查看当前会话的 unified_msg_origin，用于在 WebUI 中填写推送目标。"""
        umo = event.unified_msg_origin
        yield event.plain_result(f"当前会话标识：\n{umo}\n\n可将此值填入 WebUI 插件配置的订阅列表中。")

    # /pixivr sub list
    @sub.command("list")
    async def cmd_sub_list(self, event: AstrMessageEvent):
        """列出当前会话的所有订阅。"""
        subs = self._sub_manager.list_by_origin(event.unified_msg_origin)
        if not subs:
            yield event.plain_result("当前会话暂无订阅。使用 /pixivr sub add <间隔> 创建订阅。")
            return

        lines = ["当前会话订阅列表："]
        for sub in subs:
            next_trigger = await self._sub_manager.get_next_trigger(sub.sub_id)
            lines.append(sub.describe(next_trigger))
        yield event.plain_result("\n".join(lines))

    # /pixivr sub add <interval> [-t tag] [-e exclude] [-a author_id] [-n count] [-c cooldown]
    @filter.permission_type(filter.PermissionType.ADMIN)
    @sub.command("add")
    async def cmd_sub_add(self, event: AstrMessageEvent):
        """[管理员] 创建定时推送订阅。用法：/pixivr sub add <间隔> [-t 标签] [-e 排除标签] [-a 画师ID] [-n 数量] [-c 冷却]"""
        args = event.message_str.strip().split()
        # 去掉 "pixivr sub add"
        args = args[3:] if len(args) > 3 else []

        if not args:
            yield event.plain_result(
                "用法：/pixivr sub add <间隔> [-t 标签] [-e 排除标签] [-a 画师ID] [-n 数量] [-c 冷却]\n"
                "示例：/pixivr sub add 6h -t girl -t solo -n 2 -c 1d"
            )
            return

        # 第一个参数为间隔
        interval_str = args[0]
        try:
            interval_seconds = parse_interval(interval_str)
        except ValueError as e:
            yield event.plain_result(str(e))
            return

        args = args[1:]
        tags: list[str] = []
        exclude_tags: list[str] = []
        author_id: int | None = None
        count = self._default_count
        cooldown = self._default_cooldown or "1d"

        i = 0
        while i < len(args):
            flag = args[i]
            if flag in ("-t", "--tag") and i + 1 < len(args):
                tags.append(args[i + 1]); i += 2
            elif flag in ("-e", "--exclude") and i + 1 < len(args):
                exclude_tags.append(args[i + 1]); i += 2
            elif flag in ("-a", "--author") and i + 1 < len(args):
                try:
                    author_id = int(args[i + 1])
                except ValueError:
                    yield event.plain_result(f"画师 ID 必须为整数，收到: {args[i + 1]}")
                    return
                i += 2
            elif flag in ("-n", "--count") and i + 1 < len(args):
                try:
                    count = max(1, min(int(args[i + 1]), 10))
                except ValueError:
                    yield event.plain_result(f"数量必须为整数，收到: {args[i + 1]}")
                    return
                i += 2
            elif flag in ("-c", "--cooldown") and i + 1 < len(args):
                cooldown = args[i + 1]; i += 2
            else:
                i += 1

        sub = Subscription(
            sub_id=_short_id(),
            unified_msg_origin=event.unified_msg_origin,
            interval=interval_str,
            interval_seconds=interval_seconds,
            tags=tags,
            exclude_tags=exclude_tags,
            author_id=author_id,
            count=count,
            cooldown=cooldown,
            enabled=True,
        )
        self._sub_manager.add(sub)
        await self._sub_manager.init_next_trigger(sub)

        yield event.plain_result(f"订阅创建成功！\n{sub.describe()}")

    # /pixivr sub del <sub_id>
    @filter.permission_type(filter.PermissionType.ADMIN)
    @sub.command("del")
    async def cmd_sub_del(self, event: AstrMessageEvent, sub_id: str):
        """[管理员] 删除指定订阅。用法：/pixivr sub del <订阅ID>"""
        if self._sub_manager.remove(sub_id):
            yield event.plain_result(f"订阅 [{sub_id}] 已删除。")
        else:
            yield event.plain_result(f"未找到订阅 [{sub_id}]，请通过 /pixivr sub list 查看当前订阅。")

    # /pixivr sub clear
    @filter.permission_type(filter.PermissionType.ADMIN)
    @sub.command("clear")
    async def cmd_sub_clear(self, event: AstrMessageEvent):
        """[管理员] 清空当前会话的所有订阅。"""
        removed = self._sub_manager.clear_by_origin(event.unified_msg_origin)
        if removed:
            yield event.plain_result(f"已清空当前会话的 {removed} 个订阅。")
        else:
            yield event.plain_result("当前会话暂无订阅。")

    # ------------------------------------------------------------------ #
    # 内部：构建消息链
    # ------------------------------------------------------------------ #

    def _build_chain(self, result: FetchResult) -> list:
        """将 FetchResult 转换为消息链组件列表（普通发送）。"""
        chain: list = []
        for item in result.items:
            chain.append(Comp.Image.fromURL(item.image_url))

        if self._show_info:
            if len(result.items) == 1:
                item = result.items[0]
                info = f"Pixiv ID: {item.illust_id}"
                if item.author_name:
                    info += f" | 画师: {item.author_name}"
                info += f" | 匹配: {result.total_matched} 张"
            else:
                info = f"共 {len(result.items)} 张 | 匹配: {result.total_matched} 张"
            chain.append(Comp.Plain(info))

        return chain

    def _build_forward_nodes(self, result: FetchResult) -> list:
        """将 FetchResult 转换为合并转发节点列表，每张图独立一个 Node。"""
        nodes: list = []
        for item in result.items:
            content: list = [Comp.Image.fromURL(item.image_url)]
            if self._show_info:
                info = f"Pixiv ID: {item.illust_id}"
                if item.author_name:
                    info += f" | 画师: {item.author_name}"
                content.append(Comp.Plain(info))
            nodes.append(Comp.Node(uin=0, name="Pixiv", content=content))

        # 最后追加一个汇总节点
        if self._show_info and len(result.items) > 1:
            summary = f"共 {len(result.items)} 张 | 匹配: {result.total_matched} 张"
            nodes.append(Comp.Node(uin=0, name="Pixiv", content=[Comp.Plain(summary)]))

        return nodes

    def _set_save_image_session(self, unified_msg_origin: str) -> None:
        self._save_image_sessions[unified_msg_origin] = time.time() + _SAVE_IMAGE_TIMEOUT_SECONDS

    def _clear_save_image_session(self, unified_msg_origin: str) -> None:
        self._save_image_sessions.pop(unified_msg_origin, None)

    def _get_save_image_session(self, unified_msg_origin: str) -> tuple[bool, bool]:
        expires_at = self._save_image_sessions.get(unified_msg_origin)
        if expires_at is None:
            return False, False
        if expires_at < time.time():
            self._clear_save_image_session(unified_msg_origin)
            return False, True
        return True, False

    def _collect_image_segments(self, event: AstrMessageEvent) -> list:
        message = getattr(getattr(event, "message_obj", None), "message", None) or []
        image_segments = []
        for segment in message:
            if isinstance(segment, Comp.Image) or type(segment).__name__.lower() == "image":
                image_segments.append(segment)
        return image_segments

    def _has_image_segments(self, event: AstrMessageEvent) -> bool:
        return bool(self._collect_image_segments(event))

    @staticmethod
    def _looks_like_url(value: str) -> bool:
        return value.startswith("http://") or value.startswith("https://")

    @staticmethod
    def _looks_like_file_uri(value: str) -> bool:
        return value.startswith("file://")

    @staticmethod
    def _looks_like_base64(value: str) -> bool:
        return value.startswith("base64://")

    @staticmethod
    def _guess_segment_filename(attrs: dict, preferred_value: str | None = None) -> str | None:
        filename = attrs.get("filename")
        if isinstance(filename, str) and filename.strip():
            return filename.strip()

        if isinstance(preferred_value, str) and preferred_value.strip():
            value = preferred_value.strip()
            if value.startswith(("http://", "https://", "file://")):
                path_name = value.rsplit("/", 1)[-1].split("?", 1)[0]
                return path_name or None

        path = attrs.get("path")
        if isinstance(path, str) and path.strip():
            return path.strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1] or None

        return None

    async def _resolve_image_source(self, source_value: str, filename: str | None):
        if self._looks_like_url(source_value):
            return await self._client.build_upload_source_from_url(source_value, filename=filename)
        if self._looks_like_file_uri(source_value):
            return await self._client.build_upload_source_from_file_uri(source_value, filename=filename)
        if self._looks_like_base64(source_value):
            return await self._client.build_upload_source_from_base64(source_value, filename=filename)
        raise PixivUploadError(f"暂不支持的图片来源协议: {source_value[:32]}")

    async def _build_upload_source_from_segment(self, event: AstrMessageEvent, segment, index: int):
        attrs = getattr(segment, "__dict__", {}) or {}
        file_raw = attrs.get("file")
        url_raw = attrs.get("url")
        path_raw = attrs.get("path")
        file_value = file_raw.strip() if isinstance(file_raw, str) else ""
        url_value = url_raw.strip() if isinstance(url_raw, str) else ""
        path_value = path_raw.strip() if isinstance(path_raw, str) else ""

        direct_candidates: list[tuple[str, str]] = []
        if file_value:
            direct_candidates.append(("file", file_value))
        if url_value:
            direct_candidates.append(("url", url_value))
        if path_value:
            direct_candidates.append(("path", path_value))

        for key, value in direct_candidates:
            filename = self._guess_segment_filename(attrs, value)
            if key == "path":
                try:
                    return await self._client.build_upload_source_from_file(value, filename=filename)
                except PixivUploadError:
                    continue
            try:
                return await self._resolve_image_source(value, filename=filename)
            except PixivUploadError:
                if key != "file":
                    continue
                raise

        raw_message = getattr(getattr(event, "message_obj", None), "raw_message", None)

        for key, value in self._extract_raw_message_candidates(raw_message):
            filename = self._guess_segment_filename(attrs, value)
            try:
                if key.endswith("path") and not value.startswith("file://"):
                    return await self._client.build_upload_source_from_file(value, filename=filename)
                return await self._resolve_image_source(value, filename=filename)
            except PixivUploadError:
                continue

        raise PixivUploadError(
            f"第 {index} 张图片缺少可用来源，请查看 AstrBot 日志确认 file/url/path 字段。"
        )

    def _extract_raw_message_candidates(self, raw_message) -> list[tuple[str, str]]:
        candidates: list[tuple[str, str]] = []

        def walk(value, prefix: str = "") -> None:
            if isinstance(value, dict):
                for key, sub_value in value.items():
                    next_prefix = f"{prefix}.{key}" if prefix else str(key)
                    if isinstance(sub_value, str) and sub_value.strip():
                        if key in {"url", "file", "path", "src"}:
                            candidates.append((next_prefix, sub_value.strip()))
                    elif isinstance(sub_value, (dict, list, tuple)):
                        walk(sub_value, next_prefix)
            elif isinstance(value, (list, tuple)):
                for idx, item in enumerate(value):
                    walk(item, f"{prefix}[{idx}]")

        walk(raw_message)
        return candidates

    def _format_upload_result(self, total_count: int, result: UploadResult) -> str:
        lines = [
            f"本次收到 {total_count} 张图片，成功保存 {result.saved_count} 张。",
            f"目标目录：{result.target_dir}",
        ]
        if result.items:
            preview = []
            for item in result.items[:3]:
                preview.append(item.filepath or item.filename)
            lines.append(f"示例文件：{'，'.join(preview)}")
        return "\n".join(lines)

    async def _handle_save_images(self, event: AstrMessageEvent, image_segments: list) -> bool:
        upload_sources = []

        for index, segment in enumerate(image_segments, start=1):
            try:
                upload_sources.append(await self._build_upload_source_from_segment(event, segment, index))
            except PixivUploadError as e:
                logger.warning(
                    f"[pixiv save] 图片来源解析失败: segment={type(segment).__name__}, attrs={getattr(segment, '__dict__', None)}"
                )
                await event.send(event.plain_result(f"存图失败：{e}"))
                return True

        try:
            result = await self._client.upload_images(upload_sources)
        except PixivParamError as e:
            await event.send(event.plain_result(f"存图失败：{e}"))
            return True
        except PixivUploadError as e:
            logger.error(f"[pixiv save] 上传失败: {e}")
            await event.send(event.plain_result(f"存图失败：{e}"))
            return True
        except PixivApiError as e:
            logger.error(f"[pixiv save] API 请求失败: {e}")
            await event.send(event.plain_result(f"存图失败：{e}"))
            return True

        await event.send(event.plain_result(self._format_upload_result(len(image_segments), result)))
        return True

    # ------------------------------------------------------------------ #
    # 内部：响应指令的图片发送
    # ------------------------------------------------------------------ #

    async def _send_fetch(
        self,
        event: AstrMessageEvent,
        pid: int | None = None,
        author_id: int | None = None,
        tags: list[str] | None = None,
        exclude_tags: list[str] | None = None,
        page: int | None = None,
        count: int = 1,
        cooldown: str | None = None,
        use_merge: bool | None = None,
    ) -> None:
        if cooldown is None:
            cooldown = self._default_cooldown or None

        # 全局 R-18 过滤：利用 LIKE 模糊匹配，R-18 可同时匹配 R-18 和 R-18G
        if self._exclude_r18:
            if "R-18" not in (exclude_tags or []):
                exclude_tags = list(exclude_tags or []) + ["R-18"]

        try:
            result = await self._client.fetch(
                pid=pid,
                author_id=author_id,
                tags=tags,
                exclude_tags=exclude_tags,
                page=page,
                count=count,
                cooldown=cooldown,
            )
        except PixivNoMatchError as e:
            await event.send(event.plain_result(str(e)))
            return
        except PixivParamError as e:
            await event.send(event.plain_result(f"参数错误：{e}"))
            return
        except PixivApiError as e:
            logger.error(f"[pixiv] API 请求失败: {e}")
            await event.send(event.plain_result(str(e)))
            return

        if not result.items:
            await event.send(event.plain_result("没有找到匹配的图片"))
            return

        # use_merge=None 时跟随配置；单张图片不走合并转发
        should_forward = (use_merge if use_merge is not None else self._use_forward)
        if should_forward and len(result.items) > 1:
            nodes = self._build_forward_nodes(result)
            await event.send(event.chain_result([Comp.Nodes(nodes=nodes)]))
        else:
            await event.send(event.chain_result(self._build_chain(result)))

    # ------------------------------------------------------------------ #
    # 内部：定时推送回调（由 Scheduler 调用）
    # ------------------------------------------------------------------ #

    async def _scheduled_push(self, sub: Subscription) -> None:
        cooldown = sub.cooldown or None

        exclude_tags = list(sub.exclude_tags) if sub.exclude_tags else []
        if self._exclude_r18:
            if "R-18" not in exclude_tags:
                exclude_tags.append("R-18")
        try:
            result = await self._client.fetch(
                author_id=sub.author_id,
                tags=sub.tags or None,
                exclude_tags=exclude_tags or None,
                count=sub.count,
                cooldown=cooldown,
            )
        except PixivNoMatchError:
            logger.warning(f"[pixiv scheduler] 订阅 {sub.sub_id} 无匹配图片，跳过本次推送")
            return
        except PixivApiError as e:
            logger.error(f"[pixiv scheduler] 订阅 {sub.sub_id} API 请求失败: {e}")
            return

        if not result.items:
            return

        # 多图且开启合并转发时，将所有 Node 作为一条合并消息发出；否则逐张拼入 MessageChain
        if self._use_forward and len(result.items) > 1:
            nodes = self._build_forward_nodes(result)
            chain = MessageChain()
            chain.chain = [Comp.Nodes(nodes=nodes)]
        else:
            chain = MessageChain()
            for item in result.items:
                chain.file_image(item.image_url)
            if self._show_info:
                if len(result.items) == 1:
                    item = result.items[0]
                    info = f"Pixiv ID: {item.illust_id}"
                    if item.author_name:
                        info += f" | 画师: {item.author_name}"
                    info += f" | 匹配: {result.total_matched} 张"
                else:
                    info = f"共 {len(result.items)} 张 | 匹配: {result.total_matched} 张"
                chain.message(info)

        await self.context.send_message(sub.unified_msg_origin, chain)
        logger.info(f"[pixiv scheduler] 订阅 {sub.sub_id} 推送完成，发送 {len(result.items)} 张图片")
