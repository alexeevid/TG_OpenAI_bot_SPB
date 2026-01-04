from openai import OpenAI

class ImageService:
    def __init__(self, api_key: str, image_model: str = "gpt-image-1"):
        self.client = OpenAI(api_key=api_key)
        self.image_model = image_model

    def generate(self, prompt: str, size: str = "1024x1024") -> str:
        res = self.client.images.generate(
            model=self.image_model,
            prompt=prompt,
            size=size,
        )
        return res.data[0].url
