"""Stateless llama.cpp HTTP client; no retries and no proxy inheritance."""
import httpx
import threading
import time

class LocalClientError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

class LocalClient:
    def __init__(self, base_url: str, api_key: str):
        self.client = httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {api_key}"},
                                   timeout=httpx.Timeout(300), trust_env=False, follow_redirects=False)

    def health(self):
        response = self.client.get("/health")
        if response.status_code != 200:
            raise LocalClientError("local_runtime_not_ready")
        return response.json()

    def _request(self, method, path, *, json_body, cancelled=None):
        if cancelled is None:
            try: return self.client.request(method,path,json=json_body)
            except httpx.TimeoutException as exc: raise LocalClientError("local_inference_timeout") from exc
            except httpx.HTTPError as exc: raise LocalClientError("local_runtime_request_failed") from exc
        result={}; done=threading.Event()
        def send():
            try: result["response"]=self.client.request(method,path,json=json_body)
            except BaseException as exc: result["error"]=exc
            finally: done.set()
        threading.Thread(target=send,name="local-translation-http",daemon=True).start()
        deadline=time.monotonic()+300
        while not done.wait(.1):
            if cancelled():
                self.close()
                raise LocalClientError("local_job_cancelled")
            if time.monotonic()>=deadline:
                self.close()
                raise LocalClientError("local_inference_timeout")
        if "error" in result:
            exc=result["error"]
            if isinstance(exc,httpx.TimeoutException): raise LocalClientError("local_inference_timeout") from exc
            raise LocalClientError("local_runtime_request_failed") from exc
        return result["response"]

    def input_tokens(self, prompt: str, cancelled=None) -> int:
        response=self._request("POST","/v1/chat/completions/input_tokens",json_body={"messages":[{"role":"user","content":prompt}]},cancelled=cancelled)
        if response.is_redirect or response.status_code >= 400:
            raise LocalClientError("local_context_limit" if response.status_code in (400,413) else "local_runtime_request_failed")
        try:
            count = response.json()["input_tokens"]
            if type(count) is not int or count < 0:
                raise ValueError
            return count
        except (ValueError, KeyError, TypeError) as exc:
            raise LocalClientError("local_runtime_response_invalid") from exc

    def translate(self, prompt: str, max_tokens: int, cancelled=None) -> tuple[str, int, str | None]:
        response = self._request("POST","/v1/chat/completions",json_body={"messages":[{"role":"user","content":prompt}],
                                   "stream":False,"max_tokens":max_tokens},cancelled=cancelled)
        if response.is_redirect or response.status_code >= 400:
            raise LocalClientError("local_context_limit" if response.status_code in (400,413) else "local_runtime_request_failed")
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            usage = body.get("usage", {}).get("completion_tokens", 0)
            finish = body["choices"][0].get("finish_reason")
            if not isinstance(content, str) or type(usage) is not int:
                raise ValueError
            return content, usage, finish
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise LocalClientError("local_runtime_response_invalid") from exc

    def close(self):
        self.client.close()
