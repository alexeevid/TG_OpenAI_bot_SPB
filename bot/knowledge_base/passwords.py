_pdf_passwords = {}
_wait = {}
def store_pdf_password(filename: str, password: str): _pdf_passwords[filename] = password
def get_pdf_password(filename: str): return _pdf_passwords.get(filename)
def set_awaiting_password(user_id: int, file_path: str): _wait[user_id] = file_path
def get_awaiting_password_file(user_id: int): return _wait.get(user_id)
def clear_awaiting_password(user_id: int): _wait.pop(user_id, None)
