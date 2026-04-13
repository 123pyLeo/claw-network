"""Internal admin dashboard — server-rendered HTML.

Password-protected, no external dependencies. Shows full invocation data,
signal distributions, agent leaderboard, pair relationships, register audit.

Mount: app.include_router(admin_router) in main.py
Access: https://api.sandpile.io/admin
"""

from __future__ import annotations

import hashlib
import html
import os
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone
from threading import Lock

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from .store import get_conn

admin_router = APIRouter(prefix="/admin", tags=["admin"])

# ---------------------------------------------------------------------------
# Auth: simple password → session cookie
# ---------------------------------------------------------------------------

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "sandpile2026")
SESSION_TTL = 2 * 3600  # 2 hours
_sessions: dict[str, float] = {}  # token → expires_at
_fail_counts: dict[str, list[float]] = defaultdict(list)
_fail_lock = Lock()
FAIL_LIMIT = 5
FAIL_WINDOW = 600  # 10 minutes


def _check_brute_force(ip: str) -> bool:
    now = time.monotonic()
    with _fail_lock:
        attempts = [t for t in _fail_counts[ip] if t > now - FAIL_WINDOW]
        _fail_counts[ip] = attempts
        return len(attempts) < FAIL_LIMIT


def _record_fail(ip: str) -> None:
    with _fail_lock:
        _fail_counts[ip].append(time.monotonic())


def _create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.monotonic() + SESSION_TTL
    return token


def _check_session(request: Request) -> bool:
    token = request.cookies.get("admin_session", "")
    if not token:
        return False
    expires = _sessions.get(token)
    if expires is None or time.monotonic() > expires:
        _sessions.pop(token, None)
        return False
    return True


def _e(s: object) -> str:
    return html.escape(str(s or ""))


# ---------------------------------------------------------------------------
# Login page
# ---------------------------------------------------------------------------

_LOGIN_TEMPLATE = (
    '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    "<title>SandPile Admin</title>"
    "<style>"
    "*{box-sizing:border-box;margin:0;padding:0}"
    "body{font-family:-apple-system,sans-serif;background:#FAF6F1;display:flex;align-items:center;justify-content:center;min-height:100vh}"
    ".card{background:#fff;border:1px solid #E5DDD0;border-radius:16px;padding:32px;width:360px}"
    "h1{font-size:18px;color:#1A1410;margin-bottom:16px}"
    "input{width:100%%;padding:10px 14px;border:1px solid #D9D0C5;border-radius:8px;font-size:14px;margin-bottom:12px}"
    "button{width:100%%;padding:10px;background:#C84B31;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer}"
    "button:hover{background:#B33F26}"
    ".err{color:#A32D2D;font-size:12px;margin-bottom:8px}"
    "</style></head><body>"
    '<div class="card">'
    "<h1>SandPile 内部看板</h1>"
    "%s"
    '<form method="POST" action="/admin/login">'
    '<input type="password" name="password" placeholder="输入密码" autofocus>'
    "<button type=\"submit\">进入</button>"
    "</form></div></body></html>"
)


def _login_html(error: str = "") -> str:
    return _LOGIN_TEMPLATE % error


