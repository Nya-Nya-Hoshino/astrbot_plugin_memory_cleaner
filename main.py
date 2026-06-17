import json
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.message_components import Plain

try:
    from astrbot.core.utils.active_event_registry import active_event_registry
    _HAS_ACTIVE_REGISTRY = True
except ImportError:
    _HAS_ACTIVE_REGISTRY = False
    logger.warning("[memory_cleaner] active_event_registry 不可用，将跳过 Agent 停止步骤")


class Main(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self._parse_admin_list()
        self.debug = self.config.get("debug_mode", False)
        logger.info(f"[memory_cleaner] 插件已加载 | 管理员: {self.admin_ids} | debug: {self.debug}")

    # ==================== 配置解析 ====================

    def _parse_admin_list(self):
        raw = self.config.get("admin_users", "")
        if isinstance(raw, str) and raw.strip():
            self.admin_ids = set(
                uid.strip() for uid in raw.split(",") if uid.strip()
            )
        else:
            self.admin_ids = set()
        logger.info(f"[memory_cleaner] 管理员列表: {self.admin_ids}")

    def _is_authorized(self, event: AstrMessageEvent) -> bool:
        """
        判断用户是否有权限执行清洗命令。
        优先级：内置 is_admin() > 配置的 admin_users 列表
        """
        # 1. AstrBot 内置管理员角色
        if event.is_admin():
            return True

        # 2. 插件配置的自定义管理员列表
        sender_id = event.get_sender_id()
        if sender_id in self.admin_ids:
            return True

        return False

    # ==================== 辅助方法 ====================

    async def _get_persona_prompt(self, event: AstrMessageEvent) -> str:
        """从 AstrBot 当前配置中读取人格 System Prompt"""
        try:
            umo = event.unified_msg_origin
            persona_mgr = self.context.persona_manager
            persona = await persona_mgr.get_default_persona_v3(umo)
            prompt = persona.get("prompt", "") if persona else ""
            if not prompt:
                logger.warning("[memory_cleaner] 未获取到 persona prompt，将使用空字符串")
            return prompt
        except Exception as e:
            logger.error(f"[memory_cleaner] 读取 persona 失败: {e}")
            return ""

    async def _llm_check(self, system_prompt: str, user_question: str) -> str:
        """
        使用 LLM 执行一次独立的问答检测。
        返回 LLM 的回复文本。
        """
        try:
            provider = self.context.get_using_provider(None)
            if not provider:
                logger.warning("[memory_cleaner] 没有可用的 LLM provider")
                return "[ERROR] 没有可用的 LLM provider，无法执行检测"

            result = await provider.text_chat(
                system_prompt=system_prompt,
                prompt=user_question,
            )
            text = result.completion_text if hasattr(result, "completion_text") else str(result)
            return text or "[ERROR] LLM 返回空回复"
        except Exception as e:
            logger.error(f"[memory_cleaner] LLM 调用失败: {e}")
            return f"[ERROR] LLM 调用异常: {e}"

    async def _send_long_msg(self, event: AstrMessageEvent, text: str):
        """分条发送长消息，避免单条过长"""
        if not text:
            return
        max_len = 500
        for i in range(0, len(text), max_len):
            chunk = text[i:i + max_len]
            yield event.plain_result(chunk)

    # ==================== 诊断 ====================

    @filter.command("诊断会话")
    async def diagnose_session(self, event: AstrMessageEvent):
        """输出当前会话的 umo、平台、会话模式信息"""
        umo = event.unified_msg_origin
        parts = umo.split(":")
        platform = parts[0] if len(parts) > 0 else "?"
        msg_type = parts[1] if len(parts) > 1 else "?"
        session_id = parts[2] if len(parts) > 2 else "?"
        has_user = len(parts) > 3

        conv_mgr = self.context.conversation_manager
        curr_cid = await conv_mgr.get_curr_conversation_id(umo) or "(无)"
        conversations = await conv_mgr.get_conversations(umo) or []
        conv_count = len(conversations)

        yield event.plain_result(
            f"🔍 会话诊断\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"umo 完整值: {umo}\n"
            f"  ├ 平台:     {platform}\n"
            f"  ├ 消息类型: {msg_type}\n"
            f"  ├ 会话ID:   {session_id}\n"
            f"  └ 含用户ID: {'是 → group_unique_session' if has_user else '否 → group_shared_session'}\n\n"
            f"当前对话ID: {curr_cid[:16]}...\n"
            f"对话总数:   {conv_count}"
        )
        event.stop_event()

    @staticmethod
    def _extract_text(content) -> str:
        """从 OpenAI 格式的 content 中提取纯文本（兼容 str 和 list）"""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(str(item.get("text", "")))
                    elif item.get("text"):
                        parts.append(str(item["text"]))
                elif isinstance(item, str):
                    parts.append(item)
            return " ".join(parts)
        return str(content) if content else ""

    # ==================== 记忆查询 ====================

    @filter.command("记忆查询")
    async def query_memory(self, event: AstrMessageEvent):
        """
        /记忆查询 —— 所有人可用
        输出当前会话的对话记忆摘要，不泄露 Prompt/人格设定
        """
        umo = event.unified_msg_origin
        user_name = event.get_sender_name()
        conv_mgr = self.context.conversation_manager

        try:
            # 获取当前对话 ID 和所有对话
            curr_cid = await conv_mgr.get_curr_conversation_id(umo) or ""
            conversations = await conv_mgr.get_conversations(umo) or []

            if not conversations:
                yield event.plain_result("📭 当前没有任何对话记忆。")
                return

            # 统计总轮次
            total_turns = 0
            for conv in conversations:
                history = self._parse_history(conv.history)
                total_turns += len(history) // 2

            # 只取当前对话的最近 N 条用户可见消息
            recent_msgs = []
            for conv in conversations:
                if conv.cid == curr_cid:
                    history = self._parse_history(conv.history)
                    for item in history:
                        role = item.get("role", "")
                        content = self._extract_text(item.get("content", ""))
                        if role in ("user", "assistant") and content.strip():
                            recent_msgs.append((role, content))
                    break

            # 构建输出
            lines = [
                f"🧠 记忆查询 | 请求者: {user_name}",
                "━━━━━━━━━━━━━━━━━━━━",
                f"📊 对话总数: {len(conversations)}",
                f"💬 总对话轮次: {total_turns}",
            ]

            if curr_cid:
                lines.append(f"📍 当前对话: {curr_cid[:8]}...")
            lines.append("")

            if not recent_msgs:
                lines.append("📭 当前对话无历史记录。")
            else:
                # 取最近 6 轮（12条消息）
                show_count = min(len(recent_msgs), 12)
                recent = recent_msgs[-show_count:]
                lines.append(f"📜 最近 {show_count//2} 轮对话:")
                for role, content in recent:
                    prefix = "👤 用户" if role == "user" else "🤖 助手"
                    # 每条消息截断到 100 字符
                    short = content[:100] + ("..." if len(content) > 100 else "")
                    lines.append(f"  {prefix}: {short}")
                lines.append("")

            lines.append("💡 仅展示最近对话历史，不含系统 Prompt 设定。")
            lines.append("   管理员可使用 /清洗记忆 清除所有记忆。")

            async for chunk in self._send_long_msg(event, "\n".join(lines)):
                yield chunk

            event.stop_event()

        except Exception as e:
            logger.error(f"[memory_cleaner] 记忆查询异常: {e}", exc_info=True)
            yield event.plain_result(f"❌ 记忆查询失败: {e}")

    def _parse_history(self, history) -> list:
        """安全解析对话历史 JSON，始终返回 list"""
        if not history:
            return []
        try:
            if isinstance(history, str):
                data = json.loads(history)
            elif isinstance(history, list):
                data = history
            else:
                return []
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    # ==================== 核心指令 ====================

    @filter.command("清洗记忆")
    async def clean_memory(self, event: AstrMessageEvent):
        """
        /清洗记忆 —— 重建会话状态，强制以最新 Prompt 为起点重新推理
        删除旧对话 → 新建会话 → 清除会话偏好 → 清除 workspace → 重载 Prompt → 人格内化 → 理解度检测 → 评分
        """
        if not self._is_authorized(event):
            yield event.plain_result("⛔ 权限不足。只有授权的管理员才能执行 /清洗记忆。\n请在插件配置中设置 admin_users（QQ号，逗号分隔）。")
            return

        user_name = event.get_sender_name()
        umo = event.unified_msg_origin
        debug = self.debug

        yield event.plain_result(f"🧹 {user_name} 发起了状态重建...")

        log_lines = [f"=== 状态重建日志 | 操作用户: {user_name} ==="]
        def log(msg):
            logger.info(f"[memory_cleaner] {msg}")
            log_lines.append(msg)

        try:
            # ---- 第1步：停止活跃 Agent ----
            log("[1/8] 停止活跃 Agent...")
            if _HAS_ACTIVE_REGISTRY:
                try:
                    active_event_registry.stop_all(umo, exclude=event)
                    log("  ✓ Agent 已停止")
                except Exception as e:
                    log(f"  ⚠ Agent 停止异常: {e}")
            else:
                log("  ⚠ 跳过（模块不可用）")

            # ---- 第2步：读取最新 Prompt（删数据前先读）----
            log("[2/8] 读取当前 AstrBot 配置的人格 Prompt...")
            system_prompt = await self._get_persona_prompt(event)
            if not system_prompt:
                log("  ⚠ 未读取到有效 Prompt！")
                yield event.plain_result("⚠️ 未读取到当前人格 Prompt！请检查 AstrBot 的 Persona 配置。")
                return
            log(f"  ✓ Prompt 长度: {len(system_prompt)} 字符")
            if debug:
                yield event.plain_result(f"📝 Prompt 预览:\n{system_prompt[:200]}...")

            # ---- 第3步：删除所有旧对话 ----
            log("[3/8] 删除所有旧对话...")
            conv_mgr = self.context.conversation_manager
            old_count = 0
            try:
                old_convs = await conv_mgr.get_conversations(umo) or []
                old_count = len(old_convs)
                await conv_mgr.delete_conversations_by_user_id(umo)
                log(f"  ✓ 已删除 {old_count} 个旧对话")
            except Exception as e:
                log(f"  ⚠ 批量删除失败: {e}")
                try:
                    cid = await conv_mgr.get_curr_conversation_id(umo)
                    if cid:
                        await conv_mgr.update_conversation(umo, cid, [])
                        log("  ✓ 改为清空当前对话")
                except Exception as e2:
                    log(f"  ⚠ 清空也失败: {e2}")

            # ---- 第4步：创建全新对话（fresh UUID，零历史）----
            log("[4/8] 创建全新推理会话...")
            try:
                new_cid = await conv_mgr.new_conversation(umo, event.get_platform_id())
                log(f"  ✓ 新对话已创建: {new_cid[:16]}...")
            except Exception as e:
                log(f"  ⚠ 创建新对话失败: {e}")

            yield event.plain_result("🗑️ 旧会话已清除，新推理会话已建")

            # ---- 第5步：清除会话级偏好（sp）----
            log("[5/8] 清除会话级配置偏好...")
            try:
                from astrbot.core import sp
                await sp.session_remove(umo, "sel_conv_id")
                await sp.session_remove(umo, "session_service_config")
                log("  ✓ 会话偏好已清除")
            except Exception as e:
                log(f"  ⚠ 清除偏好失败: {e}")

            # ---- 第6步：清除 Workspace EXTRA_PROMPT.md ----
            log("[6/8] 检查并清除 workspace 残留指令...")
            try:
                from pathlib import Path
                from astrbot.core.utils.astrbot_path import get_astrbot_workspaces_path
                normalized = umo.replace(":", "_").replace("/", "_")
                ws_extra = Path(get_astrbot_workspaces_path()) / normalized / "EXTRA_PROMPT.md"
                if ws_extra.is_file():
                    ws_extra.unlink()
                    log(f"  ✓ 已删除 {ws_extra}")
                else:
                    log("  ✓ 无需清除（文件不存在）")
            except Exception as e:
                log(f"  ⚠ workspace 清理失败: {e}")

            # ---- 第7步：刷新 Persona + 人格内化 ----
            log("[7/8] 刷新 Persona 缓存并执行人格内化...")
            try:
                self.context.persona_manager.get_v3_persona_data()
                log("  ✓ Persona 缓存已刷新")
            except Exception as e:
                log(f"  ⚠ 缓存刷新异常: {e}")

            rebuild_prompt = (
                "你刚刚经历了一次完整的状态重建。旧的对话记录、会话偏好、工作区指令均已被清除。\n"
                "现在，你是全新的实例。请仔细阅读并完全内化以下系统设定，"
                "这将成为你唯一的行为准则。请用一段话（50-100字）总结你理解的人设和核心行为规范：\n\n"
                f"=== 系统设定 ===\n{system_prompt}\n=== 设定结束 ==="
            )
            rebuild_result = await self._llm_check(
                "你是一个认真负责的助手，请认真阅读并总结系统设定。",
                rebuild_prompt
            )
            log("  ✓ 人格内化完成")
            if debug:
                yield event.plain_result(f"🔄 内化结果:\n{rebuild_result[:300]}")

            # ---- 第8步：理解度检测 + 评分 ----
            log("[8/8] 执行 Prompt 理解度检测...")
            check_prompt = (
                "你是一位严格的 Prompt 合规性审计员。下面会提供一份「系统设定」和一份「助手内化自述」。\n"
                "请判断助手是否真正理解并遵循了系统设定。请按以下维度以 JSON 格式评分：\n\n"
                "1. persona_match (0-100)：人设一致性 —— 身份、角色、背景是否完全匹配\n"
                "2. style_match (0-100)：风格一致性 —— 语气、措辞、口癖是否符合设定\n"
                "3. rule_compliance (0-100)：规则遵循度 —— 是否理解所有行为限制和规则\n"
                "4. old_persona_leak (0-100)：旧人格泄露度 —— 是否残留旧设定痕迹（越高越严重）\n"
                "5. old_memory_leak (0-100)：旧记忆泄露度 —— 是否引用了不应存在的历史信息（越高越严重）\n"
                "6. overall (0-100)：综合评分\n\n"
                f"=== 系统设定 ===\n{system_prompt}\n=== 设定结束 ===\n\n"
                f"=== 助手内化自述 ===\n{rebuild_result}\n=== 自述结束 ===\n\n"
                "请仅输出 JSON（不要 markdown 代码块）：\n"
                '{"persona_match": 分, "style_match": 分, "rule_compliance": 分, '
                '"old_persona_leak": 分, "old_memory_leak": 分, "overall": 分, "comment": "评语"}'
            )
            check_result = await self._llm_check(
                "你是一个严格但公正的审计员。只输出 JSON。",
                check_prompt
            )
            log(f"  📊 原始结果: {check_result[:500]}")

            # ---- 解析评分 ----
            try:
                scores = self._parse_scores(check_result)
            except Exception as e:
                log(f"  ⚠ 解析失败: {e}")
                scores = {
                    "persona_match": "?", "style_match": "?", "rule_compliance": "?",
                    "old_persona_leak": "?", "old_memory_leak": "?", "overall": "?",
                    "comment": f"解析失败: {check_result[:200]}"
                }

            report = self._build_report(user_name, system_prompt, scores, debug)
            async for chunk in self._send_long_msg(event, report):
                yield chunk

            if debug:
                async for chunk in self._send_long_msg(event, f"📋 调试日志:\n" + "\n".join(log_lines)):
                    yield chunk

            log("=== 重建完成 ===")
            event.stop_event()

        except Exception as e:
            logger.error(f"[memory_cleaner] 状态重建异常: {e}", exc_info=True)
            yield event.plain_result(f"❌ 状态重建过程发生错误: {e}\nAstrBot 运行不受影响。")

    # ==================== 评分解析 ====================

    def _parse_scores(self, raw: str) -> dict:
        """解析 LLM 返回的 JSON 评分"""
        # 尝试直接从返回中提取 JSON
        text = raw.strip()

        # 移除可能的 markdown 代码块标记
        if text.startswith("```"):
            lines = text.split("\n")
            # 去掉首行 ``` 和末行 ```
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        # 找到第一个 { 到最后一个 }
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]

        scores = json.loads(text)

        # 验证和补全字段
        expected_keys = ["persona_match", "style_match", "rule_compliance",
                         "old_persona_leak", "old_memory_leak", "overall"]
        for key in expected_keys:
            if key not in scores:
                scores[key] = "?"
            elif isinstance(scores[key], (int, float)):
                scores[key] = min(100, max(0, int(scores[key])))

        if "comment" not in scores:
            scores["comment"] = ""

        return scores

    # ==================== 报告生成 ====================

    def _build_report(self, user_name: str, prompt: str, scores: dict, debug: bool) -> str:
        """构建清洗结果报告"""

        def _bar(score, width=10):
            """生成进度条"""
            if isinstance(score, str) or score == "?":
                return f"{score:>4}"
            filled = int(score / 100 * width)
            bar = "█" * filled + "░" * (width - filled)
            return f"{bar} {score:>3}"

        overall = scores.get("overall", "?")
        if isinstance(overall, (int, float)) and not isinstance(overall, str):
            if overall >= 85:
                emoji, status = "✅", "优秀 —— 人格已成功切换！"
            elif overall >= 65:
                emoji, status = "⚠️", "一般 —— 基本可用，建议再次清洗或用调试模式排查"
            else:
                emoji, status = "❌", "不佳 —— 建议检查 Prompt 配置后重新清洗"
        else:
            emoji, status = "❓", "无法判定"

        lines = [
            f"{emoji} 记忆清洗报告 | 操作者: {user_name}",
            "━━━━━━━━━━━━━━━━━━━━",
            f"📊 综合评分: {status}",
            "",
            "【评分详情】",
            f"  人设一致性:    {_bar(scores.get('persona_match', '?'))}",
            f"  风格匹配度:    {_bar(scores.get('style_match', '?'))}",
            f"  规则遵循度:    {_bar(scores.get('rule_compliance', '?'))}",
            f"  旧人格泄露度:  {_bar(scores.get('old_persona_leak', '?'))}  (越低越好)",
            f"  旧记忆泄露度:  {_bar(scores.get('old_memory_leak', '?'))}  (越低越好)",
            f"  综合评分:      {_bar(overall)}",
            "",
        ]

        comment = scores.get("comment", "")
        if comment:
            lines.append(f"💬 评语: {comment}")
            lines.append("")

        if debug:
            prompt_preview = prompt[:300] + ("..." if len(prompt) > 300 else "")
            lines.append(f"📝 当前 Prompt 前300字:\n{prompt_preview}")
            lines.append("")

        lines.append("💡 提示: 可在插件设置中开启 debug_mode 查看详细重建日志。")
        lines.append("   配置 admin_users（QQ号逗号分隔）可授权其他管理员使用此指令。")

        return "\n".join(lines)
