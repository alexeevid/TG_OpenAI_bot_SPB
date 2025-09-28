
class ImageService:
    def __init__(self, openai_client, model: str):
        self._cli = openai_client
        self._model = model
    def generate(self, prompt: str) -> str:
        return self._cli.image(prompt, self._model)
