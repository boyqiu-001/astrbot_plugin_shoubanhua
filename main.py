import asyncio
import base64
import functools
import io
import json
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

import aiohttp
from PIL import Image as PILImage

from astrbot import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, Image, Reply, Plain
from astrbot.core.platform.astr_message_event import AstrMessageEvent


@register(
    "astrbot_plugin_shoubanhua",
    "shskjw",
    "通过第三方api进行手办化等功能",
    "1.2.2",
    "https://github.com/shkjw/astrbot_plugin_shoubanhua",
)
class FigurineProPlugin(Star):
    class ImageWorkflow:
        def __init__(self, proxy_url: str | None = None):
            if proxy_url: logger.info(f"ImageWorkflow 使用代理: {proxy_url}")
            self.session = aiohttp.ClientSession()
            self.proxy = proxy_url

        async def _download_image(self, url: str) -> bytes | None:
            try:
                async with self.session.get(url, proxy=self.proxy, timeout=30) as resp:
                    resp.raise_for_status()
                    return await resp.read()
            except Exception as e:
                logger.error(f"图片下载失败: {e}");
                return None

        async def _get_avatar(self, user_id: str) -> bytes | None:
            if not user_id.isdigit(): logger.warning(f"无法获取非 QQ 平台或无效 QQ 号 {user_id} 的头像。"); return None
            avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
            return await self._download_image(avatar_url)

        def _extract_first_frame_sync(self, raw: bytes) -> bytes:
            img_io = io.BytesIO(raw)
            try:
                with PILImage.open(img_io) as img:
                    if getattr(img, "is_animated", False):
                        logger.info("检测到动图, 将抽取第一帧进行生成")
                        img.seek(0)
                        first_frame = img.convert("RGBA")
                        out_io = io.BytesIO()
                        first_frame.save(out_io, format="PNG")
                        return out_io.getvalue()
            except Exception as e:
                logger.warning(f"抽取图片帧时发生错误, 将返回原始数据: {e}", exc_info=True)
            return raw

        async def _load_bytes(self, src: str) -> bytes | None:
            raw: bytes | None = None
            loop = asyncio.get_running_loop()
            if Path(src).is_file():
                raw = await loop.run_in_executor(None, Path(src).read_bytes)
            elif src.startswith("http"):
                raw = await self._download_image(src)
            elif src.startswith("base64://"):
                raw = await loop.run_in_executor(None, base64.b64decode, src[9:])
            if not raw: return None
            return await loop.run_in_executor(None, self._extract_first_frame_sync, raw)

        # 【修改】将 get_first_image 修改为 get_images 以支持多图
        async def get_images(self, event: AstrMessageEvent) -> List[bytes]:
            """
            从事件中提取所有图片。
            提取顺序:
            1. 回复中的所有图片
            2. 消息体中的所有图片
            3. 如果没有上述图片，则提取消息中所有@用户的头像
            4. 如果连@用户都没有，则提取发送者自己的头像
            """
            img_bytes_list: List[bytes] = []
            at_user_ids: List[str] = []

            # 1. 搜集回复链中的图片
            for seg in event.message_obj.message:
                if isinstance(seg, Reply) and seg.chain:
                    for s_chain in seg.chain:
                        if isinstance(s_chain, Image):
                            if s_chain.url and (img := await self._load_bytes(s_chain.url)):
                                img_bytes_list.append(img)
                            elif s_chain.file and (img := await self._load_bytes(s_chain.file)):
                                img_bytes_list.append(img)

            # 2. 搜集当前消息中的图片和@用户
            for seg in event.message_obj.message:
                if isinstance(seg, Image):
                    if seg.url and (img := await self._load_bytes(seg.url)):
                        img_bytes_list.append(img)
                    elif seg.file and (img := await self._load_bytes(seg.file)):
                        img_bytes_list.append(img)
                elif isinstance(seg, At):
                    at_user_ids.append(str(seg.qq))

            # 如果已经有图片了，就直接返回
            if img_bytes_list:
                return img_bytes_list

            # 3. 如果没有图片，则搜集@用户的头像
            if at_user_ids:
                for user_id in at_user_ids:
                    if avatar := await self._get_avatar(user_id):
                        img_bytes_list.append(avatar)
                return img_bytes_list

            # 4. 如果连@都没有，则获取发送者头像
            if avatar := await self._get_avatar(event.get_sender_id()):
                img_bytes_list.append(avatar)

            return img_bytes_list

        async def terminate(self):
            if self.session and not self.session.closed: await self.session.close()

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.plugin_data_dir = StarTools.get_data_dir()
        # 用户次数数据
        self.user_counts_file = self.plugin_data_dir / "user_counts.json"
        self.user_counts: Dict[str, int] = {}
        # 群组次数数据
        self.group_counts_file = self.plugin_data_dir / "group_counts.json"
        self.group_counts: Dict[str, int] = {}
        # 用户签到数据
        self.user_checkin_file = self.plugin_data_dir / "user_checkin.json"
        self.user_checkin_data: Dict[str, str] = {}

        self.key_index = 0
        self.key_lock = asyncio.Lock()
        self.iwf: Optional[FigurineProPlugin.ImageWorkflow] = None
        self.default_prompts: Dict[str, str] = {}

    async def initialize(self):
        prompts_file = Path(__file__).parent / "prompts.json"
        if prompts_file.exists():
            try:
                content = prompts_file.read_text("utf-8")
                self.default_prompts = json.loads(content)
                logger.info("默认 prompts.json 文件已加载")
            except Exception as e:
                logger.error(f"加载默认 prompts.json 文件失败: {e}", exc_info=True)
        use_proxy = self.conf.get("use_proxy", False)
        proxy_url = self.conf.get("proxy_url") if use_proxy else None
        self.iwf = self.ImageWorkflow(proxy_url)
        # 加载所有数据文件
        await self._load_user_counts()
        await self._load_group_counts()
        await self._load_user_checkin_data()
        logger.info("FigurinePro 插件已加载")
        if not self.conf.get("api_keys"):
            logger.warning("FigurinePro: 未配置任何 API 密钥，插件可能无法工作")

    # --- 权限检查 ---
    def is_global_admin(self, event: AstrMessageEvent) -> bool:
        """检查用户是否为机器人的全局管理员"""
        admin_ids = self.context.get_config().get("admins_id", [])
        return event.get_sender_id() in admin_ids

    # --- 数据读写辅助函数 ---
    async def _load_user_counts(self):
        if not self.user_counts_file.exists(): self.user_counts = {}; return
        loop = asyncio.get_running_loop()
        try:
            content = await loop.run_in_executor(None, self.user_counts_file.read_text, "utf-8")
            data = await loop.run_in_executor(None, json.loads, content)
            if isinstance(data, dict): self.user_counts = {str(k): v for k, v in data.items()}
        except Exception as e:
            logger.error(f"加载用户次数文件时发生错误: {e}", exc_info=True);
            self.user_counts = {}

    async def _save_user_counts(self):
        loop = asyncio.get_running_loop()
        try:
            json_data = await loop.run_in_executor(None,
                                                   functools.partial(json.dumps, self.user_counts, ensure_ascii=False,
                                                                     indent=4))
            await loop.run_in_executor(None, self.user_counts_file.write_text, json_data, "utf-8")
        except Exception as e:
            logger.error(f"保存用户次数文件时发生错误: {e}", exc_info=True)

    def _get_user_count(self, user_id: str) -> int:
        return self.user_counts.get(str(user_id), 0)

    async def _decrease_user_count(self, user_id: str):
        user_id_str = str(user_id)
        count = self._get_user_count(user_id_str)
        if count > 0: self.user_counts[user_id_str] = count - 1; await self._save_user_counts()

    async def _load_group_counts(self):
        if not self.group_counts_file.exists(): self.group_counts = {}; return
        loop = asyncio.get_running_loop()
        try:
            content = await loop.run_in_executor(None, self.group_counts_file.read_text, "utf-8")
            data = await loop.run_in_executor(None, json.loads, content)
            if isinstance(data, dict): self.group_counts = {str(k): v for k, v in data.items()}
        except Exception as e:
            logger.error(f"加载群组次数文件时发生错误: {e}", exc_info=True);
            self.group_counts = {}

    async def _save_group_counts(self):
        loop = asyncio.get_running_loop()
        try:
            json_data = await loop.run_in_executor(None,
                                                   functools.partial(json.dumps, self.group_counts, ensure_ascii=False,
                                                                     indent=4))
            await loop.run_in_executor(None, self.group_counts_file.write_text, json_data, "utf-8")
        except Exception as e:
            logger.error(f"保存群组次数文件时发生错误: {e}", exc_info=True)

    def _get_group_count(self, group_id: str) -> int:
        return self.group_counts.get(str(group_id), 0)

    async def _decrease_group_count(self, group_id: str):
        group_id_str = str(group_id)
        count = self._get_group_count(group_id_str)
        if count > 0: self.group_counts[group_id_str] = count - 1; await self._save_group_counts()

    # --- 签到数据读写 ---
    async def _load_user_checkin_data(self):
        if not self.user_checkin_file.exists(): self.user_checkin_data = {}; return
        loop = asyncio.get_running_loop()
        try:
            content = await loop.run_in_executor(None, self.user_checkin_file.read_text, "utf-8")
            data = await loop.run_in_executor(None, json.loads, content)
            if isinstance(data, dict): self.user_checkin_data = {str(k): v for k, v in data.items()}
        except Exception as e:
            logger.error(f"加载用户签到文件时发生错误: {e}", exc_info=True);
            self.user_checkin_data = {}

    async def _save_user_checkin_data(self):
        loop = asyncio.get_running_loop()
        try:
            json_data = await loop.run_in_executor(None, functools.partial(json.dumps, self.user_checkin_data,
                                                                           ensure_ascii=False, indent=4))
            await loop.run_in_executor(None, self.user_checkin_file.write_text, json_data, "utf-8")
        except Exception as e:
            logger.error(f"保存用户签到文件时发生错误: {e}", exc_info=True)

    # --- 签到指令 ---
    @filter.command("手办化签到", prefix_optional=True)
    async def on_checkin(self, event: AstrMessageEvent):
        if not self.conf.get("enable_checkin", False):
            yield event.plain_result("📅 本机器人未开启签到功能。")
            return

        user_id = event.get_sender_id()
        today_str = datetime.now().strftime("%Y-%m-%d")

        last_checkin_date = self.user_checkin_data.get(user_id)
        if last_checkin_date == today_str:
            yield event.plain_result(
                f"您今天已经签到过了，明天再来吧！\n您当前剩余个人次数: {self._get_user_count(user_id)}")
            return

        # --- 【修复】采用更健壮的逻辑判断随机开关 ---
        reward = 0
        is_random_val = self.conf.get("enable_random_checkin", False)
        # 将获取到的值统一转为小写字符串'true'进行判断，兼容bool(True)和str("true")等情况
        if str(is_random_val).lower() == 'true':
            max_reward = self.conf.get("checkin_random_reward_max", 5)
            max_reward = max(1, int(max_reward))
            reward = random.randint(1, max_reward)
        else:
            reward = self.conf.get("checkin_fixed_reward", 3)
            reward = int(reward)

        current_count = self._get_user_count(user_id)
        new_count = current_count + reward
        self.user_counts[user_id] = new_count
        await self._save_user_counts()

        self.user_checkin_data[user_id] = today_str
        await self._save_user_checkin_data()

        yield event.plain_result(f"🎉 签到成功！\n您获得了 {reward} 次个人使用次数。\n当前剩余: {new_count} 次。")

    # --- 管理指令 (仅限全局管理员) ---
    @filter.command("手办化增加用户次数", prefix_optional=True)
    async def on_add_user_counts(self, event: AstrMessageEvent):
        if not self.is_global_admin(event): return
        cmd_text = event.message_str.strip()
        at_seg = next((s for s in event.message_obj.message if isinstance(s, At)), None)
        target_qq, count = None, 0
        if at_seg:
            target_qq = str(at_seg.qq)
            match = re.search(r"(\d+)\s*$", cmd_text)
            if match: count = int(match.group(1))
        else:
            match = re.search(r"(\d+)\s+(\d+)", cmd_text)
            if match: target_qq, count = match.group(1), int(match.group(2))
        if not target_qq or count <= 0:
            yield event.plain_result(
                '格式错误:\n#手办化增加用户次数 @用户 <次数>\n或 #手办化增加用户次数 <QQ号> <次数>')
            return
        current_count = self._get_user_count(target_qq)
        self.user_counts[str(target_qq)] = current_count + count
        await self._save_user_counts()
        yield event.plain_result(f"✅ 已为用户 {target_qq} 增加 {count} 次，TA当前剩余 {current_count + count} 次。")

    @filter.command("手办化增加群组次数", prefix_optional=True)
    async def on_add_group_counts(self, event: AstrMessageEvent):
        if not self.is_global_admin(event): return
        cmd_text = event.message_str.strip()
        match = re.search(r"(\d+)\s+(\d+)", cmd_text)
        if not match:
            yield event.plain_result('格式错误: #手办化增加群组次数 <群号> <次数>')
            return
        target_group, count = match.group(1), int(match.group(2))
        current_count = self._get_group_count(target_group)
        self.group_counts[str(target_group)] = current_count + count
        await self._save_group_counts()
        yield event.plain_result(f"✅ 已为群组 {target_group} 增加 {count} 次，该群当前剩余 {current_count + count} 次。")

    @filter.command("手办化查询次数", prefix_optional=True)
    async def on_query_counts(self, event: AstrMessageEvent):
        user_id_to_query = event.get_sender_id()
        if self.is_global_admin(event):
            at_seg = next((s for s in event.message_obj.message if isinstance(s, At)), None)
            if at_seg:
                user_id_to_query = str(at_seg.qq)
            else:
                match = re.search(r"(\d+)", event.message_str)
                if match: user_id_to_query = match.group(1)

        user_count = self._get_user_count(user_id_to_query)
        reply_msg = ""
        if user_id_to_query == event.get_sender_id():
            reply_msg = f"您好，您当前个人剩余次数为: {user_count}"
        else:
            reply_msg = f"用户 {user_id_to_query} 个人剩余次数为: {user_count}"

        group_id = event.get_group_id()
        if group_id:
            group_count = self._get_group_count(group_id)
            reply_msg += f"\n本群共享剩余次数为: {group_count}"
        yield event.plain_result(reply_msg)

    @filter.command("手办化添加key", prefix_optional=True)
    async def on_add_key(self, event: AstrMessageEvent):
        if not self.is_global_admin(event): return
        new_keys = event.message_str.strip().split()
        if not new_keys: yield event.plain_result("格式错误，请提供要添加的Key。"); return
        api_keys = self.conf.get("api_keys", [])
        added_keys = [key for key in new_keys if key not in api_keys]
        api_keys.extend(added_keys)
        await self.conf.set("api_keys", api_keys)
        yield event.plain_result(f"✅ 操作完成，新增 {len(added_keys)} 个Key，当前共 {len(api_keys)} 个。")

    @filter.command("手办化key列表", prefix_optional=True)
    async def on_list_keys(self, event: AstrMessageEvent):
        if not self.is_global_admin(event): return
        api_keys = self.conf.get("api_keys", [])
        if not api_keys: yield event.plain_result("📝 暂未配置任何 API Key。"); return
        key_list_str = "\n".join(f"{i + 1}. {key[:8]}...{key[-4:]}" for i, key in enumerate(api_keys))
        yield event.plain_result(f"🔑 API Key 列表:\n{key_list_str}")

    @filter.command("手办化删除key", prefix_optional=True)
    async def on_delete_key(self, event: AstrMessageEvent):
        if not self.is_global_admin(event): return
        param = event.message_str.strip()
        api_keys = self.conf.get("api_keys", [])
        if param.lower() == "all":
            count = len(api_keys)
            await self.conf.set("api_keys", [])
            yield event.plain_result(f"✅ 已删除全部 {count} 个 Key。")
        elif param.isdigit() and 1 <= int(param) <= len(api_keys):
            idx = int(param) - 1
            removed_key = api_keys.pop(idx)
            await self.conf.set("api_keys", api_keys)
            yield event.plain_result(f"✅ 已删除 Key: {removed_key[:8]}...")
        else:
            yield event.plain_result("格式错误，请使用 #手办化删除key <序号|all>")

    # --- 图片生成指令 (一个指令一个函数) ---
    @filter.command("手办化", prefix_optional=True)
    async def on_cmd_figurine(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "手办化"): yield result

    @filter.command("手办化2", prefix_optional=True)
    async def on_cmd_figurine2(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "手办化2"): yield result

    @filter.command("手办化3", prefix_optional=True)
    async def on_cmd_figurine3(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "手办化3"): yield result

    @filter.command("手办化4", prefix_optional=True)
    async def on_cmd_figurine4(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "手办化4"): yield result

    @filter.command("手办化5", prefix_optional=True)
    async def on_cmd_figurine5(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "手办化5"): yield result

    @filter.command("手办化6", prefix_optional=True)
    async def on_cmd_figurine6(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "手办化6"): yield result

    @filter.command("Q版化", prefix_optional=True)
    async def on_cmd_qversion(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "Q版化"): yield result

    @filter.command("痛屋化", prefix_optional=True)
    async def on_cmd_painroom(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "痛屋化"): yield result

    @filter.command("痛屋化2", prefix_optional=True)
    async def on_cmd_painroom2(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "痛屋化2"): yield result

    @filter.command("痛车化", prefix_optional=True)
    async def on_cmd_paincar(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "痛车化"): yield result

    @filter.command("cos化", prefix_optional=True)
    async def on_cmd_cos(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "cos化"): yield result

    @filter.command("cos自拍", prefix_optional=True)
    async def on_cmd_cos_selfie(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "cos自拍"): yield result

    @filter.command("bnn", prefix_optional=True)
    async def on_cmd_bnn(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "bnn"): yield result

    @filter.command("孤独的我", prefix_optional=True)
    async def on_cmd_clown(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "孤独的我"): yield result

    @filter.command("第三视角", prefix_optional=True)
    async def on_cmd_view3(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "第三视角"): yield result

    @filter.command("鬼图", prefix_optional=True)
    async def on_cmd_ghost(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "鬼图"): yield result

    @filter.command("第一视角", prefix_optional=True)
    async def on_cmd_view1(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "第一视角"): yield result

    @filter.command("贴纸化", prefix_optional=True)
    async def on_cmd_sticker(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "贴纸化"): yield result

    @filter.command("玉足", prefix_optional=True)
    async def on_cmd_foot_jade(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "玉足"): yield result

    @filter.command("fumo化", prefix_optional=True)
    async def on_cmd_fumo(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "fumo化"): yield result

    @filter.command("手办化帮助", prefix_optional=True)
    async def on_cmd_help(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "手办化帮助"): yield result

    @filter.command("cos相遇", prefix_optional=True)
    async def on_cmd_cos_meet(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "cos相遇"): yield result

    @filter.command("三视图", prefix_optional=True)
    async def on_cmd_three_view(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "三视图"): yield result

    @filter.command("穿搭拆解", prefix_optional=True)
    async def on_cmd_outfit_breakdown(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "穿搭拆解"): yield result

    @filter.command("拆解图", prefix_optional=True)
    async def on_cmd_model_kit(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "拆解图"): yield result

    @filter.command("角色界面", prefix_optional=True)
    async def on_cmd_character_ui(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "角色界面"): yield result

    @filter.command("角色设定", prefix_optional=True)
    async def on_cmd_character_sheet(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "角色设定"): yield result

    @filter.command("3D打印", prefix_optional=True)
    async def on_cmd_3d_printing(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "3D打印"): yield result

    @filter.command("微型化", prefix_optional=True)
    async def on_cmd_miniaturize(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "微型化"): yield result

    @filter.command("挂件化", prefix_optional=True)
    async def on_cmd_keychain(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "挂件化"): yield result

    @filter.command("姿势表", prefix_optional=True)
    async def on_cmd_pose_sheet(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "姿势表"): yield result

    @filter.command("高清修复", prefix_optional=True)
    async def on_cmd_hd_restoration(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "高清修复"): yield result

    @filter.command("人物转身", prefix_optional=True)
    async def on_cmd_turn_around(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "人物转身"): yield result

    @filter.command("绘画四宫格", prefix_optional=True)
    async def on_cmd_drawing_grid_4(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "绘画四宫格"): yield result

    @filter.command("发型九宫格", prefix_optional=True)
    async def on_cmd_hairstyle_grid_9(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "发型九宫格"): yield result

    @filter.command("头像九宫格", prefix_optional=True)
    async def on_cmd_avatar_grid_9(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "头像九宫格"): yield result

    @filter.command("表情九宫格", prefix_optional=True)
    async def on_cmd_expression_grid_9(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "表情九宫格"): yield result

    @filter.command("多机位", prefix_optional=True)
    async def on_cmd_multi_camera(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "多机位"): yield result

    @filter.command("电影分镜", prefix_optional=True)
    async def on_cmd_movie_storyboard(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "电影分镜"): yield result

    @filter.command("动漫分镜", prefix_optional=True)
    async def on_cmd_anime_storyboard(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "动漫分镜"): yield result

    @filter.command("真人化", prefix_optional=True)
    async def on_cmd_live_action_1(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "真人化"): yield result

    @filter.command("真人化2", prefix_optional=True)
    async def on_cmd_live_action_2(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "真人化2"): yield result

    @filter.command("半真人", prefix_optional=True)
    async def on_cmd_half_live_action(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "半真人"): yield result

    @filter.command("半融合", prefix_optional=True)
    async def on_cmd_half_fusion(self, event: AstrMessageEvent):
        async for result in self._process_figurine_request(event, "半融合"): yield result

    # --- 核心图片生成处理器 ---
    async def _process_figurine_request(self, event: AstrMessageEvent, cmd: str):
        cmd_text = event.message_str

        cmd_map = {
            "手办化": "figurine_1", "手办化2": "figurine_2", "手办化3": "figurine_3", "手办化4": "figurine_4",
            "手办化5": "figurine_5", "手办化6": "figurine_6",
            "Q版化": "q_version", "痛屋化": "pain_room_1", "痛屋化2": "pain_room_2", "痛车化": "pain_car",
            "cos化": "cos", "cos自拍": "cos_selfie",
            "孤独的我": "clown", "第三视角": "view_3", "鬼图": "ghost", "第一视角": "view_1", "贴纸化": "sticker",
            "玉足": "foot_jade",
            "fumo化": "fumo", "手办化帮助": "help",
            "cos相遇": "cos_meet", "三视图": "three_view", "穿搭拆解": "outfit_breakdown", "拆解图": "model_kit",
            "角色界面": "character_ui",
            "角色设定": "character_sheet", "3D打印": "3d_printing", "微型化": "miniaturize", "挂件化": "keychain",
            "姿势表": "pose_sheet",
            "高清修复": "hd_restoration", "人物转身": "turn_around", "绘画四宫格": "drawing_grid_4",
            "发型九宫格": "hairstyle_grid_9",
            "头像九宫格": "avatar_grid_9", "表情九宫格": "expression_grid_9", "多机位": "multi_camera",
            "电影分镜": "movie_storyboard",
            "动漫分镜": "anime_storyboard", "真人化": "live_action_1", "真人化2": "live_action_2",
            "半真人": "half_live_action", "半融合": "half_fusion"
        }
        prompt_key = cmd_map.get(cmd) if cmd != "bnn" else "bnn_custom"

        user_prompt = None
        if cmd == "bnn":
            user_prompt = cmd_text.strip()
        elif prompt_key == "help":
            user_prompt = self.conf.get("help_text", "帮助信息未配置")
        else:
            user_prompts = self.conf.get("prompts", {})
            user_prompt = user_prompts.get(prompt_key) or self.default_prompts.get(prompt_key, "")

        if cmd == "手办化帮助":
            yield event.plain_result(user_prompt)
            return
        if not user_prompt and cmd != "bnn":
            yield event.plain_result(f"❌ 预设 '{cmd}' 未在配置中找到或prompt为空。")
            return
        if cmd == "bnn" and not user_prompt:
            yield event.plain_result("❌ 命令格式错误: bnn <提示词> [图片]")
            return

        sender_id = event.get_sender_id()
        group_id = event.get_group_id()
        is_master = self.is_global_admin(event)

        if not is_master:
            user_blacklist = self.conf.get("user_blacklist", [])
            if user_blacklist and sender_id in user_blacklist:
                logger.info(f"FigurinePro: 拒绝黑名单用户 {sender_id} 的请求")
                return

            group_blacklist = self.conf.get("group_blacklist", [])
            if group_id and group_blacklist and group_id in group_blacklist:
                logger.info(f"FigurinePro: 拒绝黑名单群组 {group_id} 的请求")
                return

            user_whitelist = self.conf.get("user_whitelist", [])
            if user_whitelist and sender_id not in user_whitelist:
                logger.info(f"FigurinePro: 拒绝非白名单用户 {sender_id} 的请求")
                return

            group_whitelist = self.conf.get("group_whitelist", [])
            if group_id and group_whitelist and group_id not in group_whitelist:
                logger.info(f"FigurinePro: 拒绝非白名单群组 {group_id} 的请求")
                return

            user_count = self._get_user_count(sender_id)
            group_count = self._get_group_count(group_id) if group_id else 0

            user_limit_on = self.conf.get("enable_user_limit", True)
            group_limit_on = self.conf.get("enable_group_limit", False) and group_id

            has_group_count = not group_limit_on or group_count > 0
            has_user_count = not user_limit_on or user_count > 0

            if group_id:
                if not has_group_count and not has_user_count:
                    yield event.plain_result("❌ 本群次数与您的个人次数均已用尽，请联系管理员补充。")
                    return
            else:
                if not has_user_count:
                    yield event.plain_result("❌ 您的使用次数已用完，请联系管理员补充。")
                    return

        # 【修改】调用 get_images 并处理图片列表
        if not self.iwf or not (img_bytes_list := await self.iwf.get_images(event)):
            yield event.plain_result("请发送或引用一张图片，或@一个用户再试。")
            return

        # 【修改】根据指令决定是单图还是多图
        images_to_process = []
        if cmd == "bnn":
            MAX_IMAGES = 5
            original_count = len(img_bytes_list)
            if original_count > MAX_IMAGES:
                images_to_process = img_bytes_list[:MAX_IMAGES]
                yield event.plain_result(f"🎨 检测到 {original_count} 张图片，已自动选取前 {MAX_IMAGES} 张进行生成…")
            else:
                images_to_process = img_bytes_list
                yield event.plain_result(f"🎨 检测到 {len(images_to_process)} 张图片，正在生成 [{cmd}] 风格图片...")
        else:
            # 其他指令默认只使用第一张图，保持原有逻辑
            images_to_process = [img_bytes_list[0]]
            yield event.plain_result(f"🎨 收到请求，正在生成 [{cmd}] 风格图片...")

        start_time = datetime.now()
        # 【修改】将图片列表传入
        res = await self._call_api(images_to_process, user_prompt)
        elapsed = (datetime.now() - start_time).total_seconds()

        if isinstance(res, bytes):
            if not is_master:
                if self.conf.get("enable_group_limit", False) and group_id and self._get_group_count(group_id) > 0:
                    await self._decrease_group_count(group_id)
                elif self.conf.get("enable_user_limit", True) and self._get_user_count(sender_id) > 0:
                    await self._decrease_user_count(sender_id)

            caption_parts = [f"✅ 生成成功 ({elapsed:.2f}s)", f"预设: {cmd}"]
            if is_master:
                caption_parts.append("剩余次数: ∞")
            else:
                if self.conf.get("enable_user_limit", True): caption_parts.append(
                    f"个人剩余: {self._get_user_count(sender_id)}")
                if self.conf.get("enable_group_limit", False) and group_id: caption_parts.append(
                    f"本群剩余: {self._get_group_count(group_id)}")
            yield event.chain_result([Image.fromBytes(res), Plain(" | ".join(caption_parts))])
        else:
            yield event.plain_result(f"❌ 生成失败 ({elapsed:.2f}s)\n原因: {res}")

    # --- API 调用辅助函数 ---
    async def _get_api_key(self) -> str | None:
        keys = self.conf.get("api_keys", [])
        if not keys: return None
        async with self.key_lock:
            key = keys[self.key_index]
            self.key_index = (self.key_index + 1) % len(keys)
            return key

    def _extract_image_url_from_response(self, data: Dict[str, Any]) -> str | None:
        try:
            return data["choices"][0]["message"]["images"][0]["image_url"]["url"]
        except (IndexError, TypeError, KeyError):
            pass
        try:
            return data["choices"][0]["message"]["images"][0]["url"]
        except (IndexError, TypeError, KeyError):
            pass
        try:
            content_text = data["choices"][0]["message"]["content"]
            url_match = re.search(r'https?://[^\s<>")\]]+', content_text)
            if url_match: return url_match.group(0).rstrip(")>,'\"")
        except (IndexError, TypeError, KeyError):
            pass
        return None

    # 【修改】修改函数签名以接收图片列表，并构建多图请求体
    async def _call_api(self, image_bytes_list: List[bytes], prompt: str) -> bytes | str:
        api_url = self.conf.get("api_url")
        if not api_url: return "API URL 未配置"
        api_model = self.conf.get("api_model")
        if not api_url: return "API URL 未配置"
        api_key = await self._get_api_key()
        if not api_key: return "无可用的 API Key"

        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

        # 构建 content 列表，先添加 text，再循环添加所有 image
        content = [{"type": "text", "text": prompt}]
        for image_bytes in image_bytes_list:
            img_b64 = base64.b64encode(image_bytes).decode("utf-8")
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{img_b64}"
                }
            })

        payload = {
            "model": api_model,
            "max_tokens": 1500,
            "stream": False,
            "messages": [{
                "role": "user",
                "content": content
            }]
        }

        try:
            if not self.iwf: return "ImageWorkflow 未初始化"
            async with self.iwf.session.post(api_url, json=payload, headers=headers, proxy=self.iwf.proxy,
                                             timeout=120) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"API 请求失败: HTTP {resp.status}, 响应: {error_text}")
                    return f"API请求失败 (HTTP {resp.status}): {error_text[:200]}"
                data = await resp.json()
                if "error" in data: return data["error"].get("message", json.dumps(data["error"]))
                gen_image_url = self._extract_image_url_from_response(data)
                if not gen_image_url:
                    error_msg = f"API响应中未找到图片数据。原始响应 (部分): {str(data)[:500]}..."
                    logger.error(f"API响应中未找到图片数据: {data}")
                    return error_msg
                if gen_image_url.startswith("data:image/"):
                    b64_data = gen_image_url.split(",", 1)[1]
                    return base64.b64decode(b64_data)
                else:
                    return await self.iwf._download_image(gen_image_url) or "下载生成的图片失败"
        except asyncio.TimeoutError:
            logger.error("API 请求超时");
            return "请求超时"
        except Exception as e:
            logger.error(f"调用 API 时发生未知错误: {e}", exc_info=True);
            return f"发生未知错误: {e}"

    async def terminate(self):
        if self.iwf: await self.iwf.terminate()
        logger.info("[FigurinePro] 插件已终止")
