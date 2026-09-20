#!/usr/bin/env python3
"""Jev (typesafe/jev-1.13) 可视化演示的本地服务。

- 静态托管 index.html
- POST /api/decisions 代理转发到 OpenRouter Decisions API（避免浏览器 CORS，key 不落地到 HTML）
- body 中带 "mock": true 时返回本地模拟响应，便于无有效 key 时完整体验界面

用法:
    OPENROUTER_API_KEY=sk-or-v1-... python3 server.py [port]
或在页面右上角的 key 输入框中粘贴（仅保存在浏览器 localStorage）。
"""
import hashlib
import json
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8787
OPENROUTER_URL = "https://openrouter.ai/api/alpha/decisions"
ROOT = Path(__file__).parent

import os
ENV_KEY = os.environ.get("OPENROUTER_API_KEY", "")


def mock_answers(state, questions):
    """离线模拟：根据 state+问题内容生成确定性的伪概率，仅供界面演示。"""
    state_text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
    hot_words = ["紧急", "失败", "中断", "投诉", "退款", "威胁", "愤怒", "urgent", "down", "refund", "cancel", "危险", "rm -rf", "删除"]
    heat = sum(1 for w in hot_words if w in state_text)
    base = min(0.55 + 0.12 * heat, 0.97)

    def jitter(seed):
        h = int(hashlib.md5(seed.encode()).hexdigest()[:8], 16)
        return (h % 1000) / 1000.0

    answers = {}
    for name, q in questions.items():
        qtype = q.get("type")
        j = jitter(name + state_text)
        if qtype == "noul":
            p = min(max(base + (j - 0.5) * 0.15, 0.02), 0.99)
            answers[name] = {"type": "noul", "noul": round(p, 4)}
        elif qtype == "choice":
            keys = list((q.get("criteria") or {}).keys()) or ["a", "b"]
            scores = [jitter(name + k + state_text) + (base if any(w in (q["criteria"][k] + k) for w in ["计费", "支付", "billing", "技术"]) else 0.3) for k in keys]
            total = sum(scores) or 1.0
            probs = {k: round(s / total, 4) for k, s in zip(keys, scores)}
            best = max(probs, key=probs.get)
            answers[name] = {"type": "choice", "choice": best, "confidence": probs[best], "probabilities": probs}
        elif qtype == "score":
            scale = len(q.get("criteria") or [0, 1, 2]) - 1
            v = min(max(base * scale + (j - 0.5) * 0.4, 0.0), float(scale))
            answers[name] = {"type": "score", "score": round(v, 2), "confidence": round(0.7 + j * 0.29, 4), "scale_max": scale}
    return answers


def call_openrouter(upstream, key):
    """调用 OpenRouter Decisions API。

    本机环境实测：系统 Python 3.9 的 urllib 到 Cloudflare 偶发 TLS 握手卡死（~50%），
    而 curl 稳定且秒回。因此 curl 作为主通道，失败时再用 urllib 兜底。
    返回 (http_code, dict)。
    """
    import subprocess
    body = json.dumps(upstream).encode()
    try:
        out = subprocess.run(
            ["curl", "-sS", "--max-time", "30", "-X", "POST", OPENROUTER_URL,
             "-H", f"Authorization: Bearer {key}", "-H", "Content-Type: application/json",
             "-H", "HTTP-Referer: http://localhost", "-H", "X-OpenRouter-Title: jev-demo",
             "-d", body.decode(), "-w", "\n%{http_code}"],
            capture_output=True, text=True, timeout=40)
        raw, _, code = out.stdout.rpartition("\n")
        if code:
            return int(code), json.loads(raw)
    except Exception:
        pass
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost",
        "X-OpenRouter-Title": "jev-demo",
    }
    last_err = None
    for _ in range(2):
        req = urllib.request.Request(OPENROUTER_URL, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except Exception:
                return e.code, {"error": {"message": f"OpenRouter 返回 HTTP {e.code}"}}
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    return 502, {"error": {"message": f"请求 OpenRouter 失败: {last_err}"}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/api/decisions":
            return self._send_json({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception as e:
            return self._send_json({"error": {"message": f"请求体不是合法 JSON: {e}"}}, 400)

        questions = payload.get("questions") or {}
        state = payload.get("state", "")
        model = payload.get("model") or "typesafe/jev-1.13"

        if payload.get("mock"):
            t0 = time.time()
            time.sleep(0.12 + 0.02 * len(questions))
            answers = mock_answers(state, questions)
            state_text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
            in_tokens = len(state_text) // 3 + sum(len(json.dumps(q, ensure_ascii=False)) for q in questions.values()) // 3
            return self._send_json({
                "model": model + " (模拟演示)",
                "provider": "local-mock",
                "answers": answers,
                "usage": {"input_tokens": in_tokens, "output_tokens": 8 * len(questions),
                          "cost": round(in_tokens * 0.042 / 1e6, 8)},
                "latency_ms": round((time.time() - t0) * 1000),
            })

        key = self.headers.get("x-api-key", "").strip() or ENV_KEY
        if not key:
            return self._send_json({"error": {"message": "未提供 OpenRouter API Key：请在页面右上角粘贴，或以 OPENROUTER_API_KEY 环境变量启动服务。也可以切换到「模拟演示」模式。"}}, 401)

        upstream = {"model": model, "state": state, "questions": questions}
        t0 = time.time()
        code, data = call_openrouter(upstream, key)
        data["latency_ms"] = round((time.time() - t0) * 1000)
        return self._send_json(data, code)


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    print(f"Jev 演示已启动: http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
