from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from .session import Base

class Document(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True)
    path = Column(String, unique=True, nullable=False)
    size = Column(Integer, nullable=False)
    sha256 = Column(String, nullable=True)
    password_required = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Document {self.path}>"
