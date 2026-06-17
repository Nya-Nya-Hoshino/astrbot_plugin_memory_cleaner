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
                        content = item.get("content", "")
                        if role in ("user", "assistant"):
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
        /清洗记忆 —— 主指令
        清除记忆 → 重载 Prompt → 重建人格 → 理解度检测 → 评分报告
        """
        # ---- 第0步：权限校验 ----
        if not self._is_authorized(event):
            yield event.plain_result("⛔ 权限不足。只有授权的管理员才能执行 /清洗记忆。\n请在插件配置中设置 admin_users（QQ号，逗号分隔）。")
            return

        user_name = event.get_sender_name()
        umo = event.unified_msg_origin
        debug = self.debug

        yield event.plain_result(f"🧹 {user_name} 发起了记忆清洗...")

        log_lines = [f"=== 记忆清洗日志 | 操作用户: {user_name} ==="]
        def log(msg):
            logger.info(f"[memory_cleaner] {msg}")
            log_lines.append(msg)

        try:
            # ---- 第1步：停止活跃 Agent ----
            log("[1/7] 停止活跃 Agent...")
            if _HAS_ACTIVE_REGISTRY:
                try:
                    active_event_registry.stop_all(umo, exclude=event)
                    log("  ✓ Agent 已停止")
                except Exception as e:
                    log(f"  ⚠ Agent 停止异常（可能无活跃任务）: {e}")
            else:
                log("  ⚠ 跳过（模块不可用）")

            # ---- 第2步：读取当前人格 Prompt ----
            log("[2/7] 读取当前人格 Prompt...")
            system_prompt = await self._get_persona_prompt(event)
            if not system_prompt:
                log("  ⚠ 未读取到有效 Prompt！请检查 AstrBot 人格配置")
                yield event.plain_result("⚠️ 未读取到当前人格 Prompt！请检查 AstrBot 的 Persona 配置是否正确设置。")
                return

            prompt_preview = system_prompt[:200] + ("..." if len(system_prompt) > 200 else "")
            log(f"  ✓ Prompt 长度: {len(system_prompt)} 字符")
            log(f"  📝 Prompt 预览: {prompt_preview}")
            if debug:
                yield event.plain_result(f"📝 当前 Prompt 预览:\n{prompt_preview}")

            # ---- 第3步：清除会话历史 ----
            log("[3/7] 清除当前会话所有对话历史...")
            conv_mgr = self.context.conversation_manager
            try:
                await conv_mgr.delete_conversations_by_user_id(umo)
                log("  ✓ 所有对话已删除")
            except Exception as e:
                log(f"  ⚠ 批量删除失败，尝试清空当前对话: {e}")
                try:
                    cid = await conv_mgr.get_curr_conversation_id(umo)
                    if cid:
                        await conv_mgr.update_conversation(umo, cid, [])
                        log("  ✓ 当前对话已清空")
                except Exception as e2:
                    log(f"  ⚠ 清空当前对话也失败: {e2}")
            yield event.plain_result("🗑️ 对话历史已清除")

            # ---- 第4步：刷新 Persona 缓存 ----
            log("[4/7] 刷新 Persona 缓存...")
            try:
                persona_mgr = self.context.persona_manager
                persona_mgr.get_v3_persona_data()  # 刷新内存缓存
                log("  ✓ Persona 缓存已刷新")
            except Exception as e:
                log(f"  ⚠ Persona 缓存刷新异常: {e}")

            # ---- 第5步：人格重建 ----
            log("[5/7] 执行人格重建（让 LLM 深度内化新 Prompt）...")
            rebuild_prompt = (
                "你刚刚经历了一次记忆重置。现在，请你仔细阅读并完全内化以下系统设定，"
                "这将成为你唯一的行为准则。请用一段话（50-100字）总结你理解的人设：\n\n"
                f"=== 系统设定 ===\n{system_prompt}\n=== 设定结束 ==="
            )
            rebuild_result = await self._llm_check("你是一个认真负责的助手。", rebuild_prompt)
            log(f"  ✓ 人格重建完成")
            if debug:
                yield event.plain_result(f"🔄 人格重建结果:\n{rebuild_result[:300]}")

            # ---- 第6步：理解度检测 ----
            log("[6/7] 执行 Prompt 理解度检测...")
            check_prompt = (
                "你是一位严格的 Prompt 合规性审计员。下面会提供一份「系统设定」和一份「助手自述」。\n"
                "请判断助手是否真正理解并遵循了系统设定。请按以下维度以 JSON 格式评分：\n\n"
                "1. persona_match (0-100)：人设一致性 —— 助手自述中的人设是否完全匹配系统设定的角色/身份描述\n"
                "2. style_match (0-100)：风格一致性 —— 助手的语气、措辞风格是否符合系统设定要求\n"
                "3. rule_compliance (0-100)：规则遵循度 —— 助手是否理解并遵循了系统设定中的所有规则和限制\n"
                "4. memory_pollution (0-100)：记忆污染度 —— 是否存在与系统设定无关或冲突的残留信息（0=完全无污染，100=严重污染）\n"
                "5. overall (0-100)：综合评分 —— 整体表现\n\n"
                f"=== 系统设定 ===\n{system_prompt}\n=== 设定结束 ===\n\n"
                f"=== 助手自述 ===\n{rebuild_result}\n=== 自述结束 ===\n\n"
                "请仅输出如下格式的 JSON（不要带 markdown 代码块标记）：\n"
                '{"persona_match": 分数, "style_match": 分数, "rule_compliance": 分数, "memory_pollution": 分数, "overall": 分数, "comment": "一段简短的中文评语"}'
            )
            check_result = await self._llm_check(
                "你是一个严格但公正的审计员。只输出 JSON。",
                check_prompt
            )
            log(f"  📊 检测原始结果: {check_result[:500]}")

            # ---- 第7步：解析评分并生成报告 ----
            log("[7/7] 解析评分并生成报告...")
            try:
                scores = self._parse_scores(check_result)
            except Exception as e:
                log(f"  ⚠ 评分解析失败: {e}")
                scores = {
                    "persona_match": "?",
                    "style_match": "?",
                    "rule_compliance": "?",
                    "memory_pollution": "?",
                    "overall": "?",
                    "comment": f"评分解析失败，原始返回: {check_result[:200]}"
                }

            report = self._build_report(user_name, system_prompt, scores, debug)
            log(f"  ✓ 报告已生成")

            # ---- 输出最终报告 ----
            async for chunk in self._send_long_msg(event, report):
                yield chunk

            if debug:
                log_lines.insert(0, "")  # 空行分隔
                full_log = "\n".join(log_lines)
                async for chunk in self._send_long_msg(event, f"📋 调试日志:\n{full_log}"):
                    yield chunk

            log("=== 清洗完成 ===")

        except Exception as e:
            logger.error(f"[memory_cleaner] 清洗过程异常: {e}", exc_info=True)
            yield event.plain_result(f"❌ 记忆清洗过程中发生错误: {e}\nAstrBot 运行不受影响，请检查日志排查问题。")

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
        expected_keys = ["persona_match", "style_match", "rule_compliance", "memory_pollution", "overall"]
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
            f"  记忆污染度:    {_bar(scores.get('memory_pollution', '?'))}  (越低越好)",
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

        lines.append("💡 提示: 可在插件设置中开启 debug_mode 查看详细分析过程。")
        lines.append("   配置 admin_users（QQ号逗号分隔）可授权其他管理员使用此指令。")

        return "\n".join(lines)
