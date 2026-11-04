import base64
import logging

import httpx

API_URL = "https://openrouter.ai/api/v1/chat/completions"

logger = logging.getLogger(__name__)


class LLMError(Exception):
    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class OpenRouterClient:
    def __init__(self, api_key: str, text_models: list[str], image_model: str) -> None:
        self.text_models = text_models
        self.image_model = image_model
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(180),
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Title": "AI Telegram Bot",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(self, payload: dict) -> dict:
        try:
            response = await self._client.post(API_URL, json=payload)
        except httpx.HTTPError as exc:
            raise LLMError("Не удалось связаться с OpenRouter. Проверьте интернет.") from exc

        if response.status_code == 401:
            raise LLMError("Неверный ключ OpenRouter (OPENROUTER_API_KEY).")
        if response.status_code == 402:
            raise LLMError("На балансе OpenRouter недостаточно средств для этой модели.")
        if response.status_code == 429:
            raise LLMError("Модель перегружена. Подождите минуту и попробуйте снова.", retryable=True)
        if response.status_code == 404 or response.status_code >= 500:
            raise LLMError(f"Модель сейчас недоступна (ошибка {response.status_code}).", retryable=True)
        if response.status_code != 200:
            raise LLMError(f"OpenRouter вернул ошибку {response.status_code}: {response.text[:300]}")

        data = response.json()
        if "error" in data:
            raise LLMError(f"Ошибка модели: {data['error'].get('message', data['error'])}", retryable=True)
        if not data.get("choices"):
            raise LLMError("Модель вернула пустой ответ.", retryable=True)
        return data

    async def chat(self, messages: list[dict]) -> str:
        """Пробует модели по очереди и переключается на запасную, если текущая перегружена или недоступна."""
        last_error = LLMError("Не задано ни одной текстовой модели.")
        for model in self.text_models:
            try:
                data = await self._post({"model": model, "messages": messages})
            except LLMError as exc:
                if not exc.retryable:
                    raise
                logger.warning("Модель %s не ответила (%s), пробую следующую", model, exc)
                last_error = exc
                continue

            reply = (data["choices"][0]["message"].get("content") or "").strip()
            if reply:
                return reply
            logger.warning("Модель %s вернула пустой ответ, пробую следующую", model)
            last_error = LLMError("Модель вернула пустой ответ.")

        raise LLMError(f"Все модели сейчас заняты, попробуйте через минуту. Последняя ошибка: {last_error}")

    async def generate_image(self, prompt: str) -> bytes:
        data = await self._post(
            {
                "model": self.image_model,
                "messages": [{"role": "user", "content": prompt}],
                "modalities": ["image", "text"],
            }
        )
        message = data["choices"][0]["message"]
        images = message.get("images") or []
        if not images:
            text = (message.get("content") or "").strip()
            raise LLMError("Модель не вернула картинку." + (f" Ответ: {text[:300]}" if text else ""))

        url = images[0]["image_url"]["url"]
        if url.startswith("data:"):
            return base64.b64decode(url.split(",", 1)[1])

        try:
            image_response = await self._client.get(url)
            image_response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError("Не удалось скачать сгенерированную картинку.") from exc
        return image_response.content
