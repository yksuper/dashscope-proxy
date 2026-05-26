"""
阿里云 DashScope 代理
固定周期窗口：5H(整点对齐) / 自然周(周一重置) / 月度账单周期(可配置重置日，默认1号)
支持 OpenAI 协议（兼容 OpenAI 工具）和 Anthropic 协议（Claude Code）
"""

import os, time, json, secrets, string
import datetime

import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.responses import StreamingResponse
from redis.asyncio import Redis
from pydantic import BaseModel, Field, ConfigDict

# ─────────────────────────────────────────
#  默认配额（每个子Key）
# ─────────────────────────────────────────
DEFAULT_LIMITS = {"5h": 1_500, "week": 11_250, "month": 22_500}
MAX_KEYS = 10

# ─────────────────────────────────────────
#  Pydantic 校验模型
# ─────────────────────────────────────────
class _QuotaFields(BaseModel):
    """month / week / 5h 三维度配额字段，LimitsUpdate 和 UsageUpdate 共用。"""
    model_config = ConfigDict(populate_by_name=True)
    month:     int | None = Field(None, ge=0, le=1_000_000_000, description="月")
    week:      int | None = Field(None, ge=0, le=500_000_000,   description="周")
    five_hour: int | None = Field(None, alias="5h", ge=0, le=100_000_000, description="5小时")

class LimitsUpdate(_QuotaFields):
    """配额限制更新"""

class UsageUpdate(_QuotaFields):
    """用量更新"""

class LabelUpdate(BaseModel):
    """用户标签 + 重置日更新"""
    label:     str = Field(..., max_length=100,  description="用户名称")
    note:      str = Field("",  max_length=1000, description="备注")
    reset_day: int | None = Field(None, ge=1, le=28, description="月度重置日（1-28，None=跟随全局）")

class ResetDayUpdate(BaseModel):
    """月度重置日更新"""
    day: int = Field(..., ge=1, le=28, description="每月几号重置（1-28）")

# ─────────────────────────────────────────
#  Coding Plan 模型白名单
# ─────────────────────────────────────────
PLAN_MODELS: set[str] = {
    "qwen3.5-plus",
    "qwen3-max-2026-01-23",
    "qwen3-coder-next",
    "qwen3-coder-plus",
    "MiniMax-M2.5",
    "glm-5",
    "glm-4.7",
    "kimi-k2.5",
    "qwen3.6-plus",
}

# ─────────────────────────────────────────
#  上游地址 & 认证
# ─────────────────────────────────────────
ALIYUN_BASE    = os.getenv("ALIYUN_BASE_URL", "https://coding.dashscope.aliyuncs.com/v1")
ANTHROPIC_BASE = os.getenv("ANTHROPIC_BASE",  "https://coding.dashscope.aliyuncs.com/apps/anthropic")
ALIYUN_KEY     = os.getenv("ALIYUN_API_KEY",  "")

_admin_token_raw = os.getenv("ADMIN_TOKEN", "change-me")
if _admin_token_raw == "change-me":
    raise RuntimeError("启动失败：请在环境变量中设置 ADMIN_TOKEN，不能使用默认值 'change-me'")
ADMIN_TOKEN = _admin_token_raw

# 月度重置日（1-28），从 Redis 读取后缓存在此；多 worker 时各自缓存，重启后同步
_reset_day: int = int(os.getenv("MONTHLY_RESET_DAY", "1"))

# ─────────────────────────────────────────
#  固定周期 Key 计算
# ─────────────────────────────────────────
_TZ_CST = datetime.timezone(datetime.timedelta(hours=8))  # 北京时间 UTC+8

