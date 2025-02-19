from astrbot.api.all import *
import random
import requests
import os
import time
import shutil

@register("poke_monitor", "Your Name", "监控戳一戳事件插件", "1.2.0")
class PokeMonitorPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.user_poke_timestamps = {}
        self.poke_responses = [
            "别戳啦！",
            "哎呀，还戳呀，别闹啦！",
            "别戳我啦  你要做什么  不理你了"
        ]
        self._clean_legacy_directories()

    def _clean_legacy_directories(self):
        """安全清理旧目录（仅删除特定目录）"""
        legacy_dirs = [
            os.path.abspath("./data/plugins/poke_monitor"),  # 旧版本目录
            os.path.abspath("./data/plugins/plugins/poke_monitor")  # 防止误删其他插件
        ]
        
        for path in legacy_dirs:
            try:
                if os.path.exists(path):
                    shutil.rmtree(path)
                    self.logger.info(f"已清理旧目录: {path}")
            except Exception as e:
                self.logger.error(f"清理目录 {path} 失败: {str(e)}")

    @event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        message_obj = event.message_obj
        raw_message = message_obj.raw_message
        is_super = False  # 超级加倍标志

        if raw_message.get('post_type') == 'notice' and \
                raw_message.get('notice_type') == 'notify' and \
                raw_message.get('sub_type') == 'poke':
            bot_id = raw_message.get('self_id')
            sender_id = raw_message.get('user_id')
            target_id = raw_message.get('target_id')

            now = time.time()
            three_minutes_ago = now - 3 * 60

            # 清理旧记录
            if sender_id in self.user_poke_timestamps:
                self.user_poke_timestamps[sender_id] = [
                    t for t in self.user_poke_timestamps[sender_id] if t > three_minutes_ago
                ]

            if bot_id and sender_id and target_id:
                # 用户戳机器人
                if str(target_id) == str(bot_id):
                    # 记录戳一戳
                    if sender_id not in self.user_poke_timestamps:
                        self.user_poke_timestamps[sender_id] = []
                    self.user_poke_timestamps[sender_id].append(now)

                    # 文本回复
                    poke_count = len(self.user_poke_timestamps[sender_id])
                    if poke_count < 3:
                        response = self.poke_responses[poke_count-1] if poke_count <= len(self.poke_responses) else self.poke_responses[-1]
                        yield event.plain_result(response)

                    # 概率戳回
                    if random.random() < 0.3:
                        if random.random() < 0.1:
                            poke_times = 10
                            yield event.plain_result("喜欢戳是吧")
                            is_super = True
                        else:
                            poke_times = 1
                            yield event.plain_result("戳回去")

                        # 发送戳一戳
                        if event.get_platform_name() == "aiocqhttp":
                            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                            assert isinstance(event, AiocqhttpMessageEvent)
                            client = event.bot
                            group_id = raw_message.get('group_id')
                            payloads = {"user_id": sender_id}
                            if group_id:
                                payloads["group_id"] = group_id
                            for _ in range(poke_times):
                                try:
                                    await client.api.call_action('send_poke', **payloads)
                                except Exception as e:
                                    self.logger.error(f"发送戳一戳失败: {str(e)}")

                # 用户戳其他人（且不是机器人自己触发的）
                elif str(sender_id) != str(bot_id):  # 新增关键判断
                    # 表情包处理逻辑
                    available_actions = ["咬", "捣", "玩", "拍", "丢", "撕", "爬"]
                    selected_action = random.choice(available_actions)  # 定义selected_action

                    url_mapping = {
                        "咬": "https://api.lolimi.cn/API/face_suck/api.php",
                        "捣": "https://api.lolimi.cn/API/face_pound/api.php",
                        "玩": "https://api.lolimi.cn/API/face_play/api.php",
                        "拍": "https://api.lolimi.cn/API/face_pat/api.php",
                        "丢": "https://api.lolimi.cn/API/diu/api.php",
                        "撕": "https://api.lolimi.cn/API/si/api.php",
                        "爬": "https://api.lolimi.cn/API/pa/api.php"
                    }
                    url = url_mapping.get(selected_action)
                    params = {'QQ': target_id}
                    
                    try:
                        response = requests.get(url, params=params, timeout=5)
                        if response.status_code == 200:
                            # 跨平台安全路径
                            save_dir = os.path.join("data", "plugins", "astrbot_plugin_pock", "poke_monitor")
                            os.makedirs(save_dir, exist_ok=True)
                            
                            # 唯一文件名防止冲突
                            filename = f"{selected_action}_{target_id}_{int(time.time())}.gif"
                            image_path = os.path.join(save_dir, filename)
                            
                            with open(image_path, "wb") as f:
                                f.write(response.content)
                            yield event.image_result(image_path)
                        else:
                            yield event.plain_result(f"表情包请求失败，状态码：{response.status_code}")
                    except Exception as e:
                        yield event.plain_result(f"表情包处理出错：{str(e)}")