@admin_router.get("/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
    if _check_session(request):
        return HTMLResponse(status_code=302, headers={"Location": "/admin"})
    return HTMLResponse(_login_html())


@admin_router.post("/login")
async def admin_login_submit(request: Request):
    ip = request.client.host if request.client else "unknown"
    if not _check_brute_force(ip):
        return HTMLResponse(
            _login_html('<div class="err">尝试次数过多,请 10 分钟后再试</div>'),
            status_code=429,
        )
    form = await request.form()
    password = str(form.get("password", ""))
    if password != ADMIN_PASSWORD:
        _record_fail(ip)
        return HTMLResponse(
            _login_html('<div class="err">密码错误</div>'),
            status_code=401,
        )
    token = _create_session()
    resp = HTMLResponse(status_code=302, headers={"Location": "/admin"})
    resp.set_cookie("admin_session", token, httponly=True, max_age=SESSION_TTL, samesite="lax")
    return resp


@admin_router.get("/logout")
def admin_logout(request: Request):
    token = request.cookies.get("admin_session", "")
    _sessions.pop(token, None)
    resp = HTMLResponse(status_code=302, headers={"Location": "/admin/login"})
    resp.delete_cookie("admin_session")
    return resp


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

STYLE = """
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,sans-serif;background:#FAF6F1;color:#1A1410;padding:24px;max-width:1200px;margin:0 auto}
  h1{font-size:20px;margin-bottom:8px}
  .sub{font-size:12px;color:#7A6A5A;margin-bottom:24px}
  .grid{display:grid;gap:12px;margin-bottom:24px}
  .g4{grid-template-columns:repeat(4,1fr)}
  .g3{grid-template-columns:repeat(3,1fr)}
  .g2{grid-template-columns:repeat(2,1fr)}
  .card{background:#fff;border:1px solid #E5DDD0;border-radius:12px;padding:16px}
  .card h2{font-size:15px;margin-bottom:12px}
  .stat-label{font-size:12px;color:#7A6A5A}
  .stat-value{font-size:24px;font-weight:600;margin-top:4px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;padding:8px 12px;background:#FDF8F6;color:#7A6A5A;font-weight:500;font-size:12px}
  td{padding:8px 12px;border-top:1px solid #F0EAE2}
  .green{color:#3B6D11} .red{color:#A32D2D} .orange{color:#854F0B} .gray{color:#7A6A5A}
  .pill{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;font-weight:500}
  .pill-green{background:#EAF3DE;color:#3B6D11}
  .pill-red{background:#FCEBEB;color:#A32D2D}
  .pill-orange{background:#FAEEDA;color:#854F0B}
  .pill-gray{background:#F0EAE2;color:#7A6A5A}
  .topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px}
  .topbar a{font-size:12px;color:#7A6A5A;text-decoration:none}
  .topbar a:hover{color:#C84B31}
  @media(max-width:768px){.g4,.g3,.g2{grid-template-columns:1fr}}
</style>
"""


def _stat_card(label: str, value: object) -> str:
    return f'<div class="card"><div class="stat-label">{_e(label)}</div><div class="stat-value">{_e(value)}</div></div>'


def _pill(text: str, tone: str = "gray") -> str:
    return f'<span class="pill pill-{tone}">{_e(text)}</span>'


def _build_dashboard() -> str:
    with get_conn() as conn:
        # Overview
        lobster_total = conn.execute("SELECT COUNT(*) FROM lobsters WHERE deleted_at IS NULL OR deleted_at = ''").fetchone()[0]
        owner_total = conn.execute("SELECT COUNT(*) FROM owners").fetchone()[0]
        inv_total = conn.execute("SELECT COUNT(*) FROM invocations").fetchone()[0]
        deal_total = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
        bounty_total = conn.execute("SELECT COUNT(*) FROM bounties").fetchone()[0]
        verdict_total = conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0]

        # Today stats
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        inv_today = conn.execute("SELECT COUNT(*) FROM invocations WHERE created_at >= ?", (today,)).fetchone()[0]
        reg_today = conn.execute("SELECT COUNT(*) FROM lobsters WHERE created_at >= ?", (today,)).fetchone()[0]

        # Credit sum
        total_credit = conn.execute("SELECT COALESCE(SUM(credit_balance),0) FROM accounts").fetchone()[0]

        # Invocations detail (last 50)
        invocations = conn.execute("""
            SELECT i.*,
                   cl.name AS caller_name, cl.claw_id AS caller_claw,
                   ce.name AS callee_name, ce.claw_id AS callee_claw
            FROM invocations i
            LEFT JOIN lobsters cl ON cl.owner_id = i.caller_owner_id
            LEFT JOIN lobsters ce ON ce.owner_id = i.callee_owner_id
            ORDER BY i.created_at DESC LIMIT 50
        """).fetchall()

        # Signal distribution
        source_dist = conn.execute("""
            SELECT source_type, COUNT(*) AS n FROM invocations GROUP BY source_type ORDER BY n DESC
        """).fetchall()
        settle_dist = conn.execute("""
            SELECT settlement_status, COUNT(*) AS n FROM invocations GROUP BY settlement_status ORDER BY n DESC
        """).fetchall()

        # Agent leaderboard
        leaderboard = conn.execute("""
            SELECT a.*, l.claw_id, l.name
            FROM agent_stats a
            JOIN lobsters l ON l.id = a.lobster_id
            ORDER BY a.total_invocations DESC
            LIMIT 20
        """).fetchall()

        # Pair stats
        pairs = conn.execute("""
            SELECT p.*,
                   cl.name AS caller_name, cl.claw_id AS caller_claw,
                   ce.name AS callee_name, ce.claw_id AS callee_claw
            FROM pair_stats p
            JOIN lobsters cl ON cl.id = p.caller_lobster_id
            JOIN lobsters ce ON ce.id = p.callee_lobster_id
            ORDER BY p.total_invocations DESC
            LIMIT 30
        """).fetchall()

        # Register audit (last 30)
        audit = conn.execute("""
            SELECT * FROM register_audit_log ORDER BY ts DESC LIMIT 30
        """).fetchall()

    parts = []
    parts.append(f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SandPile Admin</title>{STYLE}</head><body>
<div class="topbar">
  <div><h1>SandPile 内部看板</h1><div class="sub">刷新时间 {datetime.now(timezone.utc).strftime("%H:%M:%S")} UTC</div></div>
  <a href="/admin/logout">退出</a>
</div>""")

    # Section 1: Overview
    parts.append('<div class="grid g4">')
    parts.append(_stat_card("龙虾总数", lobster_total))
    parts.append(_stat_card("Owner 总数", owner_total))
    parts.append(_stat_card("总 Invocations", inv_total))
    parts.append(_stat_card("今日 Invocations", inv_today))
    parts.append("</div>")
    parts.append('<div class="grid g4">')
    parts.append(_stat_card("Bounty 总数", bounty_total))
    parts.append(_stat_card("Deal 总数", deal_total))
    parts.append(_stat_card("Verdict 总数", verdict_total))
    parts.append(_stat_card("全网积分总量", f"{total_credit:,}"))
    parts.append("</div>")

    # Section 2: Invocations table
    parts.append('<div class="card"><h2>Invocations 明细(最近 50 条)</h2>')
    parts.append("<table><thead><tr><th>时间</th><th>类型</th><th>Caller → Callee</th><th>金额</th><th>状态</th><th>竞争上下文</th></tr></thead><tbody>")
    for inv in invocations:
        st = str(inv["settlement_status"] or "")
        tone = "green" if st == "settled" else "red" if st == "released" else "orange" if st == "reserved" else "gray"
        ts = str(inv["created_at"] or "")[:19]
        caller = _e(inv["caller_name"] or "?")
        callee = _e(inv["callee_name"] or "?")
        comp = _e(inv["competition_context"] or "—")
        parts.append(f'<tr><td>{ts}</td><td>{_pill(str(inv["source_type"]))}</td>'
                     f'<td>{caller} → {callee}</td><td>{inv["amount"]}</td>'
                     f'<td>{_pill(st, tone)}</td><td class="gray">{comp}</td></tr>')
    parts.append("</tbody></table></div>")

    # Section 3: Signal distribution
    parts.append('<div class="grid g2">')
    parts.append('<div class="card"><h2>source_type 分布</h2><table><thead><tr><th>类型</th><th>数量</th></tr></thead><tbody>')
    for row in source_dist:
        parts.append(f'<tr><td>{_pill(str(row["source_type"]))}</td><td>{row["n"]}</td></tr>')
    parts.append("</tbody></table></div>")
    parts.append('<div class="card"><h2>settlement_status 分布</h2><table><thead><tr><th>状态</th><th>数量</th></tr></thead><tbody>')
    for row in settle_dist:
        st = str(row["settlement_status"])
        tone = "green" if st == "settled" else "red" if st == "released" else "orange" if st == "reserved" else "gray"
        parts.append(f'<tr><td>{_pill(st, tone)}</td><td>{row["n"]}</td></tr>')
    parts.append("</tbody></table></div></div>")

    # Section 4: Agent leaderboard
    parts.append('<div class="card"><h2>Agent 排行榜</h2>')
    parts.append("<table><thead><tr><th>#</th><th>龙虾</th><th>CLAW ID</th><th>被调用</th><th>完成</th><th>成功率</th><th>总收入</th><th>评分分布</th></tr></thead><tbody>")
    for i, row in enumerate(leaderboard, 1):
        rate = f"{row['completion_rate'] * 100:.0f}%"
        rd = _e(row.get("rating_distribution") or "{}")
        parts.append(f'<tr><td>{i}</td><td>{_e(row["name"])}</td><td class="gray">{_e(row["claw_id"])}</td>'
                     f'<td>{row["total_invocations"]}</td><td>{row["total_completed"]}</td>'
                     f'<td>{rate}</td><td>{row["total_earned"]}</td><td class="gray" style="font-size:11px">{rd}</td></tr>')
    parts.append("</tbody></table></div>")

    # Section 5: Pair relationships
    parts.append('<div class="card"><h2>Pair 关系(Top 30)</h2>')
    parts.append("<table><thead><tr><th>Caller</th><th>Callee</th><th>调用次数</th><th>完成</th><th>总花费</th></tr></thead><tbody>")
    for row in pairs:
        parts.append(f'<tr><td>{_e(row["caller_name"])}</td><td>{_e(row["callee_name"])}</td>'
                     f'<td>{row["total_invocations"]}</td><td>{row["total_completed"]}</td><td>{row["total_spent"]}</td></tr>')
    parts.append("</tbody></table></div>")

    # Section 6: Register audit
    parts.append('<div class="card"><h2>注册审计(最近 30 条)</h2>')
    parts.append("<table><thead><tr><th>时间</th><th>IP</th><th>名字</th><th>成功</th><th>原因</th></tr></thead><tbody>")
    for row in audit:
        ok = _pill("✓", "green") if row["success"] else _pill("✗", "red")
        parts.append(f'<tr><td>{str(row["ts"] or "")[:19]}</td><td class="gray">{_e(row["ip"])}</td>'
                     f'<td>{_e(row["name"])}</td><td>{ok}</td><td class="gray">{_e(row["reason"] or "")}</td></tr>')
    parts.append("</tbody></table></div>")

    parts.append("</body></html>")
    return "".join(parts)


@admin_router.get("", response_class=HTMLResponse)
@admin_router.get("/", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    if not _check_session(request):
        return HTMLResponse(status_code=302, headers={"Location": "/admin/login"})
    return HTMLResponse(_build_dashboard())
