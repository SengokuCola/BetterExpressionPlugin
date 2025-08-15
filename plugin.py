from __future__ import annotations

from typing import List, Tuple, Type, Optional
import re
import time
import io
import base64
import traceback
import json
from PIL import Image, ImageDraw, ImageFont

from src.plugin_system import (
    BasePlugin,
    register_plugin,
    BaseCommand,
    ComponentInfo,
    ConfigField,
    get_logger,
)
from src.common.database.database_model import Expression
from src.chat.express.expression_selector import ExpressionSelector
from src.plugin_system.apis import message_api, config_api
from src.plugin_system.apis import llm_api
from src.config.config import global_config

logger = get_logger("expression_manager_plugin")


# =============================
# 工具函数
# =============================

def _parse_stream_config_to_chat_id(stream_config_str: str) -> Optional[str]:
    """解析 'platform:id:type' 为 chat_id，如果已经是32位md5则直接返回。
    与 `ExpressionSelector._parse_stream_config_to_chat_id` 规则一致。
    """
    if not stream_config_str:
        return None
    candidate = stream_config_str.strip()
    # 已是32位md5
    if re.fullmatch(r"[a-f0-9]{32}", candidate):
        return candidate
    try:
        return ExpressionSelector._parse_stream_config_to_chat_id(candidate)  # type: ignore[attr-defined]
    except Exception:
        return None


