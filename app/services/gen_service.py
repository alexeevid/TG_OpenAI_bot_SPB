from __future__ import annotations
from typing import Optional, List, Dict, Any
from openai import OpenAI

class GenAnswer:
    def __init__(self, text: str):
        self.text = text

class GenService:
    def __init__(self, api_key: str, default_model: str = "gpt-4o-mini"):
        self.client = OpenAI(api_key=api_key)
        self.default_model = default_model

    def _build_messages(
        self,
        user_msg: str,
        history: List[Dict[str, str]] | None,
        system_prompt: str | None
    ) -> List[Dict[str, str]]:
        msgs: List[Dict[str, str]] = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        if history:
            msgs.extend(history)  # [{'role':'user'|'assistant','content':...}]
        msgs.append({"role":"user","content":user_msg})
        return msgs

    async def chat(
        self,
        *,
        user_msg: str,
        dialog_id: int,
        history: List[Dict[str, str]] | None = None,
        model: Optional[str] = None,
        style: Optional[str] = None,
        temperature: float = 0.7
    ) -> GenAnswer:
        # простая карта режимов
        system_map = {
            "concise": "Отвечай кратко, по делу, пунктами.",
            "detailed": "Отвечай подробно, со структурой и примерами.",
            "mcwilliams": "Пиши в стиле Нэнси МакВильямс: ясность, точность формулировок, избегай фиолетистости.",
        }
        system_prompt = system_map.get((style or "").lower(), "")

        model_name = model or self.default_model
        messages = self._build_messages(user_msg, history, system_prompt)

        resp = self.client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=temperature,
        )
        text = (resp.choices[0].message.content or "").strip()
        return GenAnswer(text=text)
