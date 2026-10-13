import asyncio
import json as jsonlib
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).parent
DATA = BASE / "data"
UPLOADS = DATA / "uploads"
OUTPUTS = DATA / "outputs"
META_DIR = DATA / "meta"
for p in (UPLOADS, OUTPUTS, META_DIR):
    p.mkdir(parents=True, exist_ok=True)

# ---------- 配置(全部可用环境变量覆盖,见 README) ----------
LLM_BASE = os.environ.get("PT_LLM_BASE", "http://127.0.0.1:8910").rstrip("/")
LLAMA_URL = f"{LLM_BASE}/v1/chat/completions"
HEALTH_URL = f"{LLM_BASE}/health"
BABELDOC = os.environ.get("PT_BABELDOC", "babeldoc")
ZCODE = os.environ.get("PT_ZCODE", "zcode")
OPENCODE = os.environ.get("PT_OPENCODE", "opencode")
TRANSLATE_MODEL = os.environ.get("PT_TRANSLATE_MODEL", "Hy-MT2-7B")
TRANSLATE_TIMEOUT = 180
TASK_TIMEOUT = 3600
CHAT_TIMEOUT = 240

# ---------- AI 问答 harness 配置 ----------
# CLI 类:cmd 模板中 {prompt} 会被替换为问题;continue_flag 为续接会话的旗标;
#         attach_flag 非空表示支持以该旗标附加论文 PDF,为空则改为在 prompt 中提示文件路径。
# API 类:type=api,OpenAI 兼容 chat/completions,无会话(前端拼接历史),不支持论文附件。
AI_CONFIG_FILE = DATA / "ai-config.json"
DEFAULT_AI_PROVIDERS = {
    "zcode":    {"cmd": ["zcode", "-p", "{prompt}"], "json_flag": "--json", "attach_flag": "--attach"},
    "claude":   {"cmd": ["claude", "-p", "{prompt}"], "continue_flag": "--continue"},
    "codex":    {"cmd": ["codex", "exec", "{prompt}"]},
    "kimicode": {"cmd": ["kimicode", "-p", "{prompt}"]},
    "opencode": {"cmd": [OPENCODE, "run", "--model", "{model}", "{prompt}"], "continue_flag": "--continue", "model": "opencode/nemotron-3-ultra-free", "variant_flag": "--variant"},
    "openai":   {"type": "api", "base_url": "https://api.openai.com/v1", "api_key": "", "model": "gpt-4o-mini"},
}
AI_DEFAULT_PROVIDER = "zcode"


def load_ai_config():
    cfg = {"provider": AI_DEFAULT_PROVIDER, "providers": {k: dict(v) for k, v in DEFAULT_AI_PROVIDERS.items()}}
    if AI_CONFIG_FILE.is_file():
        try:
            saved = jsonlib.loads(AI_CONFIG_FILE.read_text())
            if saved.get("provider"):
                cfg["provider"] = saved["provider"]
            if isinstance(saved.get("providers"), dict):
                for k, v in saved["providers"].items():
                    if k in cfg["providers"] and isinstance(v, dict):
                        cfg["providers"][k].update(v)
                    elif isinstance(v, dict):
                        cfg["providers"][k] = v
        except Exception:
            pass
    if cfg["provider"] not in cfg["providers"]:
        cfg["provider"] = AI_DEFAULT_PROVIDER
    return cfg


def save_ai_config(cfg):
    out = {"provider": cfg["provider"], "providers": {}}
    for k, v in cfg["providers"].items():
        diff = {kk: vv for kk, vv in v.items() if DEFAULT_AI_PROVIDERS.get(k, {}).get(kk) != vv}
        if diff:
            out["providers"][k] = diff
    AI_CONFIG_FILE.write_text(jsonlib.dumps(out, ensure_ascii=False, indent=2))


def save_ai_provider(provider):
    cfg = load_ai_config()
    cfg["provider"] = provider
    save_ai_config(cfg)


# ---------- 会话 ID 映射(对话 key -> AI CLI sessionId) ----------
SESSIONS_FILE = Path.home() / ".cache" / "pdf-translate-chat" / "sessions.json"