def _format_ts(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except Exception:
        return str(ts)


# =============================
# 添加表达方式命令
# =============================
class AddExpressionCommand(BaseCommand):
    command_name = "add_expression"
    command_description = "添加表达方式：/expr add 情景 表达 [in <chat>] [w=<float>]"
    # 例：/expr add 对惊叹 我嘞个 in qq:941657197:group w=1.2
    command_pattern = r"^/(?:expr|express|表达)\s+add\s+(.+?)\s+(.+?)(?:\s+in\s+(\S+))?(?:\s+w=([0-9]+(?:\.[0-9]+)?))?$"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        text = self.message.raw_message or ""
        m = re.match(self.command_pattern, text, flags=re.IGNORECASE)
        if not m:
            msg = "用法：/expr add 情景 表达 [in <chat>] [w=<float>]\n例：/expr add 对惊叹 我嘞个 w=1.5"
            await self.send_text(msg)
            return False, msg, True

        situation = m.group(1).strip()
        style = m.group(2).strip()
        chat_spec = m.group(3) or ""
        weight_str = m.group(4) or "1.0"

        # 目标chat
        chat_id = _parse_stream_config_to_chat_id(chat_spec)
        if chat_id is None:
            if self.message.chat_stream and getattr(self.message.chat_stream, "stream_id", None):
                chat_id = self.message.chat_stream.stream_id
            else:
                msg = "找不到目标聊天ID，请在群内使用或指定 in <chat>"
                await self.send_text(msg)
                return False, msg, True

        try:
            initial_count = max(0.01, min(5.0, float(weight_str)))
        except Exception:
            initial_count = 1.0

        # 查重
        exists = (
            Expression.select()
            .where(
                (Expression.chat_id == chat_id)
                & (Expression.situation == situation)
                & (Expression.style == style)
            )
            .exists()
        )
        now_ts = time.time()
        if exists:
            # 已存在则略微增强权重
            obj = (
                Expression.select()
                .where(
                    (Expression.chat_id == chat_id)
                    & (Expression.situation == situation)
                    & (Expression.style == style)
                )
                .get()
            )
            obj.count = min(5.0, obj.count + 0.1)
            obj.last_active_time = now_ts
            if obj.create_date is None:
                obj.create_date = obj.last_active_time
            obj.save()
            msg = f"已存在相同表达，提升权重至 {obj.count:.2f}"
            await self.send_text(msg)
            return True, msg, True
        else:
            Expression.create(
                situation=situation,
                style=style,
                count=initial_count,
                last_active_time=now_ts,
                chat_id=chat_id,
                type="expression",  # 统一使用expression类型
                create_date=now_ts,
            )
            msg = "添加成功"
            await self.send_text(msg)
            return True, msg, True


# =============================
# 列举表达方式命令（分页）
# =============================
class ListExpressionsCommand(BaseCommand):
    command_name = "list_expressions"
    command_description = "列举表达方式：/expr list [in <chat>] [page=<n>] [size=<n>]"
    command_pattern = r"^/(?:expr|express|表达)\s+list(?:\s+in\s+(\S+))?(?:\s+page=(\d+))?(?:\s+size=(\d+))?$"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        text = self.message.raw_message or ""
        m = re.match(self.command_pattern, text, flags=re.IGNORECASE)
        if not m:
            msg = "用法：/expr list [in <chat>] [page=<n>] [size=<n>]"
            await self.send_text(msg)
            return False, msg, True

        chat_spec = m.group(1) or ""
        page = int(m.group(2) or "1")
        size = int(m.group(3) or "10")
        page = max(1, page)
        size = max(1, min(50, size))

        chat_id = _parse_stream_config_to_chat_id(chat_spec)
        if chat_id is None:
            if self.message.chat_stream and getattr(self.message.chat_stream, "stream_id", None):
                chat_id = self.message.chat_stream.stream_id
            else:
                msg = "找不到目标聊天ID，请在群内使用或指定 in <chat>"
                await self.send_text(msg)
                return False, msg, True

        query = Expression.select().where(Expression.chat_id == chat_id)

        total = query.count()
        total_pages = max(1, (total + size - 1) // size)
        page = min(page, total_pages)

        # 按权重、活跃时间降序
        exprs = (
            query.order_by(Expression.count.desc(), Expression.last_active_time.desc())
            .paginate(page, size)
        )

        lines: List[str] = [f"共{total}条，页{page}/{total_pages}"]
        for e in exprs:
            lines.append(
                f"id={getattr(e, 'id', 0)} {e.situation} -> {e.style} | w={e.count:.2f} | at={_format_ts(e.last_active_time)}"
            )

        if len(lines) == 1:
            lines.append("（无数据）")

        msg = "\n".join(lines)
        await self.send_text(content=msg,storage_message=False)
        return True, msg, True


# =============================
# 删除表达方式命令
# =============================
class DeleteExpressionCommand(BaseCommand):
    command_name = "delete_expression"
    command_description = "删除表达方式：/expr del <id> [in <chat>]"
    command_pattern = r"^/(?:expr|express|表达)\s+del\s+(\d+)(?:\s+in\s+(\S+))?$"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        text = self.message.raw_message or ""
        m = re.match(self.command_pattern, text, flags=re.IGNORECASE)
        if not m:
            msg = "用法：/expr del <id> [in <chat>]\n例：/expr del 123 或 /expr del 123 in qq:123:group"
            await self.send_text(msg)
            return False, msg, True

        id_str = m.group(1)
        chat_spec = m.group(2) or ""

        chat_id = _parse_stream_config_to_chat_id(chat_spec)
        if chat_id is None:
            if self.message.chat_stream and getattr(self.message.chat_stream, "stream_id", None):
                chat_id = self.message.chat_stream.stream_id
            else:
                msg = "找不到目标聊天ID，请在群内使用或指定 in <chat>"
                await self.send_text(msg)
                return False, msg, True

        # 按ID删除（ID是唯一的，不需要限制chat_id）
        try:
            expr = Expression.get(Expression.id == int(id_str))
            expr.delete_instance()
            msg = f"已删除 id={id_str}"
            await self.send_text(msg)
            return True, msg, True
        except Expression.DoesNotExist:
            msg = f"未找到ID为 {id_str} 的表达方式"
            await self.send_text(msg)
            return False, msg, True


# =============================
# 查看表达方式使用记录命令
# =============================
class ReviewExpressionsCommand(BaseCommand):
    command_name = "review_expressions"
    command_description = "查看表达方式使用记录：/expr review"
    command_pattern = r"^/(?:expr|express|表达)\s+review$"
    

    def _generate_expression_image(self, expressions_info: List[str]) -> str:
        """生成表达方式使用记录的图片，返回base64编码的字符串"""
        try:
            # 计算图片尺寸
            max_line_length = max(len(line) for line in expressions_info) if expressions_info else 50
            line_count = len(expressions_info)
            
            # 设置字体和边距
            font_size = 16
            line_height = 25
            margin = 20
            
            # 计算图片尺寸
            img_width = max_line_length * font_size + margin * 2
            img_height = line_count * line_height + margin * 2
            
            # 创建图片
            img = Image.new('RGB', (img_width, img_height), color='white')
            draw = ImageDraw.Draw(img)
            
            # 尝试加载字体，如果失败则使用默认字体
            font = None
            font_name = "未知"
            
            try:
                # 优先尝试中文字体
                font = ImageFont.truetype("simhei.ttf", font_size)  # 黑体
                font_name = "simhei.ttf (黑体)"
            except:
                try:
                    font = ImageFont.truetype("simsun.ttc", font_size)  # 宋体
                    font_name = "simsun.ttc (宋体)"
                except:
                    try:
                        font = ImageFont.truetype("msyh.ttc", font_size)  # 微软雅黑
                        font_name = "msyh.ttc (微软雅黑)"
                    except:
                        try:
                            # 尝试Windows系统字体路径
                            font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", font_size)
                            font_name = "C:/Windows/Fonts/simhei.ttf (黑体)"
                        except:
                            try:
                                font = ImageFont.truetype("C:/Windows/Fonts/simsun.ttc", font_size)
                                font_name = "C:/Windows/Fonts/simsun.ttc (宋体)"
                            except:
                                try:
                                    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", font_size)
                                    font_name = "C:/Windows/Fonts/msyh.ttc (微软雅黑)"
                                except:
                                    try:
                                        font = ImageFont.truetype("arial.ttf", font_size)  # Arial
                                        font_name = "arial.ttf (Arial)"
                                    except:
                                        try:
                                            font = ImageFont.truetype("DejaVuSans.ttf", font_size)  # DejaVuSans
                                            font_name = "DejaVuSans.ttf"
                                        except:
                                            font = ImageFont.load_default()  # 默认字体
                                            font_name = "默认字体"
            
            logger.info(f"成功加载字体: {font_name}")
            
            # 绘制文本
            y_position = margin
            for line in expressions_info:
                # 根据内容类型设置颜色
                if "消息[" in line:
                    color = (0, 0, 255)  # 蓝色
                elif "使用的表达:" in line:
                    color = (255, 0, 0)  # 红色
                elif "ID" in line and "->" in line:
                    color = (0, 128, 0)  # 绿色
                elif "---" in line:
                    color = (128, 128, 128)  # 灰色
                else:
                    color = (0, 0, 0)  # 黑色
                
                draw.text((margin, y_position), line, fill=color, font=font)
                y_position += line_height
            
            # 转换为base64编码
            img_byte_arr = io.BytesIO()
            img.save(img_byte_arr, format='PNG')
            img_byte_arr.seek(0)
            img_bytes = img_byte_arr.getvalue()
            img_byte_arr.close()
            
            # 转换为base64编码
            img_base64 = base64.b64encode(img_bytes).decode('utf-8')
            
            logger.info(f"成功生成图片，大小: {len(img_bytes)} 字节，base64长度: {len(img_base64)}")
            return img_base64
            
        except Exception as e:
            logger.error(f"生成图片时出错: {e}")
            # 如果图片生成失败，返回None
            return None

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        if not self.message.chat_stream:
            msg = "无法获取聊天流信息"
            await self.send_text(msg)
            return False, msg, True

        try:
            # 获取聊天ID
            chat_id = None
            if self.message.chat_stream and getattr(self.message.chat_stream, "stream_id", None):
                chat_id = self.message.chat_stream.stream_id
            else:
                msg = "无法获取聊天ID"
                await self.send_text(msg)
                return False, msg, True

            # 使用message_api获取最近10条消息
            recent_messages = message_api.get_recent_messages(
                chat_id=chat_id,
                hours=24.0,  # 最近24小时
                limit=15,    # 限制15条
                limit_mode="latest",  # 获取最新的
                filter_mai=False      # 不过滤bot消息，我们需要看到bot的消息
            )
            
            if not recent_messages:
                msg = "最近没有找到消息记录"
                await self.send_text(msg)
                return True, msg, True
            
            # 过滤出bot自己发送的消息
            bot_messages = []
            bot_qq = config_api.get_global_config("bot.qq_account", "")
            for msg_dict in recent_messages:
                # 检查是否是bot发送的消息
                user_id = msg_dict.get("user_id", "")
                if user_id == str(bot_qq):
                    bot_messages.append(msg_dict)
            
            if not bot_messages:
                msg = "最近15条消息中没有找到bot发送的消息"
                await self.send_text(content=msg,storage_message=False)
                return True, msg, True
            
            # 读取selected_expressions内容
            expressions_info = []
            for msg_dict in bot_messages:
                message_id = msg_dict.get("message_id", "")
                message_content = msg_dict.get("processed_plain_text", "")[:50]  # 限制长度
                if message_content:
                    message_content = message_content + "..." if len(msg_dict.get("processed_plain_text", "")) > 50 else message_content
                
                # 获取selected_expressions
                selected_expr = msg_dict.get("selected_expressions", "")
                
                if selected_expr:
                    expressions_info.append(f"消息[{message_id}]: {message_content}")
                    
                    # 将字符串转换为ID列表
                    try:
                        # 处理 "[62, 201, 386]" 格式
                        expr_ids = []
                        # 移除方括号和空格，然后按逗号分割
                        clean_str = selected_expr.strip('[]').replace(' ', '')
                        for part in clean_str.split(','):
                            if part.strip().isdigit():
                                expr_ids.append(int(part.strip()))
                        
                        if expr_ids:
                            expr_details = []
                            for expr_id in expr_ids:
                                try:
                                    # 根据ID查找表达方式
                                    expr = Expression.get(Expression.id == expr_id)
                                    expr_details.append(f"  ID {expr_id}: {expr.situation} -> {expr.style} (权重:{expr.count:.2f})")
                                except Expression.DoesNotExist:
                                    expr_details.append(f"  ID {expr_id}: 表达方式不存在")
                            
                            if expr_details:
                                expressions_info.append("使用的表达:")
                                expressions_info.extend(expr_details)
                        else:
                            expressions_info.append(f"使用的表达: {selected_expr} (无法解析ID)")
                    except Exception as e:
                        expressions_info.append(f"使用的表达: {selected_expr} (解析错误: {e})")
                    
                    expressions_info.append("---")
            
            if not expressions_info:
                msg = "没有找到表达方式使用记录"
                await self.send_text(content=msg,storage_message=False)
                return True, msg, True
            
            # 生成图片
            logger.info(f"开始生成图片，包含 {len(expressions_info)} 行信息")
            img_base64 = self._generate_expression_image(expressions_info)
            
            if img_base64:
                logger.info(f"图片生成成功，base64长度: {len(img_base64)}")
                try:
                    # 发送图片
                    logger.info("尝试发送图片...")
                    await self.send_image(image_base64=img_base64,storage_message=False)
                    msg = "已生成表达方式使用记录图片"
                    logger.info("图片发送成功")
                    return True, msg, True
                except Exception as img_error:
                    logger.error(f"发送图片失败: {img_error}")
                    # 如果图片发送失败，发送文本
                    result = "表达方式使用记录：\n" + "\n".join(expressions_info)
                    await self.send_text(content=result,storage_message=False)
                    msg = "图片发送失败，已发送文本版本"
                    return True, msg, True
            else:
                logger.warning("图片生成失败")
                # 如果图片生成失败，发送文本
                result = "表达方式使用记录：\n" + "\n".join(expressions_info)
                await self.send_text(content=result,storage_message=False)
                msg = "图片生成失败，已发送文本版本"
                return True, msg, True
            
        except Exception as e:
            logger.error(f"查看表达方式使用记录时出错: {e}")
            msg = f"查看表达方式使用记录时出错: {str(e)}"
            await self.send_text(content=msg,storage_message=False)
            return False, msg, True


# =============================
# 修改表达方式权重命令
# =============================
class ModifyExpressionWeightCommand(BaseCommand):
    command_name = "modify_expression_weight"
    command_description = "修改权重：/expr <id> <+/-数字> [in <chat>]"
    command_pattern = r"^/(?:expr|express|表达)\s+(\d+)\s+([+-][0-9]+(?:\.[0-9]+)?)(?:\s+in\s+(\S+))?$"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        text = self.message.raw_message or ""
        m = re.match(self.command_pattern, text, flags=re.IGNORECASE)
        if not m:
            msg = "用法：/expr <id> <+/-数字> [in <chat>]\n例：/expr 123 +0.5 或 /expr 123 -1.2"
            await self.send_text(content=msg,storage_message=False)
            return False, msg, True

        id_str = m.group(1)
        weight_change_str = m.group(2)
        chat_spec = m.group(3) or ""

        # 解析chat_id
        chat_id = _parse_stream_config_to_chat_id(chat_spec)
        if chat_id is None:
            if self.message.chat_stream and getattr(self.message.chat_stream, "stream_id", None):
                chat_id = self.message.chat_stream.stream_id
            else:
                msg = "找不到目标聊天ID，请在群内使用或指定 in <chat>"
                await self.send_text(content=msg,storage_message=False)
                return False, msg, True

        # 解析权重变化
        try:
            weight_change = float(weight_change_str)
        except Exception:
            msg = "权重变化值必须是数字"
            await self.send_text(content=msg,storage_message=False)
            return False, msg, True

        # 查找表达方式
        try:
            expr = Expression.get((Expression.id == int(id_str)) & (Expression.chat_id == chat_id))
        except Expression.DoesNotExist:
            msg = "未找到该ID的表达方式（或不属于目标chat）"
            await self.send_text(content=msg,storage_message=False)
            return False, msg, True

        # 计算新权重
        new_weight = expr.count + weight_change
        
        # 如果权重小于0，删除该表达方式
        if new_weight <= 0:
            expr.delete_instance()
            msg = f"权重降至 {new_weight:.2f}，已自动删除该表达方式"
            await self.send_text(msg)
            return True, msg, True
        
        # 限制权重范围在0.01到5.0之间
        new_weight = max(0.01, min(5.0, new_weight))
        
        # 更新权重
        expr.count = new_weight
        expr.last_active_time = time.time()
        if expr.create_date is None:
            expr.create_date = expr.last_active_time
        expr.save()
        
        msg = f"已更新 id={id_str} 的权重为 {new_weight:.2f}"
        await self.send_text(msg)
        return True, msg, True


# =============================
# 学习表达方式命令
# =============================
class LearnExpressionCommand(BaseCommand):
    command_name = "learn_expression"
    command_description = "学习表达方式：/expr learn [指导语]"
    command_pattern = r"^/(?:expr|express|表达)\s+learn(?:\s+(.+))?$"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        text = self.message.raw_message or ""
        m = re.match(self.command_pattern, text, flags=re.IGNORECASE)
        if not m:
            msg = "用法：/expr learn [指导语]\n例：/expr learn 让表达更自然\n例：/expr learn (不提供指导语时，将根据聊天上下文自动分析)"
            await self.send_text(content=msg,storage_message=False)
            return False, msg, True

        # 指导语是可选的，如果没有提供则为空字符串
        guidance = m.group(1).strip() if m.group(1) else ""

        try:
            # 获取当前聊天ID
            chat_id = self.message.chat_stream.stream_id if self.message.chat_stream else None
            if not chat_id:
                msg = "无法获取当前聊天ID"
                await self.send_text(content=msg,storage_message=False)
                return False, msg, True

            # 1. 获取最近一条自己发的消息
            bot_id = str(global_config.bot.qq_account)
            recent_bot_messages = message_api.get_recent_messages(
                chat_id=chat_id,
                hours=24.0,  # 获取最近24小时的消息
                limit=100,   # 限制100条消息
                limit_mode="latest",
                filter_mai=False  # 不过滤麦麦的消息，因为需要找到自己的消息
            )
            
            # 找到最新一条自己发的消息（即最后一条）
            bot_message = None
            for msg in reversed(recent_bot_messages):
                if msg.get("user_id") == bot_id:
                    bot_message = msg
                    break
            
            if not bot_message:
                msg = "未找到麦麦最近发送的消息"
                await self.send_text(content=msg,storage_message=False)
                return False, msg, True

            # 2. 使用/expr msg同样的逻辑，找到这条消息用到的表达方式
            message_content = bot_message.get("processed_plain_text", "")
            selected_expressions_str = bot_message.get("selected_expressions", "")
            
            if not message_content:
                msg = "麦麦最近的消息内容为空"
                await self.send_text(content=msg,storage_message=False)
                return False, msg, True

            # 解析selected_expressions字段
            expression_ids = []
            if selected_expressions_str:
                try:
                    # 处理 "[62, 201, 386]" 格式
                    clean_str = selected_expressions_str.strip('[]').replace(' ', '')
                    for part in clean_str.split(','):
                        if part.strip().isdigit():
                            expression_ids.append(int(part.strip()))
                except Exception as e:
                    logger.warning(f"解析selected_expressions失败: {e}, 原始值: {selected_expressions_str}")

            if not expression_ids:
                msg = f"麦麦最近的消息没有关联的表达方式\n消息内容: {message_content[:100]}{'...' if len(message_content) > 100 else ''}"
                await self.send_text(content=msg,storage_message=False)
                return True, msg, True

            # 根据ID查询表达方式详情
            expressions_details = []
            for expr_id in expression_ids:
                try:
                    expr = Expression.get(Expression.id == expr_id)
                    expressions_details.append({
                        "id": expr_id,
                        "situation": expr.situation,
                        "style": expr.style,
                        "count": expr.count
                    })
                except Expression.DoesNotExist:
                    expressions_details.append({
                        "id": expr_id,
                        "situation": "未知",
                        "style": "表达方式不存在",
                        "count": 0
                    })

            # 2. 使用/expr msg同样的逻辑，分析哪些表达方式实际被使用
            # 构建分析提示词
            analysis_prompt = self._build_analysis_prompt_for_learn(message_content, expressions_details)
            
            # 先获取可用的LLM模型
            available_models = llm_api.get_available_models()
            if not available_models:
                msg = "没有可用的LLM模型"
                await self.send_text(content=msg,storage_message=False)
                return False, msg, True
            
            # 优先使用utils模型，如果没有则尝试chat模型
            model_to_use = available_models.get("utils")
            if not model_to_use:
                model_to_use = available_models.get("chat")
            
            if not model_to_use:
                # 如果都没有，使用第一个可用模型
                first_model_name = list(available_models.keys())[0]
                model_to_use = available_models[first_model_name]
                logger.info(f"[LearnExpression] 使用备用模型: {first_model_name}")
            
            # 使用LLM分析哪些表达方式实际被使用
            success, analysis_response, reasoning, analysis_model_name = await llm_api.generate_with_model(
                prompt=analysis_prompt,
                model_config=model_to_use,
                request_type="expression_analysis"
            )
            
            logger.info(f"[LearnExpression] 表达方式分析提示词: {analysis_prompt}")
            logger.info(f"[LearnExpression] 表达方式分析响应: {analysis_response}")
            
            if not success:
                msg = f"表达方式分析失败: {analysis_response}"
                await self.send_text(content=msg,storage_message=False)
                return False, msg, True
            
            # 解析分析结果，获取实际使用的表达方式
            used_expressions = self._parse_analysis_response(analysis_response)
            if not used_expressions:
                msg = f"LLM分析结果显示没有表达方式被使用\n消息内容: {message_content[:100]}{'...' if len(message_content) > 100 else ''}"
                await self.send_text(content=msg,storage_message=False)
                return True, msg, True
            
            # 过滤出实际使用的表达方式
            actual_used_expressions = []
            for expr in expressions_details:
                if expr["id"] in used_expressions:
                    actual_used_expressions.append(expr)
            
            logger.info(f"[LearnExpression] 消息中实际使用的表达方式: {[e['id'] for e in actual_used_expressions]}")

            # 3. 组织LLM，根据上下文构建prompt
            # 以自己发的消息为中心，更早取10条，后面取5条，如果不足就取全部
            bot_message_time = bot_message.get("time", 0)
            
            # 获取更早的10条消息
            earlier_messages = message_api.get_messages_before_time_in_chat(
                chat_id=chat_id,
                timestamp=bot_message_time,
                limit=10,
                filter_mai=False
            )
            
            # 获取后面的5条消息
            later_messages = message_api.get_messages_by_time_in_chat(
                chat_id=chat_id,
                start_time=bot_message_time,
                end_time=time.time(),
                limit=5,
                limit_mode="earliest",
                filter_mai=False
            )
            
            # 构建上下文消息列表（按时间顺序）
            context_messages = earlier_messages + [bot_message] + later_messages
            
            # 构建消息文本
            context_text = message_api.build_readable_messages_to_str(
                messages=context_messages,
                replace_bot_name=True,
                merge_messages=False,
                timestamp_mode="relative",
                show_actions=False
            )

            # 构建学习提示词
            prompt = self._build_learning_prompt_v2(
                context_text, 
                bot_message, 
                actual_used_expressions, 
                guidance
            )

            # 使用LLM生成改进建议
            available_models = llm_api.get_available_models()
            if not available_models:
                msg = "没有可用的LLM模型"
                await self.send_text(content=msg,storage_message=False)
                return False, msg, True
            
            # 优先使用utils模型，如果没有则尝试chat模型
            model_to_use = available_models.get("utils")
            if not model_to_use:
                model_to_use = available_models.get("chat")
            
            if not model_to_use:
                # 如果都没有，使用第一个可用模型
                first_model_name = list(available_models.keys())[0]
                model_to_use = available_models[first_model_name]
                logger.info(f"[LearnExpression] 使用备用模型: {first_model_name}")
            
            success, response, reasoning, model_name = await llm_api.generate_with_model(
                prompt=prompt,
                model_config=model_to_use,
                request_type="expression_learning"
            )
            
            logger.info(f"[LearnExpression] 学习提示词: {prompt}")
            logger.info(f"[LearnExpression] 学习响应: {response}")

            if not success:
                msg = f"LLM生成失败: {response}"
                await self.send_text(content=msg,storage_message=False)
                return False, msg, True

            # 解析LLM响应并更新数据库
            updated_count = await self._parse_and_update_expressions_v2(response, chat_id)

            if updated_count > 0:
                # 获取更新详情并构建详细消息
                update_details = await self._get_update_details_v2(response, chat_id)
                guidance_info = f"指导语：{guidance}" if guidance else "指导语：根据聊天上下文自动分析"
                msg = f"学习完成！使用模型 {model_name}，成功更新了 {updated_count} 个表达方式\n\n{guidance_info}\n\n更新详情：\n{update_details}"
            else:
                guidance_info = f"指导语：{guidance}" if guidance else "指导语：根据聊天上下文自动分析"
                msg = f"学习完成！使用模型 {model_name}，没有找到需要更新的表达方式\n\n{guidance_info}\n\n麦麦最近的消息：{message_content[:100]}{'...' if len(message_content) > 100 else ''}\n\n关联的表达方式：\n"
                for expr in actual_used_expressions:
                    msg += f"• ID {expr['id']}:\n  表达内容: {expr['style']}\n  使用情景: {expr['situation']}\n  权重: {expr['count']:.2f}\n"
            
            await self.send_text(content=msg,storage_message=False)
            return True, msg, True

        except Exception as e:
            error_msg = f"学习表达方式时出错: {str(e)}"
            logger.error(f"[LearnExpression] {error_msg}")
            logger.error(f"[LearnExpression] 错误详情: {traceback.format_exc()}")
            await self.send_text(error_msg)
            return False, error_msg, True

    def _build_learning_prompt_v2(self, context_text: str, bot_message: dict, expressions_details: List[dict], guidance: str) -> str:
        """构建新的学习提示词"""
        # 如果没有提供指导语，使用默认的上下文分析指导语
        if not guidance or guidance.strip() == "":
            guidance = "根据聊天上下文，分析并优化表达方式的适用场合，使其更准确地描述使用场景"
        
        prompt = f"""请根据以下聊天记录和指导语，分析{global_config.bot.nickname}最近使用的表达方式，并决定是否需要优化其适用场合。

聊天上下文（以{global_config.bot.nickname}的消息为中心，前后各取几条消息）：
{context_text}

{global_config.bot.nickname}最近的消息：
时间：{time.strftime('%H:%M:%S', time.localtime(bot_message.get('time', 0)))}
内容：{bot_message.get('processed_plain_text', '')}

{global_config.bot.nickname}在这条消息中使用的表达方式：
{self._format_expressions_for_prompt(expressions_details)}

指导语：{guidance}

请根据{global_config.bot.nickname}在这条消息中使用的表达方式，和指导语和聊天上下文，决定是否需要优化表达方式的适用场合。

{{
    "expressions": [
        {{
            "id": 123,
            "situation": "当前使用情景",
            "new_situation": "优化后的使用情景",
            "reason": "调整原因"
        }}
    ]
}}

注意：
1. 要结合原来的情景，指导语和上下文综合调整适用情景
2. 只返回需要调整的表达方式
3. 如果表达方式很好，可以不包含在结果中
4. 必须返回有效的JSON格式，不要添加任何其他文字
5. 如果找不到需要调整的表达方式，返回 {{"expressions": []}}
6. 只修改使用情景（situation），不要修改表达内容（style）本身

强调：只返回JSON，不要有任何其他文字！"""

        return prompt

    def _format_expressions_for_prompt(self, expressions_details: List[dict]) -> str:
        """格式化表达方式详情用于提示词"""
        formatted = []
        for expr in expressions_details:
            formatted.append(f"• ID {expr['id']}: {expr['situation']} → {expr['style']}")
        return "\n".join(formatted)

    def _build_analysis_prompt_for_learn(self, message_content: str, expressions_details: List[dict]) -> str:
        """构建用于学习的表达方式分析提示词"""
        expressions_text = ""
        for expr in expressions_details:
            expressions_text += f"- ID {expr['id']}: 情景「{expr['situation']}」-> 表达具体内容「{expr['style']}」\n"

        prompt = f"""你是一个表达方式分析专家。请分析以下消息在生成时实际使用了哪些表达方式。

消息内容：
{message_content}

可能使用的表达方式列表：
{expressions_text}

请分析这条消息的生成过程中，实际使用了上述哪些表达方式。考虑表达具体内容是否与详细内容相符。

重要：你必须严格按照以下JSON格式返回，不要添加任何其他文字说明：

{{
    "used_expressions": [
        {{
            "id": 123,
            "reason": "详细的判断理由，说明为什么认为使用了这个表达方式"
        }}
    ]
}}

注意：
1. used_expressions包含实际使用的表达方式
2. 如果没有使用任何表达方式，used_expressions为空数组
3. **只返回JSON，不要有任何其他文字！**"""

        return prompt

    def _parse_analysis_response(self, response: str) -> List[int]:
        """解析分析响应，返回实际使用的表达方式ID列表"""
        try:
            from json_repair import repair_json
            import json
            
            # 清理响应文本
            cleaned_response = response.strip()
            start_idx = cleaned_response.find('{')
            end_idx = cleaned_response.rfind('}')
            
            if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
                logger.error(f"[LearnExpression] 无法找到有效的JSON结构: {response}")
                return []
            
            json_text = cleaned_response[start_idx:end_idx + 1]
            
            # 修复JSON格式
            try:
                fixed_json = repair_json(json_text)
                data = json.loads(fixed_json)
            except Exception:
                try:
                    data = json.loads(json_text)
                except Exception as e:
                    logger.error(f"[LearnExpression] JSON解析失败: {e}")
                    return []
            
            if "used_expressions" not in data or not isinstance(data["used_expressions"], list):
                logger.error(f"[LearnExpression] 响应格式错误，缺少used_expressions字段")
                return []
            
            # 提取使用的表达方式ID
            used_ids = []
            for expr_data in data["used_expressions"]:
                expr_id = expr_data.get("id")
                if expr_id and str(expr_id).isdigit():
                    used_ids.append(int(expr_id))
            
            logger.info(f"[LearnExpression] 解析到使用的表达方式ID: {used_ids}")
            return used_ids
            
        except Exception as e:
            logger.error(f"[LearnExpression] 解析分析响应失败: {e}")
            return []

    async def _parse_and_update_expressions_v2(self, llm_response: str, chat_id: str) -> int:
        """解析LLM响应并更新数据库（新版本）"""
        try:
            # 尝试修复JSON格式
            from json_repair import repair_json
            import json
            
            # 清理响应文本，移除可能的额外文字
            cleaned_response = llm_response.strip()
            
            # 尝试找到JSON开始和结束的位置
            start_idx = cleaned_response.find('{')
            end_idx = cleaned_response.rfind('}')
            
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                json_text = cleaned_response[start_idx:end_idx + 1]
                logger.debug(f"[LearnExpression] 提取的JSON文本: {json_text}")
            else:
                logger.error(f"[LearnExpression] 无法找到有效的JSON结构")
                logger.error(f"[LearnExpression] 原始响应: {llm_response}")
                return 0
            
            # 修复JSON格式
            try:
                fixed_json = repair_json(json_text)
                data = json.loads(fixed_json)
            except Exception as json_error:
                logger.error(f"[LearnExpression] JSON修复和解析失败: {json_error}")
                logger.error(f"[LearnExpression] 尝试直接解析原始JSON: {json_text}")
                try:
                    data = json.loads(json_text)
                except Exception as direct_error:
                    logger.error(f"[LearnExpression] 直接解析也失败: {direct_error}")
                    return 0
            
            updated_count = 0
            
            if "expressions" in data and isinstance(data["expressions"], list):
                for expr_data in data["expressions"]:
                    try:
                        expr_id = expr_data.get("id")
                        if not expr_id:
                            logger.warning(f"[LearnExpression] 表达方式数据缺少ID: {expr_data}")
                            continue
                            
                        # 查找表达方式（ID是唯一的，不需要限制chat_id）
                        try:
                            # 检查expr_id是否为数字
                            if not str(expr_id).isdigit():
                                logger.warning(f"[LearnExpression] 表达方式ID '{expr_id}' 不是有效的数字ID，跳过")
                                continue
                            
                            expr = Expression.get(Expression.id == int(expr_id))
                        except Expression.DoesNotExist:
                            logger.warning(f"[LearnExpression] 表达方式 {expr_id} 不存在，跳过")
                            continue
                        
                        # 保存原始值用于显示
                        old_situation = expr.situation
                        old_style = expr.style
                        old_weight = expr.count
                        
                        # 更新表达方式（只修改情景，不修改表达内容本身）
                        weight_change = 0.0
                        new_situation = None
                        if "new_situation" in expr_data and expr_data["new_situation"]:
                            # 如果情景有改变，权重自动+0.5
                            if expr_data["new_situation"] != old_situation:
                                new_situation = expr_data["new_situation"]
                                expr.situation = new_situation
                                weight_change = 0.5  # 新版本改为+0.5
                                logger.info(f"[LearnExpression] 更新表达方式 {expr_id} 的情景: {old_situation} → {expr.situation}，权重+0.5")
                        
                        # 应用权重变化
                        if weight_change > 0:
                            new_weight = expr.count + weight_change
                            new_weight = max(0.01, min(5.0, new_weight))
                            expr.count = new_weight
                        
                        expr.last_active_time = time.time()
                        expr.save()
                        updated_count += 1
                        
                        # 将更新信息存储到消息中，供后续显示详情使用
                        if not hasattr(self, '_update_info'):
                            self._update_info = []
                        
                        self._update_info.append({
                            'id': expr_id,
                            'old_situation': old_situation,
                            'new_situation': new_situation or old_situation,
                            'old_style': old_style,
                            'reason': expr_data.get('reason', '无原因'),
                            'weight': expr.count
                        })
                        
                        logger.info(f"[LearnExpression] 更新表达方式 {expr_id}: {expr_data.get('reason', '无原因')} - 新权重: {expr.count:.2f}")
                        
                    except Exception as e:
                        logger.error(f"[LearnExpression] 更新表达方式 {expr_data.get('id', 'unknown')} 失败: {e}")
                        continue
            
            return updated_count
            
        except Exception as e:
            logger.error(f"[LearnExpression] 解析LLM响应失败: {e}")
            logger.error(f"[LearnExpression] 原始响应: {llm_response}")
            return 0

    async def _get_update_details_v2(self, llm_response: str, chat_id: str) -> str:
        """获取表达方式更新的详细信息（新版本）"""
        try:
            # 使用存储的更新信息
            if not hasattr(self, '_update_info') or not self._update_info:
                return "没有找到更新信息"
            
            details = []
            for update_item in self._update_info:
                expr_id = update_item['id']
                old_situation = update_item['old_situation']
                new_situation = update_item['new_situation']
                old_style = update_item['old_style']
                reason = update_item['reason']
                weight = update_item['weight']
                
                # 检查是否有实际变化
                if new_situation and new_situation != old_situation:
                    details.append(f"• ID {expr_id}:\n  表达内容: {old_style}\n  原情景: {old_situation}\n  新情景: {new_situation}\n  原因: {reason}\n  权重: {weight:.2f}")
                else:
                    details.append(f"• ID {expr_id}:\n  表达内容: {old_style}\n  情景: {old_situation}\n  原因: {reason}")
            
            if not details:
                return "没有表达方式被更新"
            
            return "\n".join(details)
            
        except Exception as e:
            logger.error(f"[LearnExpression] 获取更新详情失败: {e}")
            return f"获取更新详情失败: {e}"


# =============================
# 分析消息表达方式命令
# =============================
class AnalyzeMessageExpressionCommand(BaseCommand):
    command_name = "analyze_message_expression"
    command_description = "分析消息表达方式：/expr msg <消息id>"
    command_pattern = r"^/(?:expr|express|表达)\s+msg\s+(.+)$"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        text = self.message.raw_message or ""
        m = re.match(self.command_pattern, text, flags=re.IGNORECASE)
        if not m:
            msg = "用法：/expr msg <消息id>\n例：/expr msg 12345"
            await self.send_text(msg)
            return False, msg, True

        message_id = m.group(1).strip()

        try:
            # 通过消息ID查询消息
            from src.common.database.database_model import Messages
            
            try:
                message_record = Messages.get(Messages.message_id == message_id)
            except Messages.DoesNotExist:
                msg = f"未找到消息ID为 {message_id} 的消息"
                await self.send_text(msg)
                return False, msg, True

            # 获取消息内容和表达方式
            message_content = message_record.processed_plain_text or ""
            selected_expressions_str = message_record.selected_expressions or ""
            
            if not message_content:
                msg = f"消息ID {message_id} 的内容为空"
                await self.send_text(msg)
                return False, msg, True

            # 解析selected_expressions字段
            expression_ids = []
            if selected_expressions_str:
                try:
                    # 处理 "[62, 201, 386]" 格式
                    clean_str = selected_expressions_str.strip('[]').replace(' ', '')
                    for part in clean_str.split(','):
                        if part.strip().isdigit():
                            expression_ids.append(int(part.strip()))
                except Exception as e:
                    logger.warning(f"解析selected_expressions失败: {e}, 原始值: {selected_expressions_str}")

            if not expression_ids:
                msg = f"消息ID {message_id} 没有关联的表达方式\n消息内容: {message_content[:100]}{'...' if len(message_content) > 100 else ''}"
                await self.send_text(msg)
                return True, msg, True

            # 根据ID查询表达方式详情
            expressions_details = []
            for expr_id in expression_ids:
                try:
                    expr = Expression.get(Expression.id == expr_id)
                    expressions_details.append({
                        "id": expr_id,
                        "situation": expr.situation,
                        "style": expr.style,
                        "count": expr.count
                    })
                except Expression.DoesNotExist:
                    expressions_details.append({
                        "id": expr_id,
                        "situation": "未知",
                        "style": "表达方式不存在",
                        "count": 0
                    })

            # 构建LLM分析prompt
            prompt = self._build_analysis_prompt(message_content, expressions_details)

            # 使用LLM进行分析
            available_models = llm_api.get_available_models()
            if not available_models:
                msg = "没有可用的LLM模型"
                await self.send_text(msg)
                return False, msg, True
            
            # 优先使用utils模型，如果没有则尝试chat模型
            model_to_use = available_models.get("utils")
            if not model_to_use:
                model_to_use = available_models.get("chat")
            
            if not model_to_use:
                # 如果都没有，使用第一个可用模型
                first_model_name = list(available_models.keys())[0]
                model_to_use = available_models[first_model_name]
                logger.info(f"[AnalyzeMessageExpression] 使用备用模型: {first_model_name}")
            
            success, response, reasoning, model_name = await llm_api.generate_with_model(
                prompt=prompt,
                model_config=model_to_use,
                request_type="expression_analysis"
            )

            if not success:
                msg = f"LLM分析失败: {response}"
                await self.send_text(msg)
                return False, msg, True

            # 解析LLM响应并展示结果
            try:
                analysis_result = json.loads(response)
                used_expressions = analysis_result.get("used_expressions", [])
                unused_expressions = analysis_result.get("unused_expressions", [])
                summary = analysis_result.get("summary", "")

                # 构建简洁的结果消息
                result_msg = f"消息ID: {message_id}\n"
                result_msg += f"内容: {message_content[:100]}{'...' if len(message_content) > 100 else ''}\n\n"
                
                # 使用的表达方式
                if used_expressions:
                    result_msg += f"✅ 使用的表达方式:\n"
                    for expr in used_expressions:
                        expr_id = expr.get("id")
                        confidence = expr.get("confidence", 0)
                        reason = expr.get("reason", "无判断理由")
                        
                        # 查找对应的表达方式详情
                        expr_detail = next((e for e in expressions_details if e['id'] == expr_id), None)
                        if expr_detail:
                            result_msg += f"• ID {expr_id}: {expr_detail['situation']} → {expr_detail['style']}\n"
                            result_msg += f"  理由: {reason}\n"
                        else:
                            result_msg += f"• ID {expr_id}: 表达方式不存在\n"
                            result_msg += f"  理由: {reason}\n"
                else:
                    result_msg += f"❌ 使用的表达方式: 无\n"
                
                # 未使用的表达方式
                if unused_expressions:
                    result_msg += f"\n❌ 未使用的表达方式:\n"
                    for expr in unused_expressions:
                        expr_id = expr.get("id")
                        reason = expr.get("reason", "无判断理由")
                        result_msg += f"• ID {expr_id}: {reason}\n"
                
                # 总结
                if summary:
                    result_msg += f"\n📋 总结: {summary}"

                await self.send_text(result_msg)
                return True, result_msg, True
            except json.JSONDecodeError:
                # 如果JSON解析失败，尝试修复JSON格式
                try:
                    from json_repair import repair_json
                    fixed_response = repair_json(response)
                    analysis_result = json.loads(fixed_response)
                    
                    # 使用修复后的JSON继续处理
                    used_expressions = analysis_result.get("used_expressions", [])
                    unused_expressions = analysis_result.get("unused_expressions", [])
                    summary = analysis_result.get("summary", "")
                    
                    result_msg = f"JSON格式已自动修复\n\n"
                    result_msg += f"消息ID: {message_id}\n"
                    result_msg += f"消息内容: {message_content[:100]}{'...' if len(message_content) > 100 else ''}\n\n"
                    result_msg += f"修复后的分析结果：\n"
                    
                    # 构建使用的表达方式字符串
                    used_expr_str = "无"
                    if used_expressions:
                        used_ids = [str(e.get("id", "?")) for e in used_expressions]
                        used_expr_str = ", ".join([f"ID {id}" for id in used_ids])
                    result_msg += f"使用的表达方式: {used_expr_str}\n"
                    
                    # 构建未使用的表达方式字符串
                    unused_expr_str = "无"
                    if unused_expressions:
                        unused_ids = [str(e.get("id", "?")) for e in unused_expressions]
                        unused_expr_str = ", ".join([f"ID {id}" for id in unused_ids])
                    result_msg += f"未使用的表达方式: {unused_expr_str}\n"
                    
                    if summary:
                        result_msg += f"总结: {summary}"
                    
                    await self.send_text(result_msg)
                    return True, result_msg, True
                    
                except Exception as repair_error:
                    msg = f"JSON解析失败\n\nLLM返回的响应格式不正确，无法解析。\n\n原始响应：\n{response[:300]}{'...' if len(response) > 300 else ''}\n\n错误信息：\n{str(repair_error)}"
                    await self.send_text(msg)
                    return False, msg, True

        except Exception as e:
            error_msg = f"分析消息表达方式时出错: {str(e)}"
            logger.error(f"[AnalyzeMessageExpression] {error_msg}")
            logger.error(f"[AnalyzeMessageExpression] 错误详情: {traceback.format_exc()}")
            await self.send_text(error_msg)
            return False, error_msg, True

    def _build_analysis_prompt(self, message_content: str, expressions_details: List[dict]) -> str:
        """构建分析提示词"""
        expressions_text = ""
        for expr in expressions_details:
            expressions_text += f"- ID {expr['id']}: 情景「{expr['situation']}」-> 表达「{expr['style']}」(权重:{expr['count']:.2f})\n"

        prompt = f"""你是一个表达方式分析专家。请分析以下消息在生成时可能使用了哪些表达方式。

消息内容：
{message_content}

可能使用的表达方式列表：
{expressions_text}

请分析这条消息的生成过程中，实际使用了上述哪些表达方式。考虑以下因素：
1. 消息的语气、风格是否与某个表达方式的「表达」部分匹配
2. 消息的使用场景是否与某个表达方式的「情景」部分匹配
3. 表达方式的权重（权重越高，被使用的可能性越大）

**重要：你必须严格按照以下JSON格式返回，不要添加任何其他文字说明：**

{{
    "used_expressions": [
        {{
            "id": 123,
            "confidence": 0.85,
            "reason": "详细的判断理由，说明为什么认为使用了这个表达方式"
        }}
    ],
    "unused_expressions": [
        {{
            "id": 456,
            "reason": "详细的判断理由，说明为什么认为没有使用这个表达方式"
        }}
    ],
    "summary": "对整条消息表达特点的简要总结"
}}

注意：
1. used_expressions包含实际使用的表达方式，confidence表示置信度(0-1)
2. unused_expressions包含没有使用的表达方式
3. 如果没有使用任何表达方式，used_expressions为空数组
4. **只返回JSON，不要有任何其他文字！**"""

        return prompt


# =============================
# 插件注册
# =============================


@register_plugin
class ExpressionManagerPlugin(BasePlugin):
    plugin_name: str = "expression_manager_plugin"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[str] = []
    config_file_name: str = "config.toml"

    config_section_descriptions = {
        "plugin": "插件基本信息",
        "list": "列表显示配置",
    }

    config_schema: dict = {
        "plugin": {
            "name": ConfigField(type=str, default="expression_manager_plugin", description="插件名称"),
            "version": ConfigField(type=str, default="1.0.0", description="插件版本"),
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
        },
        "list": {
            "page_size": ConfigField(type=int, default=10, description="默认每页大小"),
            "max_page_size": ConfigField(type=int, default=50, description="每页最大大小"),
        },
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        return [
            (AddExpressionCommand.get_command_info(), AddExpressionCommand),
            (ListExpressionsCommand.get_command_info(), ListExpressionsCommand),
            (DeleteExpressionCommand.get_command_info(), DeleteExpressionCommand),
            (ModifyExpressionWeightCommand.get_command_info(), ModifyExpressionWeightCommand),
            (ReviewExpressionsCommand.get_command_info(), ReviewExpressionsCommand),
            (LearnExpressionCommand.get_command_info(), LearnExpressionCommand),
            (AnalyzeMessageExpressionCommand.get_command_info(), AnalyzeMessageExpressionCommand),
        ]
