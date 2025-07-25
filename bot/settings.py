from pydantic import BaseSettings
from dotenv import load_dotenv
import os

load_dotenv()  # вручную подгружаем переменные из .env

class Settings(BaseSettings):
    database_url: str

    class Config:
        env_file = ".env"

settings = Settings()