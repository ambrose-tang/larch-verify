"""LLM providers: Anthropic API, Claude Code CLI (headless), response cache, fake."""
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from ..util import cache_root, sha256, stable_json
import re

from .base import LLMError, LLMRequest, LLMResponse, Provider, UsageLimitError

_LIMIT_RE = re.compile(r"(session|usage|weekly|monthly) limit|hit your .{0,20}limit|limit reached", re.I)
from .pricing import cost_usd, resolve_model, supports_adaptive_thinking, supports_effort


def _parse_json_text(text: str):
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            return json.loads(t[start : end + 1])
        raise


class AnthropicProvider(Provider):
    """Messages API: Anthropic directly (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / `ant`
    profile / workload identity federation), Amazon Bedrock, or Google Vertex AI.
    ANTHROPIC_BASE_URL is honoured for corporate gateways."""

    name = "anthropic"

    def __init__(self, platform: str = "anthropic") -> None:
        import anthropic

        self._anthropic = anthropic
        self.platform = platform
        self.name = platform
        if platform == "bedrock":
            region = os.environ.get("LARCH_AWS_REGION") or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
            if not region:
                raise LLMError("Amazon Bedrock needs a region: set AWS_REGION (or LARCH_AWS_REGION)")
            cls = getattr(anthropic, "AnthropicBedrockMantle", None)
            try:
                self.client = cls(aws_region=region, max_retries=4) if cls else anthropic.AnthropicBedrock(aws_region=region, max_retries=4)
            except Exception as e:  # noqa: BLE001 - missing extras or credentials
                raise LLMError(f"could not create the Bedrock client ({e}); install with `pip install 'larch-verify[bedrock]'`") from e
            self.region = region
        elif platform == "vertex":
            project = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
            region = os.environ.get("CLOUD_ML_REGION") or "global"
            if not project:
                raise LLMError("Vertex AI needs a project: set ANTHROPIC_VERTEX_PROJECT_ID (and optionally CLOUD_ML_REGION)")
            try:
                self.client = anthropic.AnthropicVertex(project_id=project, region=region, max_retries=4)
            except Exception as e:  # noqa: BLE001
                raise LLMError(f"could not create the Vertex AI client ({e}); install with `pip install 'larch-verify[vertex]'`") from e
            self.region = region
        else:
            self.client = anthropic.Anthropic(max_retries=4)

    def platform_model(self, model: str) -> str:
        """Model ID as the platform names it (Bedrock prefixes `anthropic.`)."""
        if self.platform == "bedrock" and not model.startswith(("anthropic.", "arn:", "us.", "eu.", "apac.", "global.")):
            return "anthropic." + model
        return model

    def describe(self) -> str:
        return {"bedrock": f"Amazon Bedrock ({getattr(self, 'region', '')})",
                "vertex": f"Google Vertex AI ({getattr(self, 'region', '')})"}.get(self.platform, "Anthropic API")

    def complete(self, req: LLMRequest) -> LLMResponse:
        anthropic = self._anthropic
        model = resolve_model(req.model)
        api_model = self.platform_model(model)
        kwargs: dict = {
            "model": api_model,
            "max_tokens": min(req.max_tokens, 64000) if model.startswith("claude-haiku") else req.max_tokens,
            "system": [{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": req.prompt}],
        }
        if supports_adaptive_thinking(model):
            kwargs["thinking"] = {"type": "adaptive"}
        output_config: dict = {}
        if req.effort and supports_effort(model):
            output_config["effort"] = req.effort
        if req.json_schema:
            output_config["format"] = {"type": "json_schema", "schema": req.json_schema}
        if output_config:
            kwargs["output_config"] = output_config
        t0 = time.monotonic()
        try:
            with self.client.messages.stream(**kwargs) as stream:
                msg = stream.get_final_message()
        except anthropic.BadRequestError as e:
            raise LLMError(f"bad request: {e.message}") from e
        except anthropic.AuthenticationError as e:
            raise LLMError(f"{self.describe()} authentication failed" + (" (set ANTHROPIC_API_KEY)" if self.platform == "anthropic" else "")) from e
        except anthropic.PermissionDeniedError as e:
            raise LLMError(f"{self.describe()} denied access to {api_model}: {e.message}") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"model {api_model} is not available on {self.describe()}: {e.message}") from e
        except anthropic.RateLimitError as e:
            raise LLMError("rate limited by the Anthropic API") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("could not reach the Anthropic API") from e
        latency = time.monotonic() - t0
        if msg.stop_reason == "refusal":
            raise LLMError("the model declined this request")
        text = "".join(b.text for b in msg.content if b.type == "text")
        data = None
        if req.json_schema:
            try:
                data = _parse_json_text(text)
            except json.JSONDecodeError as e:
                raise LLMError(f"model returned invalid JSON (stop_reason={msg.stop_reason})") from e
        u = msg.usage
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        return LLMResponse(
            text=text,
            model=model,
            data=data,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=cr,
            cache_write_tokens=cw,
            cost_usd=cost_usd(model, u.input_tokens, u.output_tokens, cr, cw),
            latency_s=latency,
            stop_reason=msg.stop_reason,
        )


_CLI_SLOTS = threading.BoundedSemaphore(int(os.environ.get("LARCH_MAX_CONCURRENCY", "6")))


class ClaudeCodeProvider(Provider):
    """Headless Claude Code (`claude -p`): uses the user's Claude Code login, no API key.

    Tools, settings, hooks, MCP servers and slash commands are all disabled; the
    system prompt is replaced, so the call is a plain completion."""

    name = "claude-code"

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
        if not Path(self.binary).exists():
            raise LLMError("the `claude` CLI was not found (install Claude Code, or set ANTHROPIC_API_KEY)")

    def complete(self, req: LLMRequest) -> LLMResponse:
        model = resolve_model(req.model)
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(req.system)
            sys_path = f.name
        cmd = [
            self.binary, "-p",
            "--output-format", "json",
            "--model", model,
            "--system-prompt-file", sys_path,
            "--tools", "",
            "--no-session-persistence",
            "--setting-sources", "",
            "--strict-mcp-config",
            "--disable-slash-commands",
        ]
        if req.effort and supports_effort(model):
            cmd += ["--effort", req.effort]
        if req.json_schema:
            cmd += ["--json-schema", json.dumps(req.json_schema)]
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE",)}
        last_err = "unknown error"
        _CLI_SLOTS.acquire()
        auth_failures = 0
        try:
            for attempt in range(5):
                t0 = time.monotonic()
                try:
                    proc = subprocess.run(
                        cmd, input=req.prompt, capture_output=True, text=True, timeout=1800, env=env,
                        cwd=tempfile.gettempdir(),
                    )
                except subprocess.TimeoutExpired:
                    last_err = "claude CLI timed out"
                    continue
                latency = time.monotonic() - t0
                try:
                    d = json.loads(proc.stdout)
                except json.JSONDecodeError:
                    last_err = f"claude CLI failed (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-400:]}"
                    time.sleep(2 + attempt * 3)
                    continue
                if d.get("is_error"):
                    status = d.get("api_error_status")
                    last_err = f"claude CLI error ({status}): {str(d.get('result'))[:300]}"
                    if _LIMIT_RE.search(str(d.get("result"))):
                        hint = re.search(r"resets? ([^\n]+)", str(d.get("result")))
                        raise UsageLimitError(
                            "Claude usage limit reached" + (f" (resets {hint.group(1).strip()})" if hint else ""),
                            hint.group(1).strip() if hint else "",
                        )
                    if status in (401, 403) or "authenticate" in str(d.get("result")).lower():
                        # Seen around session-limit transitions: the login is briefly rejected.
                        # Never treat it as a property of the request; back off, then pause.
                        auth_failures += 1
                        if auth_failures >= 3:
                            raise UsageLimitError("Claude Code authentication failed (is `claude` logged in?)", "")
                        time.sleep(30 * auth_failures)
                        continue
                    if status in (429, 500, 502, 503, 504, 529) or status is None:
                        time.sleep(min(60, 4 * (2**attempt)) + random.random())
                        continue
                    raise LLMError(last_err)
                text = d.get("result") or ""
                data = None
                if req.json_schema:
                    data = d.get("structured_output")
                    if data is None:
                        try:
                            data = _parse_json_text(text)
                        except json.JSONDecodeError:
                            last_err = "model returned invalid JSON"
                            continue
                u = d.get("usage") or {}
                return LLMResponse(
                    text=text if isinstance(text, str) else json.dumps(text),
                    model=model,
                    data=data,
                    input_tokens=int(u.get("input_tokens", 0)),
                    output_tokens=int(u.get("output_tokens", 0)),
                    cache_read_tokens=int(u.get("cache_read_input_tokens", 0)),
                    cache_write_tokens=int(u.get("cache_creation_input_tokens", 0)),
                    cost_usd=float(d.get("total_cost_usd") or 0.0),
                    latency_s=latency,
                    stop_reason=d.get("stop_reason"),
                )
        finally:
            _CLI_SLOTS.release()
            try:
                os.unlink(sys_path)
            except OSError:
                pass
        raise LLMError(last_err)


class CachingProvider(Provider):
    """Content-addressed response cache (used by the benchmark and `--cache`).
    Cached responses keep their original cost as *nominal* cost; spend is 0."""

    def __init__(self, inner: Provider, directory: Path | None = None, salt: str = ""):
        self.inner = inner
        self.name = f"{inner.name}+cache"
        self.dir = directory or (cache_root() / "llm")
        self.salt = salt or os.environ.get("LARCH_CACHE_SALT", "")

    def _key(self, req: LLMRequest) -> str:
        return sha256(
            self.salt, resolve_model(req.model), req.system, req.prompt,
            stable_json(req.json_schema), str(req.effort), str(req.max_tokens),
        )

    def complete(self, req: LLMRequest) -> LLMResponse:
        key = self._key(req)
        path = self.dir / key[:2] / f"{key}.json"
        if path.exists():
            try:
                d = json.loads(path.read_text())
                d["cached"] = True
                return LLMResponse(**d)
            except (json.JSONDecodeError, TypeError):
                pass
        resp = self.inner.complete(req)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(resp.__dict__))
        os.replace(tmp, path)
        return resp

    def describe(self) -> str:
        return f"{self.inner.describe()} (cached)"


