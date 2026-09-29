"""Gemini adapter. No automatic retries: an uncertain request can still be billed."""
import base64
import json
import re
import urllib.error
import urllib.request
from pathlib import Path


class GenerationError(Exception):
    def __init__(self, message, usage=None):
        super().__init__(message)
        self.usage = usage


def usage_record(model, usage):
    """Standard-tier estimate, not a billing receipt. Never treat missing usage as free."""
    record = {"model": model, "tokens": usage, "estimated_usd": None,
              "price_date": "2026-09-26", "price_source": "https://ai.google.dev/gemini-api/docs/pricing#gemini-3.1-flash-image"}
    if model != "gemini-3.1-flash-image" or not usage:
        return record
    details = usage.get("candidatesTokensDetails")
    if details and "promptTokenCount" in usage and "candidatesTokenCount" in usage:
        images = sum(d.get("tokenCount", 0) for d in details if d.get("modality") == "IMAGE")
        other = usage["candidatesTokenCount"] - images + usage.get("thoughtsTokenCount", 0)
        if images >= 0 and other >= 0:
            record["estimated_usd"] = round((usage["promptTokenCount"] * .5 + images * 60 + other * 3) / 1_000_000, 8)
    return record


class GeminiProvider:
    def __init__(self, key, model="gemini-3.1-flash-image"):
        if not re.fullmatch(r"[a-zA-Z0-9.-]+", model):
            raise ValueError("Invalid model")
        self.key, self.model = key, model

    def __call__(self, person, garment):
        parts = [{"text": Path(__file__).with_name("prompt.txt").read_text()}]
        for blob in (person, garment):
            parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(blob).decode()}})
        payload = {"contents": [{"role": "user", "parts": parts}],
                   "generationConfig": {"responseModalities": ["TEXT", "IMAGE"], "candidateCount": 1,
                                        "imageConfig": {"imageSize": "1K"}}}
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models/" + self.model + ":generateContent",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "x-goog-api-key": self.key})
        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                raw = response.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024:
                raise GenerationError("Получен слишком большой результат. Попробуйте другое фото.")
            data = json.loads(raw)
        except urllib.error.HTTPError as error:
            messages = {429: "Сервис примерки занят или исчерпан лимит. Попробуйте позже.",
                        403: "Доступ к генерации недоступен. Сообщите владельцу приложения.",
                        400: "Сервис не принял эти фотографии. Попробуйте другие изображения."}
            raise GenerationError(messages.get(error.code, "Сервис примерки временно недоступен. Попробуйте позже.")) from None
        except (OSError, ValueError):
            raise GenerationError("Не удалось получить ответ. Запрос мог быть оплачен; автоматически мы его не повторяем.") from None
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                blob = part.get("inlineData")
                if blob and not part.get("thought") and blob.get("mimeType") in ("image/jpeg", "image/png", "image/webp"):
                    try:
                        return base64.b64decode(blob["data"], validate=True), usage_record(self.model, data.get("usageMetadata", {}))
                    except (ValueError, KeyError):
                        break
        raise GenerationError("Не удалось создать изображение. Попробуйте более чёткое фото человека и одной вещи.",
                              usage_record(self.model, data.get("usageMetadata", {})))
