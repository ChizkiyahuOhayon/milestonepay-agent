# Dependencies and AI use

The app uses Python's standard library and browser HTML/CSS/JavaScript. It calls
PayPal's Sandbox REST APIs directly; it includes no copied PayPal SDK or sample
source. [Orders v2](https://developer.paypal.com/api/orders/v2) and
[Payments v2](https://developer.paypal.com/api/payments/v2) describe the APIs.

Optional local inference uses [Ollama](https://github.com/ollama/ollama), whose
upstream code is MIT-licensed, and [Qwen3-4B](https://huggingface.co/Qwen/Qwen3-4B),
whose model license is Apache-2.0. The requested Ollama model tag is
[qwen3:4b](https://ollama.com/library/qwen3:4b). Neither tool nor model weights
are bundled; their terms apply separately.

Codex assisted with code, tests, UI, prompts and documentation. Claude supplied
strategy and scope decisions. Human review of the release and rights is pending.
The project's proposed MIT license is a draft awaiting author confirmation.