def load_sessions():
    try:
        d = jsonlib.loads(SESSIONS_FILE.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_sessions(d):
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSIONS_FILE.write_text(jsonlib.dumps(d, ensure_ascii=False, indent=2))

app = FastAPI()
tasks = {}
q = queue.Queue()


def worker():
    while True:
        tid = q.get()
        t = tasks[tid]
        try:
            out = OUTPUTS / tid
            out.mkdir(exist_ok=True)
            t["status"] = "running"
            cmd = [
                BABELDOC, "--files", str(t["upload_path"]),
                "--openai", "--openai-model", TRANSLATE_MODEL,
                "--openai-base-url", f"{LLM_BASE}/v1",
                "--openai-api-key", "none",
                "--lang-in", "en", "--lang-out", "zh",
                "--output", str(out),
                "--watermark-output-mode", "no_watermark",
                "--no-watermark",
                "--use-alternating-pages-dual",
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=TASK_TIMEOUT)
            if r.returncode != 0:
                t["status"] = "failed"
                t["error"] = (r.stderr or r.stdout)[-800:]
            else:
                pdfs = list(out.glob("*.pdf"))
                dual = next((p for p in pdfs if p.name.endswith("dual.pdf")), None)
                mono = next((p for p in pdfs if p.name.endswith("mono.pdf")), None)
                if dual:
                    t["dual"] = dual.name
                if mono:
                    t["mono"] = mono.name
                t["status"] = "done" if (dual or mono) else "failed"
                if t["status"] == "failed":
                    t["error"] = "翻译完成但未生成输出文件"
                else:
                    analyze_q.put(tid)
        except Exception as e:
            t["status"] = "failed"
            t["error"] = str(e)[:800]


threading.Thread(target=worker, daemon=True).start()

# ---------- AI 语义分析(归类 + 一句话摘要) ----------
analyze_q = queue.Queue()

ANALYZE_PROMPT = (
    '阅读这篇学术论文,严格只输出一个JSON对象,禁止输出任何其他文字或代码块标记。'
    '格式:{"category": "论文研究类别,中文2-8个字", "summary": "一句话中文摘要,30字以内,概括方法核心"}'
)

def analyze_one(tid):
    t = tasks.get(tid)
    if not t:
        return
    if t.get("meta", {}).get("category"):
        return
    t["analyzing"] = True
    try:
        cmd = [ZCODE, "-p", ANALYZE_PROMPT, "--attach", t["upload_path"], "--mode", "plan", "--cwd", "/tmp"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        out = r.stdout or ""
        blob = out[out.find("{"): out.rfind("}") + 1]
        data = jsonlib.loads(blob)
        t["meta"] = {
            "category": str(data.get("category", "未分类"))[:20],
            "summary": str(data.get("summary", ""))[:80],
        }
        (META_DIR / f"{tid}.json").write_text(jsonlib.dumps(t["meta"], ensure_ascii=False))
    except Exception as e:
        t["meta"] = {"error": str(e)[:120]}
    finally:
        t["analyzing"] = False

def analyze_worker():
    while True:
        tid = analyze_q.get()
        try:
            analyze_one(tid)
        except Exception:
            pass

threading.Thread(target=analyze_worker, daemon=True).start()


def restore_tasks():
    """重启后从磁盘恢复历史任务(输出文件还在的任务标记为完成)。"""
    for out in sorted(OUTPUTS.iterdir()):
        if not out.is_dir():
            continue
        tid = out.name
        up = UPLOADS / f"{tid}.pdf"
        if not up.is_file():
            continue
        name_file = UPLOADS / f"{tid}.name"
        name = name_file.read_text().strip() if name_file.is_file() else tid
        meta_file = META_DIR / f"{tid}.json"
        meta = {}
        if meta_file.is_file():
            try:
                meta = jsonlib.loads(meta_file.read_text())
            except Exception:
                meta = {}
        dual = next((p.name for p in out.glob("*.dual.pdf")), None)
        mono = next((p.name for p in out.glob("*.mono.pdf")), None)
        if not (dual or mono):
            continue
        tasks[tid] = {
            "id": tid,
            "name": name,
            "status": "done",
            "upload_path": str(up),
            "dual": dual,
            "mono": mono,
            "error": None,
            "meta": meta,
            "createdAt": int(up.stat().st_mtime * 1000),
        }


restore_tasks()
for t in list(tasks.values()):
    if t["status"] == "done" and not t.get("meta", {}).get("category"):
        analyze_q.put(t["id"])


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    tid = uuid.uuid4().hex[:12]
    dest = UPLOADS / f"{tid}.pdf"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    stem = Path(file.filename or dest.name).stem
    (UPLOADS / f"{tid}.name").write_text(stem)
    tasks[tid] = {
        "id": tid,
        "name": stem,
        "status": "queued",
        "upload_path": str(dest),
        "dual": None,
        "mono": None,
        "error": None,
        "meta": {},
        "createdAt": int(time.time() * 1000),
    }
    q.put(tid)
    return tasks[tid]


@app.post("/api/analyze/{tid}")
async def reanalyze(tid: str):
    t = tasks.get(tid)
    if not t:
        raise HTTPException(404, "no such task")
    if t["status"] != "done":
        raise HTTPException(400, "task not done")
    analyze_q.put(tid)
    return {"ok": True}


@app.get("/api/tasks")
def list_tasks():
    return sorted(tasks.values(), key=lambda t: t["id"], reverse=True)


@app.get("/api/files/{tid}/{variant}")
def get_file(tid: str, variant: str):
    t = tasks.get(tid)
    if not t:
        raise HTTPException(404, "no such task")
    if variant == "original":
        return FileResponse(t["upload_path"], media_type="application/pdf")
    name = t.get("dual") if variant == "dual" else t.get("mono")
    if not name:
        raise HTTPException(404, "not ready")
    return FileResponse(OUTPUTS / tid / name, media_type="application/pdf")


@app.post("/api/translate")
async def translate(body: dict):
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "empty text")
    if len(text) > 6000:
        text = text[:6000]
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    direction = "en2zh" if cjk / max(len(text), 1) < 0.15 else "zh2en"
    target = "中文" if direction == "en2zh" else "英语"
    prompt = (
        f"将以下学术文本翻译成{target},注意只需要输出翻译后的结果,不要额外解释:\n\n{text}"
    )
    req = jsonlib.dumps({
        "model": TRANSLATE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2048,
        "temperature": 0,
    }).encode()
    r = urllib.request.Request(LLAMA_URL, data=req, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=TRANSLATE_TIMEOUT) as resp:
            d = jsonlib.loads(resp.read())
    except Exception as e:
        raise HTTPException(502, f"翻译服务不可达: {e}")
    return {
        "translation": d["choices"][0]["message"]["content"].strip(),
        "direction": direction,
    }


@app.get("/api/health")
def health():
    llm = False
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as r:
            llm = r.status == 200
    except Exception:
        pass
    return {"backend": True, "llm": llm}


@app.post("/api/chat")
async def chat(body: dict):
    question = (body.get("message") or "").strip()
    if not question:
        raise HTTPException(400, "empty message")
    tid = body.get("taskId") or ""
    cont = bool(body.get("continue"))
    if len(question) > 8000:
        question = question[:8000]

    key = tid or "global"
    chat_dir = Path.home() / ".cache" / "pdf-translate-chat" / f"chat-{key}"
    chat_dir.mkdir(parents=True, exist_ok=True)

    ai_cfg = load_ai_config()
    provider = ai_cfg["provider"]
    conf = dict(ai_cfg["providers"][provider])
    ptype = conf.pop("type", "cli")
    attach = UPLOADS / f"{tid}.pdf" if tid else None
    attach_ok = bool(tid and attach.is_file())

    # opencode:写入项目级权限配置,非交互模式才能使用工具(读文件/跑命令)
    if provider == "opencode":
        pc = chat_dir / "opencode.json"
        if not pc.is_file():
            pc.write_text(jsonlib.dumps({
                "permission": {"bash": "allow", "edit": "allow", "webfetch": "allow", "external_directory": "allow"}
            }, ensure_ascii=False))

    # ---- OpenAI 兼容 API:无会话,历史由前端拼接;不支持论文附件 ----
    if ptype == "api":
        history = [h for h in (body.get("history") or [])[-10:] if h.get("content")]
        messages = [{"role": h.get("role", "user"), "content": str(h["content"])[:4000]} for h in history]
        messages.append({"role": "user", "content": question})
        req = jsonlib.dumps({"model": conf.get("model", "gpt-4o-mini"), "messages": messages}).encode()
        rq = urllib.request.Request(
            conf["base_url"].rstrip("/") + "/chat/completions",
            data=req,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + conf.get("api_key", "")},
        )
        try:
            with urllib.request.urlopen(rq, timeout=CHAT_TIMEOUT) as resp:
                data = jsonlib.loads(resp.read())
        except Exception as e:
            raise HTTPException(502, f"AI API 调用失败: {e}")
        try:
            answer = data["choices"][0]["message"]["content"].strip()
        except Exception:
            raise HTTPException(502, "AI API 返回格式异常")
        if not answer:
            raise HTTPException(502, "AI 未返回内容")
        return {"answer": answer, "attached": False, "provider": provider}

    # ---- CLI harness(ZCode / Claude Code / Codex / Kimi Code …)----
    cmd_template = conf.get("cmd")
    if not cmd_template:
        raise HTTPException(500, f"harness {provider} 缺少 cmd 配置")
    prompt = question
    if attach_ok and not cont and not conf.get("attach_flag"):
        # 无附加旗标的 harness:告知论文路径,由其自行读取(需具备文件读取能力)
        prompt += f"\n\n[参考资料:论文 PDF 已保存在 {attach},如你具备文件读取能力,请先阅读该文件再回答]"

    if conf.get("model"):
        cmd_template = [part.replace("{model}", conf["model"]) for part in cmd_template]
    cmd = [str(part).replace("{prompt}", prompt) for part in cmd_template]
    if conf.get("variant") and conf.get("variant_flag"):
        cmd += [conf["variant_flag"], conf["variant"]]
    sessions = load_sessions()

    # 无附加旗标的 harness(如 opencode):把论文复制进会话目录,避免外部目录权限拒绝
    attach_for_prompt = None
    if attach_ok and not cont and not conf.get("attach_flag"):
        try:
            dst = chat_dir / "paper.pdf"
            shutil.copyfile(attach, dst)
            attach_for_prompt = dst
        except Exception:
            pass
    if attach_for_prompt:
        prompt += f"\n\n[参考资料:论文 PDF 已保存在 {attach_for_prompt},请先阅读该文件再回答]"

    if conf.get("json_flag"):
        # 支持 --json 的 harness(如 ZCode):服务端真实会话,--resume <sessionId> 精确续接
        cmd.append(conf["json_flag"])
        sid = sessions.get(key)
        if cont and sid:
            cmd += ["--resume", sid]
        if attach_ok and not cont and conf.get("attach_flag"):
            cmd += [conf["attach_flag"], str(attach)]
    else:
        if cont and conf.get("continue_flag"):
            cmd.append(conf["continue_flag"])
        if attach_ok and not cont and conf.get("attach_flag"):
            cmd += [conf["attach_flag"], str(attach)]

    try:
        # 净室环境:systemd 会话变量会让 zcode 走 GUI 路径污染输出,最小集已验证为纯 headless
        # 注意:不要传 TERM——zcode 检测到 TERM 会启动完整 TUI/Electron,无 TERM 才是纯 headless
        child_env = {
            "HOME": os.environ.get("HOME", "/tmp"),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        }
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(chat_dir), env=child_env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CHAT_TIMEOUT)
    except asyncio.TimeoutError:
        raise HTTPException(504, "AI 回答超时")
    except FileNotFoundError:
        raise HTTPException(502, f"未找到 AI 命令行工具「{cmd[0]}」,请先安装并登录,或在配置中切换 harness")
    except Exception as e:
        raise HTTPException(502, f"无法启动 AI CLI: {e}")
    if proc.returncode != 0:
        raise HTTPException(502, (stderr or b"").decode(errors="replace")[-500:] or "AI CLI 运行失败")
    output = stdout.decode(errors="replace").strip()
    if not output:
        raise HTTPException(502, "AI 未返回内容")

    output = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", output)  # 剥离 ANSI 转义
    if conf.get("json_flag"):
        # 解析 --json:输出可能混杂启动日志,从尾部向前找含 response/sessionId 的合法 JSON
        answer, sid = None, None
        end = output.rfind("}")
        while end != -1:
            start = output.rfind("{", 0, end)
            if start == -1:
                break
            try:
                data = jsonlib.loads(output[start:end + 1])
                if "response" in data or "sessionId" in data:
                    answer = (data.get("response") or "").strip()
                    sid = data.get("sessionId")
                    break
            except Exception:
                pass
            end = output.rfind("}", 0, end)
        if sid:
            sessions[key] = sid
            save_sessions(sessions)
        if not answer:
            raise HTTPException(502, "AI CLI 输出解析失败: " + output[-200:])
        return {"answer": answer, "attached": attach_ok, "provider": provider, "sessionId": sid}

    return {"answer": output, "attached": attach_ok, "provider": provider}


