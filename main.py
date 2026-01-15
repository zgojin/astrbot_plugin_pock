import asyncio
import json
import os
import random
import time
import uuid
import shutil
from pathlib import Path

import aiofiles
import aiohttp

import astrbot.api.event.filter as filter
from astrbot.api.all import *
from astrbot.api import logger

@register("poke_monitor", "长安某", "监控戳一戳事件插件", "2.1.1")
class PokeMonitorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        self.group_poke_timestamps = {}
        self.group_cooldown_end_time = {}
        self.emoji_last_used_time = 0
        self.emoji_lock = asyncio.Lock()
        self.llm_lock = asyncio.Lock()
        self.data_dir = Path("data") / "plugin_data" / "poke_monitor"
        self.temp_image_dir = self.data_dir / "temp_images"
        
        # 确保目录存在
        self.temp_image_dir.mkdir(parents=True, exist_ok=True)

        try:
            emoji_settings = self.config.get("emoji_settings", {})
            self.emoji_url_mapping = json.loads(
                emoji_settings.get("emoji_url_mapping", "{}")
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"解析 emoji_url_mapping 配置失败: {e}，将使用空字典。")
            self.emoji_url_mapping = {}

        # 初始化时清理旧的临时文件
        self._clean_temp_directory()
        # 清理旧版本的遗留目录（可选保留）
        self._clean_legacy_directories()

        self.func_tools_mgr = context.get_llm_tool_manager()
        self.conversation_manager = context.conversation_manager

    def _clean_legacy_directories(self):
        """清理旧版本插件可能产生的遗留目录"""
        legacy_dirs = [
            Path("./data/plugins/poke_monitor"),
            Path("./data/plugins/plugins/poke_monitor"),
            Path("./data/plugins/astrbot_plugin_pock/poke_monitor")
        ]
        for path in legacy_dirs:
            try:
                if path.exists():
                    shutil.rmtree(path)
                    logger.info(f"清理旧目录成功: {path}")
            except Exception as e:
                # 旧目录不存在或清理失败不影响运行，仅记录 debug
                logger.debug(f"旧目录清理跳过: {str(e)}")

    def _clean_temp_directory(self):
        """清理当前插件的临时图片目录"""
        try:
            if self.temp_image_dir.exists():
                for file_path in self.temp_image_dir.iterdir():
                    if file_path.is_file():
                        file_path.unlink()  # 删除文件
        except Exception as e:
            logger.error(f"清理临时目录失败: {str(e)}")

    # === 黑白名单逻辑 ===
    def _is_group_allowed(self, group_id: int) -> bool:
        """
        检查群组权限。
        优先级: 黑名单 > 白名单 > 默认允许
        """
        g_id = int(group_id)

        # 1. 检查黑名单
        blacklist_settings = self.config.get("blacklist_settings", {})
        if blacklist_settings.get("enabled", False):
            blocked_groups = [int(x) for x in blacklist_settings.get("blocked_groups", [])]
            if g_id in blocked_groups:
                return False

        # 2. 检查白名单
        whitelist_settings = self.config.get("whitelist_settings", {})
        if whitelist_settings.get("enabled", False):
            allowed_groups = [int(x) for x in whitelist_settings.get("allowed_groups", [])]
            if g_id not in allowed_groups:
                return False
        
        # 3. 默认允许
        return True

    # === 分群计数 ===
    def _record_group_poke(self, group_id: int) -> int:
        """记录指定群聊的戳一戳行为，并返回该群在2分钟内的被戳次数"""
        now = time.time()
        two_minutes_ago = now - 120

        timestamps = self.group_poke_timestamps.get(group_id, [])
        # 清理2分钟前的记录
        valid_timestamps = [t for t in timestamps if t > two_minutes_ago]
        # 添加当前记录
        valid_timestamps.append(now)

        self.group_poke_timestamps[group_id] = valid_timestamps
        return len(valid_timestamps)

    async def _get_user_display_name(
        self, event: AstrMessageEvent, group_id: int, user_id: int
    ) -> str:
        # 获取用户昵称
        client = event.bot
        try:
            payloads = {"group_id": group_id, "user_id": user_id, "no_cache": True}
            member_info = await client.api.call_action(
                "get_group_member_info", **payloads
            )
            display_name = member_info.get("card")
            return (
                display_name
                if display_name
                else member_info.get("nickname", f"QQ用户{user_id}")
            )
        except Exception as e:
            logger.error(
                f"通过API获取群成员信息失败 (group: {group_id}, user: {user_id}): {e}"
            )
            return f"某位群友({user_id})"

    async def _get_llm_response(self, poke_count, event, user_nickname=""):
        # 获取 LLM 回复
        curr_cid = await self.conversation_manager.get_curr_conversation_id(
            event.unified_msg_origin
        )
        context = []
        if curr_cid:
            conversation = await self.conversation_manager.get_conversation(
                event.unified_msg_origin, curr_cid
            )
            if conversation and conversation.history:
                context = json.loads(conversation.history)

        llm_config = self.config.get("llm_settings", {})
        prompt_map = {
            1: llm_config.get(
                "poke_1_prompt",
                "用户“{user_nickname}”突然戳了你一下，回复要略带无奈，请求不要打扰：",
            ),
            2: llm_config.get(
                "poke_2_prompt",
                "用户“{user_nickname}”戳了你一下，这是你第二次被戳，回复要带点撒娇和警告：",
            ),
            3: llm_config.get(
                "poke_3_prompt",
                "用户“{user_nickname}”戳了你一下，已经是第三次了，回复要表示无奈和生气：",
            ),
        }
        prompt_template = prompt_map.get(
            poke_count,
            llm_config.get(
                "poke_default_prompt",
                "用户“{user_nickname}”又戳你了，回复要俏皮、有趣：",
            ),
        )

        prompt_prefix = prompt_template.format(user_nickname=user_nickname)
        system_prompt = llm_config.get(
            "system_prompt",
            "用户戳你时要回复俏皮、有趣的内容，每次回复风格要略有变化，避免重复。",
        )

        provider = self.context.get_using_provider()
        try:
            llm_response = await provider.text_chat(
                prompt=prompt_prefix,
                contexts=context,
                func_tool=self.func_tools_mgr,
                system_prompt=system_prompt,
            )
            return (
                llm_response.completion_text.strip()
                if llm_response.role == "assistant"
                else "呜哇，被戳到啦！"
            )
        except Exception as e:
            logger.error(f"LLM调用失败: {str(e)}")
            return "哎呀，我有点懵，等下再戳我吧~"

    def _should_reply_text(self, group_id: int):
        return time.time() >= self.group_cooldown_end_time.get(group_id, 0)

    def _set_cooldown(self, group_id: int):
        self.group_cooldown_end_time[group_id] = time.time() + 300

    async def _handle_poke_back(self, event, sender_id: int, group_id: int):
        """处理反击（回戳）逻辑"""
        feature_switches = self.config.get("feature_switches", {})
        if not feature_switches.get("poke_back_enabled", True):
            return

        poke_probabilities = self.config.get("poke_probabilities", {})
        if random.random() < poke_probabilities.get("poke_back_probability", 0.3):
            is_super = random.random() < poke_probabilities.get(
                "super_poke_probability", 0.1
            )
            poke_times = 5 if is_super else 1
            yield event.plain_result("喜欢戳是吧" if is_super else "戳回去")

            client = event.bot
            payloads = {"user_id": sender_id, "group_id": group_id}
            for _ in range(poke_times):
                try:
                    await client.api.call_action("send_poke", **payloads)
                except Exception as e:
                    logger.error(f"QQ群聊戳回失败: {str(e)}")
                    break

    async def _handle_emoji(self, event, target_id: int):
        """处理表情包生成逻辑"""
        feature_switches = self.config.get("feature_switches", {})
        if not feature_switches.get("emoji_trigger_enabled", True):
            return

        emoji_settings = self.config.get("emoji_settings", {})
        async with self.emoji_lock:
            if time.time() - self.emoji_last_used_time < emoji_settings.get(
                "emoji_cooldown_seconds", 20
            ):
                return
            if random.random() >= emoji_settings.get(
                "random_emoji_trigger_probability", 0.5
            ):
                return
            if not self.emoji_url_mapping:
                return

            self.emoji_last_used_time = time.time()
            selected_action = random.choice(list(self.emoji_url_mapping.keys()))
            emoji_type = self.emoji_url_mapping[selected_action]
            url = "https://api.lolimi.cn/API/preview/api.php"
            params = {"qq": target_id, "action": "create_meme", "type": emoji_type}

            timeout = emoji_settings.get("post_timeout", 20)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url, params=params, timeout=timeout
                    ) as response:
                        if response.status == 200:
                            content = await response.read()
                            
                            # 使用 UUID 生成唯一文件名，防止冲突
                            filename = f"{selected_action}_{target_id}_{uuid.uuid4().hex}.gif"
                            image_path = self.temp_image_dir / filename
                            
                            async with aiofiles.open(image_path, "wb") as f:
                                await f.write(content)
                            yield event.image_result(str(image_path))
                            
                            # 发送后尝试清理
                            try:
                                if image_path.exists():
                                    image_path.unlink()
                            except Exception as e:
                                logger.error(f"表情包临时文件清理失败: {str(e)}")
            except Exception as e:
                logger.error(f"表情包请求失败: {str(e)}")

    @event_message_type(filter.EventMessageType.ALL)
    async def on_group_message(self, event: AstrMessageEvent):
        # raw_message 可能为 None,也可能不是 dict
        # 跳过
        raw_message = event.message_obj.raw_message
        if not raw_message or not isinstance(raw_message, dict):
            return

        # 检查是否为 poke 消息
        if not (
            raw_message.get("post_type") == "notice"
            and raw_message.get("notice_type") == "notify"
            and raw_message.get("sub_type") == "poke"
        ):
            return

        group_id = raw_message.get("group_id")
        if not group_id:
            return
        
        # 检查权限
        if not self._is_group_allowed(group_id):
            return
        
        bot_id = raw_message.get("self_id")
        sender_id = raw_message.get("user_id")
        target_id = raw_message.get("target_id")
        
        if not (bot_id and sender_id and target_id):
            return

        # 自己(Bot)被戳
        if str(target_id) == str(bot_id):
            user_display_name = await self._get_user_display_name(
                event, group_id, sender_id
            )

            async with self.llm_lock:
                poke_count = self._record_group_poke(group_id)
                llm_settings = self.config.get("llm_settings", {})
                max_pokes = llm_settings.get("max_poke_count_before_cooldown", 3)
                if poke_count > max_pokes:
                    self._set_cooldown(group_id)

                feature_switches = self.config.get("feature_switches", {})
                # 检查是否在回复冷却中
                if feature_switches.get(
                    "poke_response_enabled", True
                ) and self._should_reply_text(group_id):
                    response = await self._get_llm_response(
                        poke_count, event, user_display_name
                    )
                    yield event.plain_result(response)

            # 触发回戳
            async for result in self._handle_poke_back(event, sender_id, group_id):
                yield result

        # 群友互戳
        elif str(sender_id) != str(bot_id):
            async for result in self._handle_emoji(event, target_id):
                yield result
