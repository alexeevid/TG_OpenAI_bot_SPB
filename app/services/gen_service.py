from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from ..clients.openai_client import OpenAIClient

log = logging.getLogger(__name__)


class GenService:
    """Сервис генерации текста/изображений/транскрибации.

    Принцип:
    - синхронные вызовы OpenAI SDK выполняются в threadpool (asyncio.to_thread)
    - модель выбирается из настроек диалога (settings JSON) по модальностям:
        - text_model
        - image_model
        - transcribe_model
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: str = "gpt-5.2",
        temperature: float = 0.2,
        max_output_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        image_model: str = "gpt-image-1",
        transcribe_model: str = "whisper-1",
    ):
        self.client = OpenAIClient(api_key=api_key)
        self.default_model = default_model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.image_model = image_model
        self.transcribe_model = transcribe_model

    async def list_models(self) -> List[str]:
        return await asyncio.to_thread(self.client.list_models)

    async def list_models_by_kind(self, kind: str) -> List[str]:
        return await asyncio.to_thread(self.client.list_models_by_kind, kind)

    def _rank_models(self, models: List[str]) -> List[str]:
        preferred = [
            "gpt-5.2-pro",
            "gpt-5.2",
            "gpt-5.1",
            "gpt-5",
            "gpt-4.1",
            "gpt-4o",
            "gpt-4o-mini",
        ]
        s = set(models)
        ordered = [m for m in preferred if m in s]
        rest = sorted([m for m in models if m not in set(ordered)])
        return ordered + rest

    async def selectable_models(self, limit: int = 12) -> List[str]:
        """
        Список выбираемых TEXT моделей для UI.
        Используем central filtering из OpenAIClient.
        """
        try:
            filtered = await self.list_models_by_kind("text")
            ranked = self._rank_models(filtered)
            if ranked:
                return ranked[:limit]
        except Exception as e:
            log.warning("selectable_models(): failed to list models: %s", e)

        # Safe fallback (UI-only list)
        return ["gpt-5.2", "gpt-5.2-pro", "gpt-4o", "gpt-4o-mini"]

    def _pick_from_dialog_settings(self, dialog_settings: Optional[Dict[str, Any]], key: str, fallback: str) -> str:
        if dialog_settings and isinstance(dialog_settings, dict):
            v = dialog_settings.get(key)
            if v:
                return str(v)
        return fallback

    async def chat(
        self,
        user_msg: str,
        history: Optional[List[Dict[str, str]]] = None,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        dialog_settings: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        dialog_settings: settings активного диалога (JSON dict).
        Если model не передан — используем dialog_settings["text_model"].
        """
        messages: List[Dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if history:
            for m in history:
                r = m.get("role")
                c = m.get("content")
                if r and c is not None:
                    messages.append({"role": str(r), "content": str(c)})
        messages.append({"role": "user", "content": user_msg})

        # 1) choose model (explicit param > dialog setting > service default)
        desired_model = (
            model
            or self._pick_from_dialog_settings(dialog_settings, "text_model", self.default_model)
        )

        use_temp = self.temperature if temperature is None else float(temperature)

        # 2) ensure availability (soft)
        safe_model = await asyncio.to_thread(
            self.client.ensure_model_available,
            model=desired_model,
            kind="text",
            fallback=self.default_model,
        )

        try:
            return await asyncio.to_thread(
                self.client.generate_text,
                model=safe_model,
                messages=messages,
                temperature=use_temp,
                max_output_tokens=self.max_output_tokens,
                reasoning_effort=self.reasoning_effort,
            )
        except Exception as e:
            log.exception("OpenAI generate_text failed (model=%s): %s", safe_model, e)

            # Fallback strategy:
            # - if chosen model differs from a known stable fallback, try that
            fallback_model = "gpt-4o"
            if safe_model != fallback_model:
                try:
                    txt = await asyncio.to_thread(
                        self.client.generate_text,
                        model=fallback_model,
                        messages=messages,
                        temperature=use_temp,
                        max_output_tokens=self.max_output_tokens,
                        reasoning_effort=self.reasoning_effort,
                    )
                    return (
                        f"⚠️ Выбранная модель `{desired_model}` недоступна или вернула ошибку. "
                        f"Переключился на `{fallback_model}`.\n\n{txt}"
                    )
                except Exception as e2:
                    log.exception("Fallback model also failed: %s", e2)
            raise

    async def image(
        self,
        prompt: str,
        model: Optional[str] = None,
        size: str = "1024x1024",
        dialog_settings: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        dialog_settings: settings активного диалога.
        Если model не передан — используем dialog_settings["image_model"].
        """
        desired_model = (
            model
            or self._pick_from_dialog_settings(dialog_settings, "image_model", self.image_model)
        )

        safe_model = await asyncio.to_thread(
            self.client.ensure_model_available,
            model=desired_model,
            kind="image",
            fallback=self.image_model,
        )

        return await asyncio.to_thread(self.client.generate_image_url, model=safe_model, prompt=prompt, size=size)

    async def transcribe_path(
        self,
        file_path: str,
        model: Optional[str] = None,
        dialog_settings: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        dialog_settings: settings активного диалога.
        Если model не передан — используем dialog_settings["transcribe_model"].
        """
        desired_model = (
            model
            or self._pick_from_dialog_settings(dialog_settings, "transcribe_model", self.transcribe_model)
        )

        safe_model = await asyncio.to_thread(
            self.client.ensure_model_available,
            model=desired_model,
            kind="transcribe",
            fallback=self.transcribe_model,
        )

        return await asyncio.to_thread(self.client.transcribe_path, file_path=file_path, model=safe_model)