@app.post("/api/chat/reset")
def chat_reset(body: dict):
    """清空某篇论文(或通用)对话:删除其会话目录与 sessionId 映射,下次提问重新开始。"""
    key = (body or {}).get("key") or "global"
    d = Path.home() / ".cache" / "pdf-translate-chat" / f"chat-{key}"
    shutil.rmtree(d, ignore_errors=True)
    sessions = load_sessions()
    sessions.pop(key, None)
    save_sessions(sessions)
    return {"ok": True}


@app.get("/api/ai/providers")
def ai_providers():
    cfg = load_ai_config()
    return {"current": cfg["provider"], "providers": list(cfg["providers"].keys())}


@app.get("/api/ai/variant")
def get_ai_variant():
    cfg = load_ai_config()
    prov = cfg["provider"]
    return {"provider": prov, "variant": cfg["providers"][prov].get("variant", "")}


@app.post("/api/ai/variant")
def set_ai_variant(body: dict):
    variant = (body or {}).get("variant", "").strip()
    cfg = load_ai_config()
    prov = cfg["provider"]
    if not conf_supports_variant(cfg, prov):
        raise HTTPException(400, f"harness {prov} 不支持推理强度设置")
    if variant:
        cfg["providers"][prov]["variant"] = variant
    else:
        cfg["providers"][prov].pop("variant", None)
    save_ai_config(cfg)
    return {"ok": True, "provider": prov, "variant": variant}


