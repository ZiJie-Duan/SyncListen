# -*- coding: utf-8 -*-
"""AI 大模型客户端，支持 OpenAI / DeepSeek 等兼容 API。"""

import os
from openai import OpenAI

from ..config import AI_API_KEY, AI_BASE_URL, AI_MODEL


class AIClient:
    """封装 OpenAI 兼容 API 的客户端。"""

    def __init__(self, api_key=None, base_url=None, model=None):
        self.api_key = api_key or AI_API_KEY
        self.base_url = base_url or AI_BASE_URL
        self.model = model or AI_MODEL

        if not self.api_key:
            raise ValueError(
                "AI API Key 未设置。请设置环境变量 OPENAI_API_KEY，"
                "或在 .env / config.py 中配置。"
            )

        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def chat(self, system_prompt, user_prompt, temperature=0.7):
        """发送聊天请求。"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
        )

        return response.choices[0].message.content

    def process_document(self, document_text, instruction):
        """对文档内容执行 AI 指令，直接返回处理结果。"""
        if document_text:
            system_prompt = (
                "你正在根据用户的要求处理用户的文字信息"
                "用户会给你一段文本信息和一个处理要求。"
                "请严格按照用户的要求处理文本，只输出处理后的结果，不要添加解释。"
                "请确保输出内容是完整的，不允许缩写，不允许简化任何细节" 
            )
            user_prompt = (
                f"【文本信息】\n{document_text}\n\n"
                f"【处理要求】\n{instruction}\n\n"
                f" 处理后的结果："
            )
        else:
            # 没有现有内容时，直接根据指令生成
            system_prompt = (
                "请根据用户的要求直接生成内容，只输出结果，不要添加解释。"
            )
            user_prompt = (
                f"【要求】\n{instruction}\n\n"
                f"请直接输出结果："
            )

        return self.chat(system_prompt, user_prompt)

    def incremental_notes(self, existing_content, new_input):
        """增量式笔记：将已有内容与新输入合并提炼。

        Args:
            existing_content: 当前已有笔记内容
            new_input: 新转写的语音内容

        Returns:
            合并后的完整笔记
        """
        system_prompt = (
            "你是一位笔记整理专家。用户已有笔记内容，现在通过语音补充了新信息。\n"
            "请将【已有笔记】和【新输入】合并整理为一份完整、简洁、结构化的笔记。\n"
            "去除重复和冗余信息，保留核心事实、观点、待办事项。\n"
            "只输出最终笔记，不要加解释。"
        )

        if existing_content:
            user_prompt = (
                f"【已有笔记】\n{existing_content}\n\n"
                f"【新输入】\n{new_input}\n\n"
                f"请输出合并后的完整笔记："
            )
        else:
            user_prompt = (
                f"【新输入】\n{new_input}\n\n"
                f"请整理为简洁的笔记："
            )

        return self.chat(system_prompt, user_prompt, temperature=0.3)

    def polish_text(self, raw_text):
        """润色转写内容：去语病、去重复、口语转书面、保留细节。

        Args:
            raw_text: 语音转写的原始文字

        Returns:
            润色后的书面文字
        """
        system_prompt = (
            "你是一位文字润色专家。用户的话是语音转写结果，包含大量口语杂质。\n"
            "请将其整理为流畅的书面文字，要求：\n"
            "1. 去除语病、口癖（'然后'、'嗯'、'那个'等填充词）\n"
            "2. 去除高度重复的句子和表达\n"
            "3. 把口语化表达转为规范书面语\n"
            "4. 强化重点，让表达更精炼、有逻辑\n"
            "5. 适当分段和格式化（列表、标题等）\n"
            "6. 保留所有细节和信息，不要省略任何实质性内容\n"
            "只输出润色后的结果，不要加解释。"
        )

        user_prompt = f"请润色以下内容：\n\n{raw_text}"
        return self.chat(system_prompt, user_prompt, temperature=0.3)
