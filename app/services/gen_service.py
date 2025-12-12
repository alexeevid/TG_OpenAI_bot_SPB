from ..dialog_manager import Dialog  # Исправленный относительный импорт
import openai

def generate_answer(prompt: str, dialog: Dialog) -> str:
    model = dialog.settings.get("model", "gpt-4")
    style = dialog.settings.get("style", "default")

    if style == "concise":
        system_prompt = "Отвечай кратко и по существу."
    elif style == "mcwilliams":
        system_prompt = "Отвечай как профессор МакВильямс: высокоумно и с иронией."
    else:
        system_prompt = "Ты — полезный помощник."

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    if dialog.settings.get("kb_enabled"):
        from ..rag_engine import retrieve_and_format
        rag_context = retrieve_and_format(prompt)
        messages.insert(1, {"role": "user", "content": rag_context})

    response = openai.ChatCompletion.create(
        model=model,
        messages=messages,
        temperature=0.7,
    )
    return response["choices"][0]["message"]["content"]
