# Bring Your Own LLM: Provider Configuration

Wheatley is completely LLM-agnostic. You can connect it to cloud APIs or run 100% locally and privately in your homelab.

Configure the provider in `/etc/wheatley/config.yaml` under `llm:`:

---

## 1. Google Gemini (Native Cloud)

Google Gemini is fast, highly accurate, and cost-effective for homelab reasoning.

In `/etc/wheatley/config.yaml`:
```yaml
llm:
  provider: gemini
  model: gemini-3.8-flash
  fallback_models:               # optional: tried automatically if the primary model returns 503 or 429
    - gemini-3.5-flash
  history_turns: 10
  session_timeout_minutes: 60    # auto-resets context after 60 min inactivity (0 to disable)
  max_tool_iterations: 10
```

In `/etc/wheatley/secrets.env`:
```bash
GEMINI_API_KEY="AIzaSy..."
```

> **Model Names & Discovery:**
> - Specify the bare model name without the `models/` prefix (e.g. `gemini-3.8-flash`, not `models/gemini-3.8-flash`). If `models/` is accidentally included, Wheatley strips it automatically.
> - To check which models are available for your API key, run:
>   ```bash
>   curl "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY" | grep '"name"'
>   ```
> - Use `fallback_models` to ensure high availability if your primary model experiences transient capacity spikes (e.g. `503 UNAVAILABLE`).

---

## 2. Local Ollama (100% Private Offline Homelab)

Run Wheatley completely offline without sending any homelab data outside your network.

In `/etc/wheatley/config.yaml`:
```yaml
llm:
  provider: ollama               # or 'openai'
  api_base: http://192.168.1.50:11434/v1  # defaults to http://localhost:11434/v1
  model: qwen2.5:14b
  history_turns: 10
  session_timeout_minutes: 60
  max_tool_iterations: 8
```

In `/etc/wheatley/secrets.env`:
```bash
OPENAI_API_KEY="ollama"          # client requires non-empty string; any value works
```

Recommended Ollama models for function calling and tool use:
- `qwen2.5:7b` / `qwen2.5:14b` / `qwen2.5:32b`
- `llama3.2:3b` / `llama3.1:8b`
- `mistral:7b`

---

## 3. OpenAI (`gpt-4o-mini`, `gpt-4o`)

In `/etc/wheatley/config.yaml`:
```yaml
llm:
  provider: openai
  api_base: https://api.openai.com/v1
  model: gpt-4o-mini
  history_turns: 10
  session_timeout_minutes: 60
  max_tool_iterations: 10
```

In `/etc/wheatley/secrets.env`:
```bash
OPENAI_API_KEY="sk-proj-..."
```

---

## 4. Groq (Ultra-Fast Cloud Inference)

In `/etc/wheatley/config.yaml`:
```yaml
llm:
  provider: groq                 # automatically resolves https://api.groq.com/openai/v1
  model: llama-3.3-70b-versatile
  history_turns: 10
  session_timeout_minutes: 60
  max_tool_iterations: 10
```

In `/etc/wheatley/secrets.env`:
```bash
GROQ_API_KEY="gsk_..."
```

---

## 5. Other OpenAI-Compatible Endpoints

Any inference server implementing the OpenAI Chat Completions API (`/v1/chat/completions`) with tool-calling support is compatible with Wheatley:
- **vLLM**
- **LocalAI**
- **DeepSeek API** (`api_base: https://api.deepseek.com/v1`)
- **Mistral API** (`api_base: https://api.mistral.ai/v1`)

```yaml
llm:
  provider: openai_compatible
  api_base: http://my-vllm-server:8000/v1
  model: deepseek-ai/DeepSeek-R1-Distill-Qwen-14B
  history_turns: 10
  session_timeout_minutes: 60
  max_tool_iterations: 10
```
