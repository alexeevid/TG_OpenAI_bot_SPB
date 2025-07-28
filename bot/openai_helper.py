from typing import List, Dict, Any, Optional
from openai import OpenAI
import logging

logger = logging.getLogger(__name__)

class OpenAIHelper:
    def __init__(self, api_key: str, default_model: str):
        self.client = OpenAI(api_key=api_key)
        self.model = default_model

    def list_models(self) -> List[str]:
        try:
            models = self.client.models.list()
            names = [m.id for m in models.data]
            # Prefer sorted by 'gpt'
            names.sort()
            return names
        except Exception as e:
            logger.exception("Failed to list models: %s", e)
            # Fallback shortlist
            return ["gpt-4o", "gpt-4o-mini", "gpt-4.1-mini", "gpt-3.5-turbo"]

    def set_model(self, model: str):
        self.model = model

    def chat(self, messages: List[Dict[str, str]]) -> str:
        """Try Responses API first; fall back to chat.completions."""
        # Responses API
        try:
            resp = self.client.responses.create(model=self.model, input={"role": "user", "content": messages[-1]['content']})
            # Extract text
            for out in resp.output_text.split("\n\n"):
                if out.strip():
                    return resp.output_text
            return resp.output_text or ""
        except Exception:
            pass

        # Fallback: Chat Completions
        try:
            cc = self.client.chat.completions.create(model=self.model, messages=messages)
            return cc.choices[0].message.content or ""
        except Exception as e:
            logger.exception("OpenAI chat failed: %s", e)
            raise