def conf_supports_variant(cfg, prov):
    return bool(cfg["providers"][prov].get("variant_flag"))


@app.get("/api/ai/model")
def get_ai_model():
    cfg = load_ai_config()
    prov = cfg["provider"]
    conf = cfg["providers"][prov]
    return {"provider": prov, "model": conf.get("model"), "editable": "model" in conf or prov == "opencode"}


@app.get("/api/ai/models")
def list_ai_models(provider: str = ""):
    cfg = load_ai_config()
    prov = provider or cfg["provider"]
    if prov not in cfg["providers"]:
        raise HTTPException(404, "未知的 AI harness")
    if prov != "opencode":
        return {"provider": prov, "models": []}
    cmd = cfg["providers"][prov].get("cmd")
    if not isinstance(cmd, list) or not cmd or not isinstance(cmd[0], str) or not cmd[0]:
        raise HTTPException(500, "OpenCode 缺少命令行工具配置")
    child_env = {
        "HOME": os.environ.get("HOME", "/tmp"),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    }
    try:
        result = subprocess.run(
            [cmd[0], "models"], capture_output=True, text=True,
            cwd=BASE, env=child_env, timeout=20,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "获取 OpenCode 模型列表超时")
    except FileNotFoundError:
        raise HTTPException(502, "未找到 OpenCode 命令行工具")
    except OSError:
        raise HTTPException(502, "无法启动 OpenCode 命令行工具")
    if result.returncode != 0:
        raise HTTPException(502, "OpenCode 模型列表读取失败")
    output = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout)
    models = [line.strip() for line in output.splitlines() if line.strip()]
    if not models:
        raise HTTPException(502, "OpenCode 没有返回可选模型，请先配置模型服务")
    if any(not re.fullmatch(r"[^/\s]+/\S+", model) for model in models):
        raise HTTPException(502, "OpenCode 返回了无法识别的模型列表")
    return {"provider": prov, "models": sorted(set(models))}


@app.post("/api/ai/model")
def set_ai_model(body: dict):
    model = (body or {}).get("model", "").strip()
    if not model:
        raise HTTPException(400, "missing model")
    cfg = load_ai_config()
    prov = cfg["provider"]
    if body.get("provider") and body["provider"] != prov:
        raise HTTPException(409, "AI harness 已切换，请重新加载模型配置")
    if "model" not in cfg["providers"][prov] and prov != "opencode":
        raise HTTPException(400, f"harness {prov} 不支持切换模型")
    cfg["providers"][prov]["model"] = model
    save_ai_config(cfg)
    return {"ok": True, "provider": prov, "model": model}


@app.post("/api/ai/provider")
def set_ai_provider(body: dict):
    p = (body or {}).get("provider")
    if not p:
        raise HTTPException(400, "missing provider")
    save_ai_provider(p)
    return {"ok": True, "provider": p}


@app.middleware("http")
async def no_cache_static(request, call_next):
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.endswith((".html", ".js", ".css")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
