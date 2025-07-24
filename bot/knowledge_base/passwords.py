from typing import Dict, Optional

_pdf_passwords: Dict[str, str] = {}
_waiting_password_for_user: Dict[int, str] = {}

def store_pdf_password(filename: str, password: str):
    _pdf_passwords[filename] = password

def get_pdf_password(filename: str) -> Optional[str]:
    return _pdf_passwords.get(filename)

def set_awaiting_password(user_id: int, file_path: str):
    _waiting_password_for_user[user_id] = file_path

def get_awaiting_password_file(user_id: int) -> Optional[str]:
    return _waiting_password_for_user.get(user_id)

def clear_awaiting_password(user_id: int):
    _waiting_password_for_user.pop(user_id, None)
