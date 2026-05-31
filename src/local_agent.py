import trio
import json

from abc import ABC, abstractmethod

from src.model_defaults import DEFAULT_LOCAL_MODEL_ID, DEFAULT_OLLAMA_MODEL


class LocalAgent(ABC):
    @abstractmethod
    async def generate(self, prompt: str) -> str:
        """
        Generate an answer locally.
        """
        raise NotImplementedError


class DummyAgent(LocalAgent):
    async def generate(self, prompt: str) -> str:
        """
        Temporary placeholder for a real LLM.
        """
        if "Capability score JSON:" in prompt:
            return '{"general": 0.6}'
        return f"[LOCAL DUMMY ANSWER] I received: {prompt}"


class LocalTransformersAgent(LocalAgent):
    def __init__(
        self,
        model_id: str = DEFAULT_LOCAL_MODEL_ID,
        max_new_tokens: int = 512,
        system_prompt: str | None = None,
        temperature: float = 0.7,
        top_p: float = 0.8,
        top_k: int = 20,
        enable_thinking: bool = False,
        timeout_s: float = 40.0,
        tokenizer=None,
        model=None,
    ) -> None:
        self.model_id = model_id
        self.max_new_tokens = max_new_tokens
        self.system_prompt = system_prompt
        self.temperature = temperature
        self.top_p = top_p
        self.top_k = top_k
        self.enable_thinking = enable_thinking
        self.timeout_s = timeout_s

        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.tokenizer = tokenizer or AutoTokenizer.from_pretrained(model_id)
        self.model = model or AutoModelForCausalLM.from_pretrained(
            model_id,
            torch_dtype="auto",
            device_map="auto",
        )

    async def generate(self, prompt: str) -> str:
        try:
            with trio.fail_after(self.timeout_s):
                return await trio.to_thread.run_sync(
                    self._generate_sync,
                    prompt,
                    abandon_on_cancel=True,
                )
        except trio.TooSlowError as exc:
            raise RuntimeError(
                f"Local Transformers request timed out after {self.timeout_s:.0f}s "
                f"for model {self.model_id!r}."
            ) from exc

    def _generate_sync(self, prompt: str) -> str:
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": prompt})

        text = self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=self.enable_thinking,
        )

        inputs = self.tokenizer([text], return_tensors="pt").to(self.model.device)

        generation_kwargs = {
            "max_new_tokens": self.max_new_tokens,
            "do_sample": self.temperature > 0.0,
        }
        if self.temperature > 0.0:
            generation_kwargs.update(
                {
                    "temperature": self.temperature,
                    "top_p": self.top_p,
                    "top_k": self.top_k,
                }
            )

        outputs = self.model.generate(**inputs, **generation_kwargs)

        generated_ids = outputs[0][len(inputs.input_ids[0]) :]
        answer = self.tokenizer.decode(
            generated_ids,
            skip_special_tokens=True,
        )

        return answer.strip()


class OllamaAgent(LocalAgent):
    def __init__(
        self,
        model: str = DEFAULT_OLLAMA_MODEL,
        host: str = "http://localhost:11434",
        system_prompt: str | None = None,
        timeout_s: float = 300.0,
        num_predict: int = 512,
        temperature: float = 0.7,
        top_p: float = 0.8,
        think: bool = False,
        response_format: str | dict | None = None,
    ) -> None:
        from src.ollama_utils import ollama_generate_url

        self.model = model
        self.host = host.rstrip("/")
        self.url = ollama_generate_url(host)
        self.system_prompt = system_prompt
        self.timeout_s = timeout_s
        self.num_predict = num_predict
        self.temperature = temperature
        self.top_p = top_p
        self.think = think
        self.response_format = response_format

    async def generate(self, prompt: str) -> str:
        return await trio.to_thread.run_sync(self._generate_sync, prompt)

    def _generate_sync(self, prompt: str) -> str:
        import urllib.error
        import urllib.request

        from src.ollama_utils import OllamaError

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "think": self.think,
            "options": {
                "num_predict": self.num_predict,
                "temperature": self.temperature,
                "top_p": self.top_p,
            },
        }

        if self.system_prompt is not None:
            payload["system"] = self.system_prompt
        if self.response_format is not None:
            payload["format"] = self.response_format

        data = json.dumps(payload).encode("utf-8")

        request = urllib.request.Request(
            self.url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                body = response.read().decode("utf-8")
        except TimeoutError as exc:
            raise RuntimeError(
                f"Ollama request timed out after {self.timeout_s:.0f}s for "
                f"model {self.model!r} at {self.host}."
            ) from exc
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            detail = raw.strip() or str(exc)
            raise OllamaError(
                f"Ollama request failed for model {self.model!r} at {self.url}: "
                f"HTTP {exc.code}. {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise OllamaError(
                f"Failed to reach Ollama at {self.host}. "
                "Start Ollama with `ollama serve` or open the Ollama desktop app. "
                f"Details: {reason}"
            ) from exc

        try:
            result = json.loads(body)
        except json.JSONDecodeError as exc:
            raise OllamaError(
                f"Ollama returned invalid JSON for model {self.model!r}."
            ) from exc

        if "error" in result:
            error = str(result["error"])
            hint = ""
            if "not found" in error.lower() or "pull" in error.lower():
                hint = f" Run `ollama pull {self.model}`."
            raise OllamaError(f"Ollama error for model {self.model!r}: {error}.{hint}")

        answer = result.get("response", "")
        return answer.strip()


class AIAssAgent(LocalAgent):
    """
    OpenAI-compatible chat backend for the EPFL AIaaS service.
    """

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        system_prompt: str | None = None,
        timeout_s: float = 300.0,
        max_tokens: int = 512,
        temperature: float = 0.7,
        top_p: float = 0.8,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.system_prompt = system_prompt
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.top_p = top_p

    async def generate(self, prompt: str) -> str:
        try:
            with trio.fail_after(self.timeout_s):
                return await trio.to_thread.run_sync(
                    self._generate_sync,
                    prompt,
                    abandon_on_cancel=True,
                )
        except trio.TooSlowError as exc:
            raise RuntimeError(
                f"AIaaS request timed out after {self.timeout_s:.0f}s "
                f"for model {self.model!r} at {self.base_url}."
            ) from exc

    def _generate_sync(self, prompt: str) -> str:
        import urllib.error
        import urllib.request

        if not self.api_key:
            raise RuntimeError(
                "AIaaS API key is missing. Set AIASS_API_KEY or pass --aiass-api-key."
            )

        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
        }
        data = json.dumps(payload).encode("utf-8")

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                body = response.read().decode("utf-8")
        except TimeoutError as exc:
            raise RuntimeError(
                f"AIaaS request timed out after {self.timeout_s:.0f}s for "
                f"model {self.model!r} at {self.base_url}."
            ) from exc
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            detail = raw.strip() or str(exc)
            raise RuntimeError(
                f"AIaaS request failed for model {self.model!r} at "
                f"{self.base_url}: HTTP {exc.code}. {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise RuntimeError(
                f"Failed to reach AIaaS at {self.base_url}. Details: {reason}"
            ) from exc

        try:
            result = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("AIaaS returned invalid JSON.") from exc

        if "error" in result:
            raise RuntimeError(f"AIaaS error: {result['error']}")

        choices = result.get("choices") or []
        if not choices:
            raise RuntimeError("AIaaS returned no choices.")

        first = choices[0]
        message = first.get("message") if isinstance(first, dict) else None
        if isinstance(message, dict):
            return str(message.get("content") or "").strip()

        return str(first.get("text") or "").strip()
