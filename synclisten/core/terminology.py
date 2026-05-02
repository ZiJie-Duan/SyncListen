# -*- coding: utf-8 -*-
"""易错词持久化存储。"""

import json
import os

DEFAULT_PATH = os.path.expanduser("~/.config/synclisten/terminology.json")


class TerminologyStore:
    """简单的 JSON 列表持久化，存放用户录入的易错词 / 替换规则。

    词条支持两种形式：
      - 直接给正确词（如 "SenseVoice"），AI 在润色/处理时会优先使用
      - "错->对" 显式替换规则（如 "传写->转写"）
    """

    def __init__(self, path=None):
        self.path = path or DEFAULT_PATH
        self._terms = self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [str(x) for x in data if str(x).strip()]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return []

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._terms, f, ensure_ascii=False, indent=2)

    def list(self):
        """返回当前所有易错词的副本。"""
        return list(self._terms)

    def add(self, term):
        """新增一个易错词。已存在则返回 False，新增成功返回 True。"""
        term = (term or "").strip()
        if not term:
            return False
        if term in self._terms:
            return False
        self._terms.append(term)
        self._save()
        return True

    def remove(self, index):
        """按 1-based 索引删除。返回被删除的词，越界返回 None。"""
        if not isinstance(index, int) or index < 1 or index > len(self._terms):
            return None
        removed = self._terms.pop(index - 1)
        self._save()
        return removed

    def as_string(self):
        """空格分隔的字符串形式，供 AI prompt / 替换函数使用。"""
        return " ".join(self._terms)
