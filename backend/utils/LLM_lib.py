import os
import json
import time
import random
import base64
import logging
import requests
from datetime import datetime, timezone
import PIL.Image
import mimetypes
from pathlib import Path
from openai import OpenAI, AsyncOpenAI
from googlesearch import search
import asyncio

def read_txt_file(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        txt_content = file.read()
    return txt_content

class LLM_lib:
    def __init__(
        self,
        key_file_path=None,
        max_tokens=600,
        temperature=0.85,
        server_port=None,
        request_timeout_s=20,
        max_retries=1,
        retry_backoff_s=0.35,
    ):
        self.key_file_path = key_file_path
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.server_port = server_port
        self.request_timeout_s = float(request_timeout_s)
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_s = max(0.0, float(retry_backoff_s))
        self.api_key_data = self._load_api_keys(key_file_path)
        self._deepseek_sync_client = None
        self._deepseek_async_client = None
        # Approximate per-1K-token pricing used for experiment accounting.
        self._pricing_per_1k = {
            "deepseek-chat": {"prompt": 0.00014, "completion": 0.00028},
            "gpt-4o-2024-11-20": {"prompt": 0.0025, "completion": 0.01},
            "gpt-4o-mini": {"prompt": 0.00015, "completion": 0.0006},
            "qwen2.5-72b-instruct": {"prompt": 0.0, "completion": 0.0},
        }

    def _get_deepseek_sync_client(self):
        if self._deepseek_sync_client is None:
            self._deepseek_sync_client = OpenAI(
                api_key=self.api_key_data['DeepSeek_Michale'],
                base_url="https://api.deepseek.com",
            )
        return self._deepseek_sync_client

    def _get_deepseek_async_client(self):
        if self._deepseek_async_client is None:
            self._deepseek_async_client = AsyncOpenAI(
                api_key=self.api_key_data['DeepSeek_Michale'],
                base_url="https://api.deepseek.com",
            )
        return self._deepseek_async_client

    def _load_api_keys(self, key_file_path):
        api_keys = {}

        candidate_paths = []
        if key_file_path:
            candidate_paths.append(Path(key_file_path).expanduser())

        env_path = os.getenv('CRAWL_API_KEYS_PATH')
        if env_path:
            candidate_paths.append(Path(env_path).expanduser())

        default_path = Path(__file__).resolve().parents[1] / 'app' / 'API_KEY.json'
        candidate_paths.append(default_path)

        for candidate in candidate_paths:
            if candidate.exists():
                try:
                    with candidate.open('r', encoding='utf-8') as f:
                        loaded = json.load(f)
                        if isinstance(loaded, dict):
                            api_keys.update(loaded)
                    break
                except Exception:
                    continue

        env_overlay = {
            'OPENAI_API_KEY': os.getenv('OPENAI_API_KEY'),
            'DeepSeek_Michale': os.getenv('DEEPSEEK_API_KEY'),
            'QWEN_API_KEY': os.getenv('QWEN_API_KEY'),
            'SEARCH_API_KEY': os.getenv('SEARCH_API_KEY'),
        }

        for key_name, value in env_overlay.items():
            if value:
                api_keys[key_name] = value

        return api_keys
            
    def get_response_with_deepseek_chat(
        self,
        sys_prompt,
        user_prompt,
        max_tokens=8000,
        temperature=1,
        messages=None,
        model_name="deepseek-chat",
    ):
        start_ts = time.perf_counter()
        
        client = self._get_deepseek_sync_client()
        
        sys_prompt = "You are a helpful assistant" if len(sys_prompt) == 0 else sys_prompt
        task_description = user_prompt
        if messages == None:
            messages=[
                {"role": "system", "content": "You are a helpful assistant"},
                {"role": "user", "content": task_description},
            ]
        
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                response = client.chat.completions.create(
                    model="deepseek-chat",
                    max_tokens=max_tokens,
                    messages=messages,
                    response_format={'type': 'json_object'},
                    timeout=self.request_timeout_s,
                )
                parsed = json.loads(response.choices[0].message.content)
                return parsed, self._build_meta(
                    provider="deepseek",
                    model=model_name,
                    response_obj=response,
                    latency_ms=(time.perf_counter() - start_ts) * 1000.0,
                    status="ok",
                )
            except Exception as error:
                last_error = error
                if attempt >= self.max_retries:
                    break
                sleep_for = self.retry_backoff_s * (2 ** attempt) + random.uniform(0.0, 0.2)
                time.sleep(sleep_for)
        raise last_error
    
    async def async_get_response_with_deepseek_chat(
        self,
        sys_prompt,
        user_prompt,
        max_tokens=4000,
        temperature=1,
        messages=None,
        model_name="deepseek-chat",
    ):
        start_ts = time.perf_counter()
        
        client = self._get_deepseek_async_client()
        
        sys_prompt = "You are a helpful assistant" if len(sys_prompt) == 0 else sys_prompt
        task_description = user_prompt
        if messages == None:
            messages=[
                {"role": "system", "content": "You are a helpful assistant"},
                {"role": "user", "content": task_description},
            ]

        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await client.chat.completions.create(
                    model="deepseek-chat",
                    max_tokens=max_tokens,
                    messages=messages,
                    response_format={'type': 'json_object'},
                    timeout=self.request_timeout_s,
                )
                parsed = json.loads(response.choices[0].message.content)
                return parsed, self._build_meta(
                    provider="deepseek",
                    model=model_name,
                    response_obj=response,
                    latency_ms=(time.perf_counter() - start_ts) * 1000.0,
                    status="ok",
                )
            except Exception as error:
                last_error = error
                if attempt >= self.max_retries:
                    break
                sleep_for = self.retry_backoff_s * (2 ** attempt) + random.uniform(0.0, 0.2)
                await asyncio.sleep(sleep_for)
        raise last_error
    
    def get_response_with_Qwen(self, sys_prompt, user_prompt, max_tokens, temperature, model_name):
        start_ts = time.perf_counter()

        client = OpenAI(
            # If the environment variable is not configured, replace the following line with: api_key="sk-xxx",
            api_key=self.api_key_data['QWEN_API_KEY'], 
            base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        )
        response = client.chat.completions.create(
            model="qwen2.5-72b-instruct", # Model list: https://www.alibabacloud.com/help/en/model-studio/getting-started/models
            messages=[
                    {
                    "role": "user",
                    "content": [
                        {
                        "type": "text",
                        "text": sys_prompt
                        }
                    ]
                    },
                    {
                    "role": "user",
                    "content": [
                        {
                        "type": "text",
                        "text": user_prompt
                        }
                    ]
                    }
                ]
            )
        parsed = json.loads(response.choices[0].message.content)
        return parsed, self._build_meta(
            provider="qwen",
            model=model_name,
            response_obj=response,
            latency_ms=(time.perf_counter() - start_ts) * 1000.0,
            status="ok",
        )
    
    def get_response_with_gpt4(self, sys_prompt, user_prompt, max_tokens, temperature, model_name):
        start_ts = time.perf_counter()
        
        headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key_data['OPENAI_API_KEY']}"
            }
            
        payload = {
            "model": model_name,
            "messages": [
            {
                "role": "system",
                "content": [
                        {
                            "type":"text",
                            "text": sys_prompt
                        }
                    ]
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": user_prompt
                    }
                ]
            }
            ],
            "max_tokens": max_tokens,
            "temperature": temperature
        }
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                response = requests.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=self.request_timeout_s,
                )
                response.raise_for_status()
                body = response.json()
                parsed = json.loads(body['choices'][0]['message']['content'])
                return parsed, self._build_meta(
                    provider="openai",
                    model=model_name,
                    response_obj=body,
                    latency_ms=(time.perf_counter() - start_ts) * 1000.0,
                    status="ok",
                )
            except Exception as error:
                last_error = error
                if attempt >= self.max_retries:
                    break
                sleep_for = self.retry_backoff_s * (2 ** attempt) + random.uniform(0.0, 0.2)
                time.sleep(sleep_for)
        raise last_error
    
    
    def get_response_with_local_api(self, sys_prompt, user_prompt):
        
        url = "http://129.32.94.221:{}/generate".format(self.server_port)
       
        response = requests.post(url,  data={
            "sys_prompt": sys_prompt,
            "user_prompt": user_prompt
        })
        return response.json()['response']

           
    def get_response(self, model_name, sys_prompt, user_prompt, messages=None, return_metadata=False):
        if model_name not in [
            'deepseek-chat',
            'qwen2.5-72b-instruct',
            'gpt-4o-2024-11-20', 
            'gpt-4o-mini']:
            print('Please check model_type!!!', model_name)
            return 
            
        try:
            if model_name in ['gpt-4o-2024-11-20', 'gpt-4o-mini']: 
                payload, meta = self.get_response_with_gpt4(
                    sys_prompt,
                    user_prompt,
                    self.max_tokens,
                    self.temperature,
                    model_name,
                )
        
            elif model_name in ['deepseek-chat']:
                payload, meta = self.get_response_with_deepseek_chat(
                    sys_prompt,
                    user_prompt,
                    self.max_tokens,
                    self.temperature,
                    messages,
                    model_name=model_name,
                )
            
            elif model_name in ['qwen2.5-72b-instruct']:
                payload, meta = self.get_response_with_Qwen(
                    sys_prompt,
                    user_prompt,
                    self.max_tokens,
                    self.temperature,
                    model_name,
                )
            
            else:
                print('model_type error! now the value is: ', model_name)
                return 
            if return_metadata:
                return payload, meta
            return payload
            
        except Exception as e:
            print('Error in get_response:', e)
            if return_metadata:
                return None, self._build_error_meta(model_name=model_name, error=e)
            return None

    async def async_get_response(self, model_name, sys_prompt, user_prompt, messages=None, return_metadata=False):
        if model_name not in [
            'deepseek-chat',
            'qwen2.5-72b-instruct',
            'gpt-4o-2024-11-20', 
            'gpt-4o-mini']:
            print('Please check model_type!!!', model_name)
            return 
            
        try:
            if model_name in ['gpt-4o-2024-11-20', 'gpt-4o-mini']: 
                payload, meta = self.get_response_with_gpt4(
                    sys_prompt,
                    user_prompt,
                    self.max_tokens,
                    self.temperature,
                    model_name,
                )
        
            elif model_name in ['deepseek-chat']:
                payload, meta = await self.async_get_response_with_deepseek_chat(
                    sys_prompt,
                    user_prompt,
                    self.max_tokens,
                    self.temperature,
                    messages,
                    model_name=model_name,
                )
            elif model_name in ['qwen2.5-72b-instruct']:
                payload, meta = self.get_response_with_Qwen(
                    sys_prompt,
                    user_prompt,
                    self.max_tokens,
                    self.temperature,
                    model_name,
                )
            
            else:
                print('model_type error! now the value is: ', model_name)
                return 
            if return_metadata:
                return payload, meta
            return payload
            
        except Exception as e:
            print('Error in get_response:', e)
            if return_metadata:
                return None, self._build_error_meta(model_name=model_name, error=e)
            return None

    def _build_meta(self, *, provider, model, response_obj, latency_ms, status):
        usage = self._extract_usage(response_obj)
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)
        estimated_cost = self._estimate_cost(model, prompt_tokens, completion_tokens)
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "provider": provider,
            "model": model,
            "latencyMs": round(float(latency_ms), 3),
            "promptTokens": int(prompt_tokens),
            "completionTokens": int(completion_tokens),
            "totalTokens": int(total_tokens),
            "estimatedCostUsd": round(float(estimated_cost), 8),
            "status": status,
            "error": "",
        }

    def _build_error_meta(self, *, model_name, error):
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "provider": self._provider_for_model(model_name),
            "model": model_name,
            "latencyMs": 0.0,
            "promptTokens": 0,
            "completionTokens": 0,
            "totalTokens": 0,
            "estimatedCostUsd": 0.0,
            "status": "error",
            "error": str(error)[:500],
        }

    @staticmethod
    def _extract_usage(response_obj):
        if response_obj is None:
            return {}
        usage = getattr(response_obj, "usage", None)
        if usage is not None:
            return {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                "total_tokens": getattr(usage, "total_tokens", 0) or 0,
            }
        if isinstance(response_obj, dict):
            usage = response_obj.get("usage", {}) or {}
            return {
                "prompt_tokens": usage.get("prompt_tokens", 0) or 0,
                "completion_tokens": usage.get("completion_tokens", 0) or 0,
                "total_tokens": usage.get("total_tokens", 0) or 0,
            }
        return {}

    def _estimate_cost(self, model_name, prompt_tokens, completion_tokens):
        price = self._pricing_per_1k.get(model_name, {"prompt": 0.0, "completion": 0.0})
        return (prompt_tokens / 1000.0) * price["prompt"] + (
            completion_tokens / 1000.0
        ) * price["completion"]

    @staticmethod
    def _provider_for_model(model_name):
        if model_name.startswith("gpt-"):
            return "openai"
        if "deepseek" in model_name:
            return "deepseek"
        if "qwen" in model_name:
            return "qwen"
        return "unknown"
        
if __name__ == '__main__':
    llm_test = LLM_lib(
                        key_file_path='./API_KEY.json', 
                        max_tokens=1024, 
                        temperature = 0.85
                    )
    
    with open('./Prompts.json') as f:
        task_templates_lib = json.load(f)
        
    sys_prompt = 'You are a helpful assistant'
    print(task_templates_lib)
    
    '''
    init_queries = llm_test.get_response(
        model_name = 'deepseek-chat',
        sys_prompt=sys_prompt,
        user_prompt=task_templates_js['database_course_query']
    )
    
    print('res:', init_queries)
    '''
    
    test_promt = task_templates_lib['classify_prompt'].format(clean_title='a', clean_body='b', topic_seed='c')
    print('test_prompt:', test_promt)
        


