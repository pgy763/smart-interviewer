"""Starlette 应用入口。

为什么是 Starlette 而不是 FastAPI：这台机器的 Python 环境装不上 fastapi
（`pip install` 报 `[Errno 13] Permission denied`，是沙箱对 site-packages 的
写限制，`--user` / 改 TMPDIR 都没用）。Starlette 是 FastAPI 的底座，
Route / WebSocketRoute / Jinja2Templates / StaticFiles 都在，
本项目只需要这些，所以直接用底座，少一层依赖反而更清楚。

路由分层：
- `/`                  面试官端控制台（参数面板 + 历史列表）
- `/interview/{id}`    候选人端（摄像头 + 麦克风 + 问答）
- `/report/{id}`       报告页（HTML）
- `/api/...`           JSON 接口
- `/ws/{id}`           候选人端实时通道（进度的推送用 SSE 更简单，这里用 WS 统一）

**一个刻意的设计**：状态变化全部由 HTTP POST 驱动，WebSocket 只做单向推送
（进度、观察记录、题目）。原因是浏览器 WebSocket 断线重连时的语义很难做对，
而"提交答案"这种关键动作必须是可重试的幂等 HTTP 请求。
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from app import db
from app.api import session as sess
from app.config import DIMENSIONS, STATIC_DIR, TEMPLATE_DIR, settings
from app.providers.llm import estimate_cost, reset_provider

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def _json(data, status: int = 200) -> JSONResponse:
    # Starlette 1.6 的 JSONResponse 内部已经是 json.dumps(ensure_ascii=False)，
    # 这里再传 ensure_ascii 会被当成未知参数直接 TypeError。
    return JSONResponse(data, status_code=status)


def _err(msg: str, status: int = 400, **extra) -> JSONResponse:
    body = {"ok": False, "error": msg}
    body.update(extra)
    return _json(body, status)


def _b64(v) -> bytes:
    """前端传 base64（可能是 data URI）。解不开就当没传，不抛。

    为什么容忍 data URI：前端用 FileReader.readAsDataURL 最省事，它会带着
    `data:application/pdf;base64,` 前缀；但接口文档里写的是裸 base64。
    两种都收，少一个"为什么我传了却解析失败"的排查。
    """
    if not v:
        return b""
    try:
        import base64
        import binascii

        s = str(v).strip()
        if s.startswith("data:") and "," in s:
            s = s.split(",", 1)[1]
        return base64.b64decode(s, validate=False)
    except (binascii.Error, ValueError, TypeError):
        return b""


async def _body(request: Request) -> dict:
    try:
        raw = await request.body()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
async def page_console(request: Request):
    rows = db.list_interviews(30)
    for r in rows:
        try:
            r["config"] = json.loads(r.get("config_json") or "{}")
        except Exception:
            r["config"] = {}
    return templates.TemplateResponse(request, "console.html", {
        "rows": rows,
        "dimensions": DIMENSIONS,
        "provider": settings.resolved_provider(),
        "has_key": bool(settings.dashscope_api_key),
    })


async def page_interview(request: Request):
    iid = request.path_params["iid"]
    row = db.get_interview(iid)
    if not row:
        return HTMLResponse("<h1>面试不存在</h1>", status_code=404)
    try:
        cfg = json.loads(row.get("config_json") or "{}")
    except Exception:
        cfg = {}
    return templates.TemplateResponse(request, "interview.html", {
        "interview": row,
        "config": cfg,
        "dimensions": DIMENSIONS,
    })


async def page_report(request: Request):
    iid = request.path_params["iid"]
    from app.report.builder import build_report

    try:
        rep = build_report(iid)
    except KeyError:
        return HTMLResponse("<h1>面试不存在</h1>", status_code=404)
    return templates.TemplateResponse(request, "report.html", {"r": rep})


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
async def api_create(request: Request):
    body = await _body(request)
    name = (body.get("candidate_name") or "").strip()
    if not name:
        return _err("candidate_name 必填")
    job_role = (body.get("job_role") or "后端开发实习生").strip()
    resume = body.get("resume_text") or ""
    cfg = body.get("config") or {}
    if not isinstance(cfg, dict):
        return _err("config 必须是对象")
    cfg.setdefault("job_role", job_role)

    tenant = (body.get("tenant") or "default").strip() or "default"

    # 配额：能不能再开一场。这里返回 402 而不是 500 —— 额度用尽是业务状态，
    # 不是服务器错误；前端要据此显示"联系管理员"而不是"系统故障"。
    from app.billing.quota import QuotaExceeded, check_quota, consume

    q = check_quota(tenant)
    if not q.allowed:
        return _json({"ok": False, "error": q.reason, "quota": {
            "used": q.used, "limit": q.limit, "period": q.period,
            "plan": q.plan,
        }}, 402)

    # 接入层：简历文件 > 粘贴文本；上一轮报告可选
    from app.providers.llm import get_provider
    from app.intake import build_prior_report, parse_report_json, profile_from_any

    resume_profile: dict = {}
    material: dict = {}
    warnings: list[str] = []
    try:
        prof = profile_from_any(
            text=resume,
            data=_b64(body.get("resume_file_b64")),
            filename=body.get("resume_filename") or "",
            provider=get_provider(),
        )
        resume_profile = prof.to_dict()
        material["resume"] = {"source": prof.source, "chars": prof.raw_chars,
                              "filename": body.get("resume_filename") or ""}
        warnings.extend(prof.warnings or [])
        if not resume:
            resume = prof.prompt_text(limit=1200)
    except Exception as e:  # 简历解析不该让人开不了面试
        warnings.append(f"简历解析未成功：{type(e).__name__}: {e}（已退化为纯文本模式）")

    prior: dict = {}
    raw_prior = body.get("prior_report_json")
    if raw_prior:
        try:
            parsed = parse_report_json(raw_prior if isinstance(raw_prior, str)
                                       else json.dumps(raw_prior, ensure_ascii=False))
            prior = build_prior_report(parsed).to_dict()
            material["prior_report"] = {"source_id": prior.get("source_id")}
        except Exception as e:
            warnings.append(f"上一轮报告解析未成功：{e}")

    try:
        out = sess.manager.create(name, job_role, resume, cfg,
                                  resume_profile=resume_profile,
                                  prior_report=prior,
                                  material=material,
                                  tenant=tenant)
    except ValueError as e:
        return _err(str(e))
    consume(tenant)
    return _json({"ok": True, **out, "warnings": warnings,
                  "resume_profile": resume_profile, "prior_report": prior,
                  "quota": {"used": q.used + 1, "limit": q.limit,
                            "remaining": max(0, q.remaining - 1)}})


async def api_material(request: Request):
    """面试进行中重传简历 / 粘贴上一轮报告。

    为什么单独一个接口而不复用 create：面试已经开跑了，换个候选人来重开会
    丢掉已有问答；而"刚拿到一份更好的简历"是真的会中途发生的事。
    """
    iid = request.path_params["iid"]
    row = db.get_interview(iid)
    if not row:
        return _err("面试不存在", 404)
    body = await _body(request)
    from app.providers.llm import get_provider
    from app.intake import build_prior_report, parse_report_json, profile_from_any

    warnings: list[str] = []
    patch: dict = {}
    if body.get("resume_file_b64") or body.get("resume_filename") or body.get("resume_text"):
        try:
            prof = profile_from_any(
                text=body.get("resume_text") or "",
                data=_b64(body.get("resume_file_b64")),
                filename=body.get("resume_filename") or "",
                provider=get_provider(),
            )
            patch["resume_profile"] = prof.to_dict()
            patch["resume_text"] = body.get("resume_text") or prof.prompt_text(1200)
            warnings.extend(prof.warnings or [])
        except Exception as e:
            return _err(f"简历解析失败：{e}")
    if body.get("prior_report_json"):
        try:
            parsed = parse_report_json(body["prior_report_json"])
            patch["prior_report"] = build_prior_report(parsed).to_dict()
        except Exception as e:
            return _err(f"报告解析失败：{e}")
    if not patch:
        return _err("没有可更新的材料")
    db.update_material(iid, **patch)
    return _json({"ok": True, "updated": list(patch.keys()), "warnings": warnings})


async def api_export(request: Request):
    """导出报告。format=json|md|html，未知值退回 json。"""
    iid = request.path_params["iid"]
    fmt = (request.query_params.get("format") or "json").lower()
    from app.report.builder import build_report
    from app.report import export as exp

    try:
        rep = build_report(iid)
    except KeyError:
        return _err("面试不存在", 404)
    body, ctype, ext = exp.export(rep, fmt)
    from starlette.responses import Response

    resp = Response(body, media_type=ctype)
    # attachment 只在 ?download=1 时加：默认在浏览器里直接看，
    # 要存档再下载。两种场景都有人用，用一个参数分开。
    if request.query_params.get("download") in ("1", "true", "yes"):
        fname = exp.filename(rep, ext)
        resp.headers["Content-Disposition"] = (
            f"attachment; filename*=UTF-8''{__import__('urllib.parse', fromlist=['quote']).quote(fname)}"
        )
    return resp


async def api_quota(request: Request):
    tenant = (request.query_params.get("tenant") or "default").strip()
    from app.billing.quota import check_quota, ensure_tenant, ledger_summary

    ensure_tenant(tenant, monthly_limit=settings.monthly_quota)
    q = check_quota(tenant)
    return _json({"ok": True, **ledger_summary(tenant),
                  "quota": {"allowed": q.allowed, "used": q.used, "limit": q.limit,
                            "remaining": q.remaining, "period": q.period,
                            "plan": q.plan, "reason": q.reason}})


async def api_audit(request: Request):
    tenant = request.query_params.get("tenant")
    from app.billing.quota import list_audit

    return _json({"ok": True, "rows": list_audit(tenant, limit=50)})


async def api_get_interview(request: Request):
    iid = request.path_params["iid"]
    row = db.get_interview(iid)
    if not row:
        return _err("面试不存在", 404)
    s = sess.manager.get(iid)
    turns = db.list_turns(iid)
    pending = next((t for t in turns if not (t.get("answer") or "").strip()), None)
    return _json({
        "ok": True,
        "interview": row,
        "provider": (s.raw_provider.name if s else row.get("provider")),
        "turns": turns,
        "pending": pending,
        "dimensions": DIMENSIONS,
    })


async def api_answer(request: Request):
    """提交一次回答。这是全场最关键的写接口。

    幂等性说明：前端如果因为网络问题重发，会重复 answer_turn 同一 idx
    （UPDATE 是幂等的），但会**多跑一次图**、多产生一轮题。
    所以前端在拿到响应前禁用提交按钮；后端这里也会检查
    "这一轮已经答过了就不再跑图"，直接返回当前状态。
    """
    iid = request.path_params["iid"]
    body = await _body(request)
    answer = body.get("answer") or ""
    audio_ms = int(body.get("audio_ms") or 0)
    s = sess.manager.get(iid)
    if not s:
        return _err("面试不存在", 404)

    turns = db.list_turns(iid)
    # 幂等：如果所有轮次都答过了（没有 pending），说明是重复提交，直接回当前状态
    pending = next((t for t in turns if not (t.get("answer") or "").strip()), None)
    if pending is None and turns:
        return _json({"ok": True, "duplicate": True,
                      "action": "waiting", "question": "", "steps": []})

    try:
        out = s.submit(answer, audio_ms)
    except Exception as e:
        traceback.print_exc()
        return _err(f"处理回答失败：{type(e).__name__}: {e}", 500)
    return _json({"ok": True, **out})


async def api_frame(request: Request):
    iid = request.path_params["iid"]
    body = await _body(request)
    img = body.get("image") or ""
    if not img:
        return _err("image 必填（data URL 或纯 base64）")
    try:
        out = sess.ingest_frame(iid, img)
    except KeyError:
        return _err("面试不存在", 404)
    except Exception as e:
        return _err(f"帧分析失败：{type(e).__name__}: {e}", 500)
    return _json({"ok": True, **out})


async def api_audio(request: Request):
    """音频事件上报。两种来源：
    1. 前端用 Web Speech API 转写后把文本发上来（`kind=transcript`）
    2. 前端记录到停顿/语速（`kind=pause` / `kind=speed`）

    为什么不在服务端做语音识别：那需要把整段音频上传 + 排队识别，
    延迟以秒计，无法支撑"实时分析"。浏览器侧转写（Chrome/Edge 的
    Web Speech API）是零成本、低延迟的；代价是只有 Chrome/Edge 支持。
    这一点在报告局限里写明。真实生产应该用 Qwen-Omni 的 WS 双向流，
    代码在 `app/audio/analyzer.py:OmniSession`，有 key 时可切。
    """
    iid = request.path_params["iid"]
    body = await _body(request)
    kind = body.get("kind") or "transcript"
    if kind not in ("transcript", "pause", "speed", "filler"):
        return _err("kind 非法")
    sess.ingest_audio_event(iid, kind, value=body.get("value"),
                            text=body.get("text"), meta=body.get("meta"))
    return _json({"ok": True})


async def api_adjust(request: Request):
    iid = request.path_params["iid"]
    patch = await _body(request)
    try:
        out = sess.adjust_config(iid, patch)
    except KeyError:
        return _err("面试不存在", 404)
    except ValueError as e:
        return _err(str(e))
    return _json({"ok": True, **out})


async def api_pacing(request: Request):
    iid = request.path_params["iid"]
    try:
        out = sess.pacing_suggestion(iid)
    except KeyError:
        return _err("面试不存在", 404)
    return _json({"ok": True, **out})


async def api_finish(request: Request):
    iid = request.path_params["iid"]
    body = await _body(request)
    reason = (body.get("reason") or "").strip()
    aborted = bool(body.get("aborted"))
    row = db.get_interview(iid)
    if not row:
        return _err("面试不存在", 404)
    # 已经走完流程（答完最后一题时 submit 会把状态置 finished）的场次，
    # 再点一次「结束面试」不能把它改写成 aborted —— 那会让报告显示
    # 「会话被主动终止」，把一场完整面试写成中途退出。
    if row.get("status") == "finished" and not aborted:
        # 即使状态已经是 finished（答完最后一题时 submit 自动置的），也要落一条审计：
        # 客户问「这场什么时候结束的」时，不能因为它自动结束就没有记录。
        from app.billing.quota import audit

        audit("interview.finish", tenant=(body.get("tenant") or "default"),
              interview_id=iid, actor="console",
              detail={"status": "finished", "reason": reason, "noop": True},
              outcome="ok")
        return _json({"ok": True, "status": "finished", "noop": True})
    # 只有明确声明 aborted（候选人关页面、掉线不回来）才算异常终止。
    # 「面试官点结束」是正常流程，不能记成 aborted，否则报告里
    # 每场面试都会挂一条「会话被主动终止」的异常。
    status = "aborted" if aborted else "finished"
    db.set_interview_status(iid, status, ended=True,
                           abort_reason=reason if aborted else None)
    from app.billing.quota import audit

    audit("interview.finish", tenant=(body.get("tenant") or "default"),
          interview_id=iid, actor="console",
          detail={"status": status, "reason": reason},
          outcome="ok" if not aborted else "aborted")
    return _json({"ok": True, "status": status})


async def api_report(request: Request):
    iid = request.path_params["iid"]
    from app.report.builder import build_report

    try:
        rep = build_report(iid)
    except KeyError:
        return _err("面试不存在", 404)
    return _json({"ok": True, "report": rep})


async def api_list(request: Request):
    rows = db.list_interviews(50)
    out = []
    for r in rows:
        try:
            cfg = json.loads(r.get("config_json") or "{}")
        except Exception:
            cfg = {}
        out.append({"id": r["id"], "candidate": r["candidate_name"],
                    "job_role": r["job_role"], "status": r["status"],
                    "provider": r["provider"], "started_at": r["started_at"],
                    "ended_at": r.get("ended_at"),
                    "questions_target": cfg.get("questions_target")})
    return _json({"ok": True, "rows": out})


async def api_cost(request: Request):
    """成本核算。按面试聚合 + 全局总计，单位人民币元。

    这是验收项「成本核算（API 调用次数）」的落地：
    每一次模型调用都写进 llm_call 表（provider / model / purpose /
    input_tokens / output_tokens / audio_ms），这里只做汇总。
    """
    iid = request.query_params.get("interview_id")
    calls = db.list_llm_calls(iid)
    per_interview = {}
    for c in calls:
        k = c.get("interview_id") or "-"
        per_interview.setdefault(k, []).append(c)
    by_iv = {k: estimate_cost(v) for k, v in per_interview.items()}
    return _json({"ok": True, "total": estimate_cost(calls), "by_interview": by_iv,
                  "calls": len(calls)})


async def api_health(request: Request):
    return _json({"ok": True, "provider": settings.resolved_provider(),
                  "has_key": bool(settings.dashscope_api_key),
                  "port": settings.port, "ts": time.time()})


async def api_reset_provider(request: Request):
    """改完环境变量后热切换 provider，不用重启进程。"""
    reset_provider()
    from app.providers.llm import get_provider

    p = get_provider()
    return _json({"ok": True, "provider": p.name})


# ---------------------------------------------------------------------------
# WebSocket：候选人端实时通道
# ---------------------------------------------------------------------------
async def ws_interview(websocket):
    """只做服务端 → 客户端的单向推送（题目 / 进度 / 观察）。

    客户端消息只有一种：心跳 ping。为什么不做双向 RPC：
    答题走 HTTP（可重试、可幂等），这里做 RPC 会让"提交答案"有两条路径，
    断线重连时就会出现"到底提交成功没有"的不确定性。
    """
    iid = websocket.path_params["iid"]
    await websocket.accept()
    s = sess.manager.get(iid)
    if not s:
        await websocket.send_json({"type": "error", "error": "interview not found"})
        await websocket.close()
        return

    async def pump():
        """把会话的最近事件推给前端，1 秒一次。"""
        last_turn = -1
        while True:
            await asyncio.sleep(1.0)
            try:
                turns = db.list_turns(iid)
                if len(turns) != last_turn:
                    last_turn = len(turns)
                    await websocket.send_json({
                        "type": "state",
                        "turns": turns,
                        "fallbacks": s.fallbacks,
                    })
                else:
                    await websocket.send_json({"type": "ping", "ts": time.time()})
            except Exception:
                break

    task = asyncio.create_task(pump())
    try:
        while True:
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_json({"type": "pong", "ts": time.time()})
    except Exception:
        pass
    finally:
        task.cancel()


# ---------------------------------------------------------------------------
# 应用装配
# ---------------------------------------------------------------------------
routes = [
    Route("/", page_console),
    Route("/interview/{iid}", page_interview),
    Route("/report/{iid}", page_report),
    Route("/api/interview", api_create, methods=["POST"]),
    Route("/api/interview/{iid}", api_get_interview),
    Route("/api/interview/{iid}/answer", api_answer, methods=["POST"]),
    Route("/api/interview/{iid}/frame", api_frame, methods=["POST"]),
    Route("/api/interview/{iid}/audio", api_audio, methods=["POST"]),
    Route("/api/interview/{iid}/adjust", api_adjust, methods=["POST"]),
    Route("/api/interview/{iid}/pacing", api_pacing),
    Route("/api/interview/{iid}/finish", api_finish, methods=["POST"]),
    Route("/api/interview/{iid}/report", api_report),
    Route("/api/interview/{iid}/export", api_export),
    Route("/api/interview/{iid}/material", api_material, methods=["POST"]),
    Route("/api/interviews", api_list),
    Route("/api/cost", api_cost),
    Route("/api/quota", api_quota),
    Route("/api/audit", api_audit),
    Route("/api/health", api_health),
    Route("/api/provider/reset", api_reset_provider, methods=["POST"]),
    WebSocketRoute("/ws/{iid}", ws_interview),
    Mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static"),
]


@asynccontextmanager
async def _lifespan(app_):
    """启动时建表 + 初始化 provider（Starlette 1.6 已移除 on_startup 参数，
    只接受 lifespan 上下文管理器；这是 1.x 的破坏性变更之一）。
    """
    db.init_db()
    from app.providers.llm import get_provider

    get_provider()
    yield {}


app = Starlette(
    routes=routes,
    lifespan=_lifespan,
    middleware=[Middleware(CORSMiddleware, allow_origins=["*"],
                           allow_methods=["*"], allow_headers=["*"])],
)
