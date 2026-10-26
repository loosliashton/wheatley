# Bring Your Own LLM: Provider Configuration

Wheatley is completely LLM-agnostic. You can connect it to cloud APIs or run 100% locally and privately in your homelab.

Configure the provider in `/etc/wheatley/config.yaml`:

---

## 1. Google Gemini (Native Cloud)

Google Gemini is fast, accurate, and cost-effective for homelab reasoning.

```yaml
llm:
  provider: gemini
  model: gemini-2.5-flash
  history_turns: 10
  max_tool_iterations: 10
```

In `/etc/wheatley/secrets.env`:
```bash
GEMINI_API_KEY="your-api-key-here"
```

---

## 2. Local Ollama (100% Private Homelab)

Run Wheatley completely offline without sending any homelab data outside your network.

```yaml
llm:
  provider: openai
  base_url: http://192.168.1.50:11434/v1
  model: qwen2.5:14b
  api_key: ollama  # required by client, any string works
  history_turns: 10
  max_tool_iterations: 8
```

Recommended Ollama models for function calling and tool use:
- `qwen2.5:7b` / `qwen2.5:14b` / `qwen2.5:32b`
- `llama3.2:3b` / `llama3.1:8b`
- `mistral:7b`

---

## 3. OpenAI (`gpt-4o-mini`, `gpt-4o`)

```yaml
llm:
  provider: openai
  base_url: https://api.openai.com/v1
  model: gpt-4o-mini
  history_turns: 10
  max_tool_iterations: 10
```

In `/etc/wheatley/secrets.env`:
```bash
OPENAI_API_KEY="sk-..."
```

---

## 4. Groq (Ultra-Fast Inference)

```yaml
llm:
  provider: openai
  base_url: https://api.groq.com/openai/v1
  model: llama-3.3-70b-versatile
  history_turns: 10
  max_tool_iterations: 10
```

In `/etc/wheatley/secrets.env`:
```bash
OPENAI_API_KEY="gsk_..."
```

---

## 5. Other OpenAI-Compatible Endpoints

Any server implementing the OpenAI Chat Completions API (`/v1/chat/completions`) with tool calling support works with Wheatley:
- **vLLM**
- **LocalAI**
- **DeepSeek API** (`https://api.deepseek.com/v1`)
- **Mistral API** (`https://api.mistral.ai/v1`)