class FakeProvider(Provider):
    """Deterministic provider for tests: a function from request to text/data."""

    name = "fake"

    def __init__(self, respond: Callable[[LLMRequest], str | dict]):
        self.respond = respond
        self.requests: list[LLMRequest] = []

    def complete(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        out = self.respond(req)
        if isinstance(out, dict):
            return LLMResponse(text=json.dumps(out), model=req.model, data=out, cost_usd=0.0)
        return LLMResponse(text=out, model=req.model, data=None, cost_usd=0.0)


def make_provider(kind: str = "auto", *, cache: bool = False) -> Provider:
    """auto: Bedrock or Vertex when selected by the standard Claude Code environment
    variables (CLAUDE_CODE_USE_BEDROCK / CLAUDE_CODE_USE_VERTEX), else the Anthropic API
    if credentials are configured, else the Claude Code CLI."""
    if kind == "auto":
        if os.environ.get("CLAUDE_CODE_USE_BEDROCK") in ("1", "true"):
            kind = "bedrock"
        elif os.environ.get("CLAUDE_CODE_USE_VERTEX") in ("1", "true"):
            kind = "vertex"
        elif os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            kind = "anthropic"
        elif shutil.which("claude") or (Path.home() / ".local" / "bin" / "claude").exists():
            kind = "claude-code"
        else:
            raise LLMError(
                "No LLM provider available. Set ANTHROPIC_API_KEY, or install and log in to "
                "Claude Code (`claude`) to use your Claude subscription."
            )
    if kind in ("anthropic", "bedrock", "vertex"):
        p: Provider = AnthropicProvider(kind)
    elif kind in ("claude-code", "claude", "cli"):
        p = ClaudeCodeProvider()
    else:
        raise LLMError(f"unknown provider {kind!r} (use auto, anthropic, bedrock, vertex or claude-code)")
    return CachingProvider(p) if cache else p
