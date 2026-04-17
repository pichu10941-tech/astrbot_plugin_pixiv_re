"""
AstrBot 插件：Pixiv 本地图库
从本地 Pixiv 图库 API 获取图片，支持随机、标签、画师、作品ID等多种查询方式，
以及定时推送订阅功能。
"""

from __future__ import annotations

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

from .api_client import FetchResult, PixivApiClient, PixivApiError, PixivNoMatchError, PixivParamError
from .scheduler import Scheduler, Subscription, SubscriptionManager, _short_id, parse_interval


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
            nodes.append(Comp.Node(content=content))

        # 最后追加一个汇总节点
        if self._show_info and len(result.items) > 1:
            summary = f"共 {len(result.items)} 张 | 匹配: {result.total_matched} 张"
            nodes.append(Comp.Node(content=[Comp.Plain(summary)]))

        return nodes

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
            await event.send(event.chain_result(self._build_forward_nodes(result)))
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

        # 多图且开启合并转发时，使用 Node 列表；否则逐张拼入 MessageChain
        if self._use_forward and len(result.items) > 1:
            nodes = self._build_forward_nodes(result)
            chain = MessageChain()
            for node in nodes:
                # Node 内容序列化为图片 + 文字
                for comp in node.content:
                    if isinstance(comp, Comp.Image):
                        chain.file_image(comp.url or "")
                    elif isinstance(comp, Comp.Plain):
                        chain.message(comp.text)
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
