from app.services.openai_client import OpenAIClient

class GenService:
    def __init__(self):
        self.client = OpenAIClient()

    def chat(self, prompt: str, history: list, model: str, system: str):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.extend(history)
        messages.append({"role": "user", "content": prompt})
        return self.client.chat(messages, model=model)