def period_info(kid: str, reset_day: int | None = None) -> dict:
    """返回当前周期的 Redis key、TTL（EXPIREAT时间戳）、重置时刻（北京时间）
    reset_day 由调用方从 Redis 读取后传入，确保多 worker 一致。
    """
    now = datetime.datetime.now(tz=_TZ_CST).replace(tzinfo=None)  # 强制北京时间
    if reset_day is None:
        reset_day = _reset_day  # fallback 到启动时缓存值

    slot = now.hour // 5
    date_s = now.strftime("%Y%m%d")
    next_slot_hour = (slot + 1) * 5
    if next_slot_hour >= 24:
        next_reset_5h = (now + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
    else:
        next_reset_5h = now.replace(
            hour=next_slot_hour, minute=0, second=0, microsecond=0)

    days_to_monday = 7 - now.weekday()
    next_monday = (now + datetime.timedelta(days=days_to_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    iso = now.isocalendar()

    # 月度账单周期：reset_day 日 00:00 开始，到下次 reset_day 日前结束
    # key 用账单周期开始的年月标识，保证同一周期内的计数在同一个 key 下累积
    if now.day < reset_day:
        # 当前日期还未到本月重置日，仍处于上月账单周期
        prev = now.replace(day=1) - datetime.timedelta(days=1)
        month_period_key = prev.strftime('%Y%m')
        next_month_dt = datetime.datetime(now.year, now.month, reset_day)
    else:
        # 已过本月重置日，处于本月账单周期
        month_period_key = now.strftime('%Y%m')
        if now.month == 12:
            next_month_dt = datetime.datetime(now.year + 1, 1, reset_day)
        else:
            next_month_dt = datetime.datetime(now.year, now.month + 1, reset_day)

    return {
        "5h": {
            "key":       f"quota:5h:{kid}:{date_s}:{slot}",
            "expire_at": int(next_reset_5h.timestamp()),
            "reset_at":  next_reset_5h.strftime("%Y-%m-%d %H:%M"),
            "label":     f"{slot*5:02d}:00–{min((slot+1)*5,24):02d}:00",
        },
        "week": {
            "key":       f"quota:week:{kid}:{iso[0]}W{iso[1]:02d}",
            "expire_at": int(next_monday.timestamp()),
            "reset_at":  next_monday.strftime("%Y-%m-%d %H:%M"),
            "label":     f"第{iso[1]}周",
        },
        "month": {
            "key":       f"quota:month:{kid}:{month_period_key}",
            "expire_at": int(next_month_dt.timestamp()),
            "reset_at":  next_month_dt.strftime("%Y-%m-%d %H:%M"),
            "label":     now.strftime("%Y年%m月"),
        },
    }

# ─────────────────────────────────────────
#  Lua
# ─────────────────────────────────────────
LUA_CHECK = """
local k5h  = KEYS[1]; local kw  = KEYS[2]; local km  = KEYS[3]
local l5h  = tonumber(ARGV[1]); local lw = tonumber(ARGV[2]); local lm = tonumber(ARGV[3])
local e5h  = tonumber(ARGV[4]); local ew = tonumber(ARGV[5]); local em = tonumber(ARGV[6])

local cm = tonumber(redis.call('GET', km)) or 0
if cm >= lm then return 'MONTH_LIMIT' end
local cw = tonumber(redis.call('GET', kw)) or 0
if cw >= lw then return 'WEEK_LIMIT' end
local c5h = tonumber(redis.call('GET', k5h)) or 0
if c5h >= l5h then return '5H_LIMIT' end

local nm = redis.call('INCR', km)
if nm == 1 then redis.call('EXPIREAT', km, em) end
local nw = redis.call('INCR', kw)
if nw == 1 then redis.call('EXPIREAT', kw, ew) end
local n5h = redis.call('INCR', k5h)
if n5h == 1 then redis.call('EXPIREAT', k5h, e5h) end
return 'OK'
"""

LUA_MIGRATE_MONTH = """
-- 修改重置日时迁移当前月度计数器
-- KEYS[1]=旧key, KEYS[2]=新key, ARGV[1]=新过期时间戳
local old_k = KEYS[1]; local new_k = KEYS[2]
local new_expire = tonumber(ARGV[1])
if old_k == new_k then
    -- key 相同，只更新过期时间
    if redis.call('EXISTS', old_k) == 1 then
        redis.call('EXPIREAT', old_k, new_expire)
    end
else
    -- key 不同，把旧值合并进新 key
    local old_val = tonumber(redis.call('GET', old_k)) or 0
    if old_val > 0 then
        redis.call('INCRBY', new_k, old_val)
        redis.call('EXPIREAT', new_k, new_expire)
        redis.call('DEL', old_k)
    end
end
return 'OK'
"""

LUA_ROLLBACK = """
local function decr_safe(k)
    local v = tonumber(redis.call('GET', k)) or 0
    if v > 0 then redis.call('DECR', k) end
end
decr_safe(KEYS[1]); decr_safe(KEYS[2]); decr_safe(KEYS[3])
return 'OK'
"""

# ─────────────────────────────────────────
#  App
# ─────────────────────────────────────────
app = FastAPI(title="DashScope Proxy", docs_url=None, redoc_url=None)
rdb: Redis = None
_http_client: httpx.AsyncClient = None  # 全局复用，避免每次请求重建 TCP/TLS 连接


@app.on_event("startup")
async def startup():
    global rdb, _http_client, _reset_day
    rdb = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True)
    _http_client = httpx.AsyncClient(timeout=None)
    # 月度重置日：env 提供初始值，Redis 存储运行时修改值（nx=True 首次写入不覆盖已有）
    env_day = max(1, min(28, int(os.getenv("MONTHLY_RESET_DAY", "1"))))
    await rdb.set("config:monthly_reset_day", env_day, nx=True)
    stored = await rdb.get("config:monthly_reset_day")
    _reset_day = int(stored) if stored else env_day
    existing = await _scan_all_kids()
    if not existing:
        # 首次启动：创建 k1-k4 作为兼容
        for i in range(1, 5):
            kid = f"k{i}"
            secret = os.getenv(f"KEY_{i}", "sk-sub-" + "".join(
                secrets.choice(string.ascii_lowercase + string.digits) for _ in range(16)))
            meta = {
                "kid": kid, "label": f"用户{i}", "secret": secret,
                "enabled": True, "limits": {"5h": None, "week": None, "month": None},
                "note": "", "created_at": int(time.time()), "reset_day": _reset_day,
            }
            await rdb.set(f"key:meta:{kid}", json.dumps(meta, ensure_ascii=False), nx=True)
        await rdb.set("config:next_kid_num", 5, nx=True)
    await _rebuild_secret_map()


@app.on_event("shutdown")
async def shutdown():
    if _http_client:
        await _http_client.aclose()


async def _get_reset_day() -> int:
    """从 Redis 读取当前月度重置日，确保多 worker 一致。"""
    stored = await rdb.get("config:monthly_reset_day")
    return int(stored) if stored else _reset_day


async def _rebuild_secret_map():
    """从所有 key:meta:* 重建 secret→kid 映射表。仅在 Key secret 变更时调用。"""
    kids = await _scan_all_kids()
    if not kids:
        return
    pipe = rdb.pipeline()
    for kid in kids:
        pipe.get(f"key:meta:{kid}")
    raws = await pipe.execute()
    mapping = {json.loads(r)["secret"]: json.loads(r)["kid"] for r in raws if r}
    if mapping:
        await rdb.delete("map:secret")
        await rdb.hset("map:secret", mapping=mapping)


async def _scan_all_kids() -> list[str]:
    """Scan Redis for all existing key:meta:* entries, return sorted kid list."""
    kids = []
    async for k in rdb.scan_iter(match="key:meta:*", count=100):
        kids.append(k.removeprefix("key:meta:"))
    def _sort_key(kid: str):
        num = kid[1:]
        return int(num) if num.isdigit() else 999999
    kids.sort(key=_sort_key)
    return kids


async def _get_next_kid_num() -> int:
    """Get next kid number from Redis counter. Falls back to scanning if missing."""
    n = await rdb.get("config:next_kid_num")
    if n is not None:
        return int(n)
    kids = await _scan_all_kids()
    if not kids:
        return 1
    max_num = 0
    for kid in kids:
        num = kid[1:]
        if num.isdigit():
            max_num = max(max_num, int(num))
    return max_num + 1


async def _get_meta(kid: str) -> dict | None:
    raw = await rdb.get(f"key:meta:{kid}")
    return json.loads(raw) if raw else None


async def _save_meta(meta: dict, rebuild_map: bool = False):
    """保存 meta。仅 secret 变更（regenerate）时需要 rebuild_map=True。"""
    await rdb.set(f"key:meta:{meta['kid']}", json.dumps(meta, ensure_ascii=False))
    if rebuild_map:
        await _rebuild_secret_map()


def _limits(meta: dict) -> dict:
    return {k: (meta["limits"].get(k) or DEFAULT_LIMITS[k]) for k in ("5h", "week", "month")}


async def _usage(kid: str) -> dict:
    meta = await _get_meta(kid)
    rd = (meta or {}).get("reset_day") or _reset_day
    pi = period_info(kid, rd)
    vals = await rdb.mget(pi["5h"]["key"], pi["week"]["key"], pi["month"]["key"])
    return {
        "5h":    int(vals[0]) if vals[0] else 0,
        "week":  int(vals[1]) if vals[1] else 0,
        "month": int(vals[2]) if vals[2] else 0,
    }


def _is_plan_model(body: bytes) -> bool:
    """请求体中的模型在 Coding Plan 白名单内则返回 True（需计入配额）。
    无法解析或未指定模型时默认返回 True。"""
    if not body:
        return True
    try:
        model = json.loads(body).get("model", "")
        return not model or model in PLAN_MODELS
    except Exception:
        return True


# ─────────────────────────────────────────
#  静态页面
# ─────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/_panel/admin")
async def admin_page(): return FileResponse("static/admin.html")

@app.get("/_panel/usage")
async def user_page(): return FileResponse("static/user.html")


# ─────────────────────────────────────────
#  管理 API
# ─────────────────────────────────────────
def _check_admin(request: Request):
    if request.headers.get("X-Admin-Token", "") != ADMIN_TOKEN:
        raise HTTPException(status_code=403)


def _mask_secret(secret: str) -> str:
    """脱敏显示 secret：保留前缀和后4位"""
    if len(secret) <= 8:
        return "****"
    return f"{secret[:7]}****{secret[-4:]}"


@app.get("/_admin/keys")
async def admin_list_keys(request: Request):
    _check_admin(request)

    # 两轮 pipeline：先拉全部 meta，再批量拉 usage 计数器
    kids = await _scan_all_kids()
    pipe = rdb.pipeline()
    for kid in kids:
        pipe.get(f"key:meta:{kid}")
    metas = [json.loads(r) for r in await pipe.execute() if r]

    rd_global = await _get_reset_day()
    period_infos = []
    for m in metas:
        child_rd = m.get("reset_day") or rd_global
        period_infos.append(period_info(m["kid"], child_rd))
    pipe = rdb.pipeline()
    for pi in period_infos:
        for dim in ("5h", "week", "month"):
            pipe.get(pi[dim]["key"])
    usage_vals = await pipe.execute()

    result = []
    for idx, meta in enumerate(metas):
        pi   = period_infos[idx]
        lims = _limits(meta)
        base = idx * 3
        used = {
            "5h":    int(usage_vals[base])   if usage_vals[base]   else 0,
            "week":  int(usage_vals[base+1]) if usage_vals[base+1] else 0,
            "month": int(usage_vals[base+2]) if usage_vals[base+2] else 0,
        }
        result.append({
            **{k: v for k, v in meta.items() if k != "secret"},
            "secret_preview": _mask_secret(meta["secret"]),
            "limits_effective": lims,
            "usage": used,
            "pct":   {k: round(used[k] / lims[k] * 100, 1) for k in lims},
            "period_info": {k: pi[k]["reset_at"] for k in pi},
            "reset_day": meta.get("reset_day"),
        })
    return result


@app.post("/_admin/keys/{kid}/toggle")
async def admin_toggle(kid: str, request: Request):
    _check_admin(request)
    meta = await _get_meta(kid)
    if not meta: raise HTTPException(404)
    meta["enabled"] = not meta["enabled"]
    await _save_meta(meta)
    return {"kid": kid, "enabled": meta["enabled"]}


@app.post("/_admin/keys/{kid}/regenerate")
async def admin_regenerate(kid: str, request: Request):
    _check_admin(request)
    meta = await _get_meta(kid)
    if not meta: raise HTTPException(404)
    meta["secret"] = "sk-sub-" + "".join(
        secrets.choice(string.ascii_lowercase + string.digits) for _ in range(24))
    await _save_meta(meta, rebuild_map=True)  # secret 变了，必须重建映射
    return {"kid": kid, "secret": meta["secret"]}


@app.get("/_admin/keys/{kid}/secret")
async def admin_reveal_secret(kid: str, request: Request):
    """获取完整 secret（敏感操作，需要 admin token）"""
    _check_admin(request)
    meta = await _get_meta(kid)
    if not meta: raise HTTPException(404)
    return {"kid": kid, "secret": meta["secret"]}


@app.put("/_admin/keys/{kid}/limits")
async def admin_set_limits(kid: str, body: LimitsUpdate, request: Request):
    _check_admin(request)
    meta = await _get_meta(kid)
    if not meta: raise HTTPException(404)
    if body.month     is not None: meta["limits"]["month"] = body.month
    if body.week      is not None: meta["limits"]["week"]  = body.week
    if body.five_hour is not None: meta["limits"]["5h"]    = body.five_hour
    await _save_meta(meta)
    return {"kid": kid, "limits": meta["limits"], "effective": _limits(meta)}


@app.put("/_admin/keys/{kid}/label")
async def admin_set_label(kid: str, body: LabelUpdate, request: Request):
    _check_admin(request)
    meta = await _get_meta(kid)
    if not meta: raise HTTPException(404)
    meta["label"] = body.label
    meta["note"]  = body.note
    if body.reset_day is not None:
        meta["reset_day"] = body.reset_day
    elif "reset_day" not in meta:
        meta["reset_day"] = None
    await _save_meta(meta)
    return {"kid": kid, "label": meta["label"], "note": meta["note"], "reset_day": meta.get("reset_day")}


@app.post("/_admin/keys")
async def admin_create_key(request: Request):
    _check_admin(request)
    kids = await _scan_all_kids()
    if len(kids) >= MAX_KEYS:
        raise HTTPException(400, f"已达到最大子Key数量限制 ({MAX_KEYS})")

    next_num = await _get_next_kid_num()
    kid = f"k{next_num}"

    if await _get_meta(kid):
        raise HTTPException(409, f"Key {kid} already exists")

    secret = "sk-sub-" + "".join(
        secrets.choice(string.ascii_lowercase + string.digits) for _ in range(24))
    global_rd = await _get_reset_day()
    meta = {
        "kid": kid, "label": f"用户{next_num}", "secret": secret,
        "enabled": True, "limits": {"5h": None, "week": None, "month": None},
        "note": "", "created_at": int(time.time()), "reset_day": global_rd,
    }
    await _save_meta(meta)
    await rdb.set("config:next_kid_num", next_num + 1)
    await _rebuild_secret_map()

    return {**{k: v for k, v in meta.items() if k != "secret"}, "secret": secret}


@app.delete("/_admin/keys/{kid}")
async def admin_delete_key(kid: str, request: Request):
    _check_admin(request)
    meta = await _get_meta(kid)
    if not meta:
        raise HTTPException(404, f"Key {kid} not found")

    kids = await _scan_all_kids()
    if len(kids) <= 1:
        raise HTTPException(400, "不能删除最后一个子Key")

    rd = await _get_reset_day()
    pi = period_info(kid, rd)
    await rdb.delete(pi["5h"]["key"], pi["week"]["key"], pi["month"]["key"])

    await rdb.hdel("map:secret", meta["secret"])
    await rdb.delete(f"key:meta:{kid}")

    return {"kid": kid, "deleted": True}


@app.put("/_admin/keys/{kid}/usage")
async def admin_set_usage(kid: str, body: UsageUpdate, request: Request):
    """手动修改当前周期的已用量"""
    _check_admin(request)
    meta = await _get_meta(kid)
    if not meta: raise HTTPException(404)
    rd = (meta or {}).get("reset_day") or await _get_reset_day()
    pi   = period_info(kid, rd)
    pipe = rdb.pipeline()
    if body.month     is not None:
        pipe.set(pi["month"]["key"], body.month)
        pipe.expireat(pi["month"]["key"], pi["month"]["expire_at"])
    if body.week      is not None:
        pipe.set(pi["week"]["key"], body.week)
        pipe.expireat(pi["week"]["key"], pi["week"]["expire_at"])
    if body.five_hour is not None:
        pipe.set(pi["5h"]["key"], body.five_hour)
        pipe.expireat(pi["5h"]["key"], pi["5h"]["expire_at"])
    await pipe.execute()
    return {"kid": kid, "usage": await _usage(kid)}


@app.delete("/_admin/keys/{kid}/reset-usage")
async def admin_reset_usage(kid: str, request: Request):
    _check_admin(request)
    meta = await _get_meta(kid)
    if not meta: raise HTTPException(404)
    rd = meta.get("reset_day") or await _get_reset_day()
    pi = period_info(kid, rd)
    await rdb.delete(pi["5h"]["key"], pi["week"]["key"], pi["month"]["key"])
    return {"kid": kid, "reset": True}


@app.get("/_admin/config")
async def admin_get_config(request: Request):
    """获取系统配置（直接读 Redis，多 worker 一致）"""
    _check_admin(request)
    return {"monthly_reset_day": await _get_reset_day()}


@app.put("/_admin/config/reset-day")
async def admin_set_reset_day(body: ResetDayUpdate, request: Request):
    """修改月度重置日：迁移跟随全局的子账号计数器后生效，已用量不丢失。"""
    global _reset_day
    _check_admin(request)
    old_day = await _get_reset_day()
    new_day = body.day
    if old_day != new_day:
        kids = await _scan_all_kids()
        for kid in kids:
            meta = await _get_meta(kid)
            if not meta:
                continue
            # 只有 reset_day 跟随全局的子账号才迁移
            rd = meta.get("reset_day") or old_day
            if rd != old_day:
                continue
            old_pi = period_info(kid, old_day)
            new_pi = period_info(kid, new_day)
            await rdb.eval(
                LUA_MIGRATE_MONTH, 2,
                old_pi["month"]["key"], new_pi["month"]["key"],
                new_pi["month"]["expire_at"],
            )
            # 更新子账号的 reset_day 为 None，保持跟随全局
            meta["reset_day"] = None
            await _save_meta(meta)
    await rdb.set("config:monthly_reset_day", new_day)
    _reset_day = new_day
    return {"monthly_reset_day": new_day}


# ─────────────────────────────────────────
#  用户用量 API
# ─────────────────────────────────────────
@app.get("/_usage")
async def user_usage(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "): raise HTTPException(401)
    secret = auth.removeprefix("Bearer ").strip()
    kid = await rdb.hget("map:secret", secret)
    if not kid: raise HTTPException(401, "Invalid key")
    meta = await _get_meta(kid)
    lims = _limits(meta)
    used = await _usage(kid)
    rd = meta.get("reset_day") or await _get_reset_day()
    pi   = period_info(kid, rd)
    return {
        "label": meta["label"], "kid": kid, "secret": secret,
        "enabled": meta["enabled"],
        "limits": lims, "usage": used,
        "pct":    {k: round(used[k] / lims[k] * 100, 1) for k in lims},
        "reset_day": rd,
        "reset_at":     {k: pi[k]["reset_at"] for k in pi},
        "period_label": {k: pi[k]["label"]    for k in pi},
        "updated_at": int(time.time()),
    }


# ─────────────────────────────────────────
#  配额检查公共逻辑
# ─────────────────────────────────────────
async def _check_and_deduct_quota(kid: str, meta: dict) -> tuple:
    """原子检查并扣除三维配额，返回 (k5h, kw, km) 供回滚用。不足则抛 429。"""
    lims = _limits(meta)
    rd = meta.get("reset_day") or await _get_reset_day()
    pi   = period_info(kid, rd)
    k5h, kw, km = pi["5h"]["key"], pi["week"]["key"], pi["month"]["key"]
    e5h, ew, em = pi["5h"]["expire_at"], pi["week"]["expire_at"], pi["month"]["expire_at"]

    res = await rdb.eval(
        LUA_CHECK, 3, k5h, kw, km,
        lims["5h"], lims["week"], lims["month"],
        e5h, ew, em,
    )
    if res != "OK":
        msgs = {
            "5H_LIMIT":    f"5小时配额已用尽，重置于 {pi['5h']['reset_at']}",
            "WEEK_LIMIT":  f"本周配额已用尽，重置于 {pi['week']['reset_at']}",
            "MONTH_LIMIT": f"本月配额已用尽，重置于 {pi['month']['reset_at']}",
        }
        raise HTTPException(429, msgs.get(res, res))
    return (k5h, kw, km)


# ─────────────────────────────────────────
#  主代理
# ─────────────────────────────────────────
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(path: str, request: Request):
    x_api_key   = request.headers.get("x-api-key", "").strip()
    auth_header = request.headers.get("Authorization", "")

    is_anthropic_path = (path == "v1/messages" or path.startswith("v1/messages?")
                         or path == "messages"  or path.startswith("messages?"))

    if x_api_key:
        secret = x_api_key
    elif auth_header.startswith("Bearer "):
        secret = auth_header.removeprefix("Bearer ").strip()
    else:
        raise HTTPException(401, "Missing Authorization header")

    protocol = "anthropic" if (x_api_key or is_anthropic_path) else "openai"
    return await _handle_proxy(request, path, secret, protocol)


async def _handle_proxy(request: Request, path: str, secret: str, protocol: str):
    """统一代理处理：验证 Key → 检查配额 → 转发上游。"""
    kid = await rdb.hget("map:secret", secret)
    if not kid: raise HTTPException(401, "Invalid API key")
    meta = await _get_meta(kid)
    if not meta or not meta["enabled"]: raise HTTPException(403, "Key is disabled")

    body = await request.body()

    if protocol == "openai":
        upstream_path = path[3:] if path.startswith("v1/") else path
        upstream_url  = f"{ALIYUN_BASE}/{upstream_path}"
        strip         = {"host", "authorization", "content-length",
                         "transfer-encoding", "connection", "keep-alive"}
        upstream_headers = {k: v for k, v in request.headers.items() if k.lower() not in strip}
        upstream_headers["Authorization"] = f"Bearer {ALIYUN_KEY}"
    else:  # anthropic
        upstream_url  = f"{ANTHROPIC_BASE}/{path}"
        strip         = {"host", "x-api-key", "authorization", "content-length",
                         "transfer-encoding", "connection", "keep-alive"}
        upstream_headers = {k: v for k, v in request.headers.items() if k.lower() not in strip}
        upstream_headers["x-api-key"] = ALIYUN_KEY

    quota_keys = await _check_and_deduct_quota(kid, meta) if _is_plan_model(body) else None
    return await _forward(request, upstream_url, upstream_headers, body, quota_keys)


async def _forward(request: Request, upstream_url: str,
                   headers: dict, body: bytes,
                   quota_keys: tuple | None):
    """透传到上游，自动处理流式/非流式。quota_keys 为 None 时不回滚配额。"""
    is_stream = False
    if body:
        try:
            is_stream = json.loads(body).get("stream", False)
        except Exception:
            pass

    skip = {"transfer-encoding", "connection", "keep-alive", "content-length"}

    if is_stream:
        async def event_stream():
            rollback_done = False
            try:
                async with _http_client.stream(
                    method=request.method, url=upstream_url,
                    headers=headers, content=body,
                    params=dict(request.query_params),
                ) as upstream:
                    if upstream.status_code >= 500 and quota_keys:
                        await rdb.eval(LUA_ROLLBACK, 3, *quota_keys)
                        rollback_done = True
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
            except Exception:
                if quota_keys and not rollback_done:
                    await rdb.eval(LUA_ROLLBACK, 3, *quota_keys)
                raise

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        upstream = await _http_client.request(
            method=request.method, url=upstream_url,
            headers=headers, content=body,
            params=dict(request.query_params),
            timeout=120.0,
        )
    except Exception as e:
        if quota_keys:
            await rdb.eval(LUA_ROLLBACK, 3, *quota_keys)
        raise HTTPException(502, f"上游连接失败: {e}")

    if upstream.status_code >= 500 and quota_keys:
        await rdb.eval(LUA_ROLLBACK, 3, *quota_keys)

    resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in skip}
    return Response(content=upstream.content,
                    status_code=upstream.status_code,
                    headers=resp_headers)
