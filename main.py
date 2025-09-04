import os, re, json, time, asyncio, logging, tempfile, html
from datetime import datetime, timedelta


import nest_asyncio
nest_asyncio.apply()

from dotenv import load_dotenv
load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_API_KEY") or ""

if not API_ID or not API_HASH or not BOT_TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    faltan = []
    if not API_ID: faltan.append("API_ID")
    if not API_HASH: faltan.append("API_HASH")
    if not BOT_TOKEN: faltan.append("BOT_TOKEN")
    if not SUPABASE_URL: faltan.append("SUPABASE_URL")
    if not SUPABASE_KEY: faltan.append("SUPABASE_ANON_KEY (o SUPABASE_API_KEY)")
    raise SystemExit("❌ Faltan variables en .env: " + ", ".join(faltan))


from supabase import create_client, Client
from telethon import TelegramClient, events


from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("bot")


OWNER_ID = "2016769834"
SEED_ADMIN_IDS = {"7988910268"}

SERVICIO_VIP = "@Servicio_VIPBot"
CODIGOS_NETFLIX = "@codigosnetflix_bot"
VIP_REEMPLAZARBOT = "@VIPREEMPLAZARBOT"

COOLDOWN_SECONDS = 3.5
WAIT_TIMEOUT = 300


supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
client = TelegramClient("forwarder", API_ID, API_HASH)
app = None


_entity_cache = {}
_last_cmd_by_user = {}
_ADMIN_CLIENT_COL_CACHE = None   # (no se usa ya para listar clientes; se conserva para /miusuario)
_admins_cache_ts = 0
_admins_cache_ids = set()



def esc(s: str) -> str:
    return html.escape(str(s) if s is not None else "")

def pill(label: str) -> str:
    return f"<code>{esc(label)}</code>"

def fmt_kv(**pairs) -> str:
    return "\n".join(f"• <b>{esc(k)}:</b> {esc(v)}" for k, v in pairs.items())

# Respuesta con fallback si falla el parseo HTML
async def _safe_reply(msg, text_html: str, prefix: str = ""):
    try:
        await msg.reply_text(prefix + text_html, parse_mode=ParseMode.HTML)
    except BadRequest:
        plain = re.sub(r"<[^>]+>", "", html.unescape(text_html))
        await msg.reply_text(prefix + plain)

async def say_ok(update: Update, text_html: str):
    await _safe_reply(update.effective_message, text_html, "✅ ")

async def say_warn(update: Update, text_html: str):
    await _safe_reply(update.effective_message, text_html, "⚠️ ")

async def say_err(update: Update, text_html: str):
    await _safe_reply(update.effective_message, text_html, "❌ ")



def upsert_usuario(telegram_id: str, username: str, rol: str | None = None):
    data = {"telegram_id": str(telegram_id), "username": username}
    if rol is not None:
        data["rol"] = rol
    supabase.table("usuarios").upsert(data).execute()

def get_role(telegram_id: str) -> str:
    try:
        r = supabase.table("usuarios").select("rol").eq("telegram_id", str(telegram_id)).execute()
        if r.data:
            return r.data[0].get("rol", "user") or "user"
    except Exception:
        pass
    return "user"

def ensure_owner_and_seed_admins():
    upsert_usuario(OWNER_ID, "owner", rol="owner")
    for aid in SEED_ADMIN_IDS:
        if get_role(aid) != "owner":
            upsert_usuario(aid, f"admin_{aid}", rol="admin")

def is_admin_or_owner(uid: str) -> bool:
    return get_role(uid) in ("admin", "owner")

# (se mantiene para /miusuario, pero ya NO se usa para listar clientes)
def _detect_admin_client_col() -> str:
    global _ADMIN_CLIENT_COL_CACHE
    if _ADMIN_CLIENT_COL_CACHE:
        return _ADMIN_CLIENT_COL_CACHE
    for col in ["cliente_id", "cliente", "user_id", "clienteId", "client_id"]:
        try:
            supabase.table("admin_clientes").select(col).limit(1).execute()
            _ADMIN_CLIENT_COL_CACHE = col
            return col
        except Exception:
            continue
    _ADMIN_CLIENT_COL_CACHE = "cliente_id"
    return _ADMIN_CLIENT_COL_CACHE


def admin_client_ids(admin_id: str) -> set[str]:
    ids = set()
    for col in ["cliente_id", "cliente", "user_id", "clienteId", "client_id"]:
        try:
            r = supabase.table("admin_clientes").select(col).eq("admin_id", str(admin_id)).execute()
            ids |= {str(x.get(col)) for x in (r.data or []) if x.get(col)}
        except Exception:
            continue
    return ids

def admin_has_clients(admin_id: str) -> bool:
    return bool(admin_client_ids(admin_id))

def try_upsert_admin_cliente(admin_id: str, cliente_id: str) -> str:
    errs = []
    for col in ["cliente_id", "cliente", "user_id", "clienteId", "client_id"]:
        try:
            supabase.table("admin_clientes").upsert({"admin_id": str(admin_id), col: str(cliente_id)}).execute()
            global _ADMIN_CLIENT_COL_CACHE
            _ADMIN_CLIENT_COL_CACHE = col
            return col
        except Exception as e:
            errs.append(f"{col}: {e}")
    raise RuntimeError("No pude escribir en admin_clientes. Revisa columnas. " + " | ".join(errs))

def correo_asignado_a_usuario(uid: str, correo: str) -> bool:
    r = supabase.table("asignaciones").select("id").eq("usuario_id", uid).eq("correo", correo).eq("activo", True).execute()
    return bool(r.data)

def buscar_duenho_por_correo_activo(correo: str):
    r = supabase.table("asignaciones").select("usuario_id, fecha_venc").eq("correo", correo).eq("activo", True).execute()
    return r.data[0] if r.data else None

def obtener_asignacion_activa(uid: str, correo: str):
    r = supabase.table("asignaciones").select("*").eq("usuario_id", uid).eq("correo", correo).eq("activo", True).execute()
    return r.data[0] if r.data else None

def listar_asignaciones_usuario(uid: str):
    r = supabase.table("asignaciones").select("correo, fecha_venc").eq("usuario_id", uid).eq("activo", True).order("correo").execute()
    return r.data or []

def listar_todas_asignaciones_activas():
    r = supabase.table("asignaciones").select("usuario_id, correo, fecha_venc").eq("activo", True).order("usuario_id").execute()
    return r.data or []

def get_creditos(uid: str) -> int:
    r = supabase.table("usuarios").select("creditos").eq("telegram_id", str(uid)).execute()
    if not r.data: return 0
    try:
        return int(r.data[0].get("creditos", 0))
    except Exception:
        return 0

def set_creditos(uid: str, val: int):
    if val < 0: val = 0
    supabase.table("usuarios").update({"creditos": int(val)}).eq("telegram_id", str(uid)).execute()

def recalc_cuentas_asignadas(uid: str) -> int:
    r = supabase.table("asignaciones").select("id", count="exact").eq("usuario_id", str(uid)).eq("activo", True).execute()
    total = getattr(r, "count", None) or 0
    supabase.table("usuarios").update({"cuentas_asignadas": total}).eq("telegram_id", str(uid)).execute()
    return total

def user_has_blocking_action(uid: str) -> bool:
    try:
        r = supabase.table("operaciones").select("id").eq("usuario_id", uid).eq("estado", "pendiente").execute()
        return bool(r.data)
    except Exception:
        return False

def start_operation(uid: str, tipo: str, correo: str | None, payload: str | None):
    try:
        data = {
            "usuario_id": uid,
            "tipo": tipo,
            "payload": json.dumps({"correo": correo, "raw": payload}) if (correo or payload) else None,
            "estado": "pendiente",
        }
        ret = supabase.table("operaciones").insert(data).execute()
        return ret.data[0] if ret.data else {"id": -1}
    except Exception as e:
        log.warning(f"start_operation error: {e}")
        return {"id": -1}

def finish_operation(op_id: int, estado: str, raw_resp: str | None = None):
    if op_id in (None, -1):
        return
    try:
        data = {"estado": estado}
        if raw_resp is not None:
            data["raw_resp"] = raw_resp
        supabase.table("operaciones").update(data).eq("id", op_id).execute()
    except Exception as e:
        log.warning(f"finish_operation error: {e}")



async def get_entity_cached(username: str):
    if username not in _entity_cache:
        _entity_cache[username] = await client.get_entity(username)
    return _entity_cache[username]

async def send_and_wait_reply(username: str, text: str, timeout_sec: int = WAIT_TIMEOUT) -> str | None:
    entity = await get_entity_cached(username)
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()

    async def _on_reply(event):
        if not fut.done():
            fut.set_result(event.message.message or "")

    handler = events.NewMessage(chats=entity)
    client.add_event_handler(_on_reply, handler)
    try:
        await client.send_message(entity, text)
        try:
            return await asyncio.wait_for(fut, timeout=timeout_sec)
        except asyncio.TimeoutError:
            return None
    finally:
        client.remove_event_handler(_on_reply, handler)

def enforce_user_cooldown(update: Update) -> bool:
    uid = str(update.effective_user.id)
    if is_admin_or_owner(uid):
        return True
    now = time.time()
    last = _last_cmd_by_user.get(uid, 0)
    if now - last < COOLDOWN_SECONDS:
        left = COOLDOWN_SECONDS - (now - last)
        try:
            asyncio.create_task(update.message.reply_text(f"⏳ Espera {left:.1f}s antes de usar otro comando."))
        except Exception:
            pass
        return False
    _last_cmd_by_user[uid] = now
    return True

async def send_txt_document(context: ContextTypes.DEFAULT_TYPE, chat_id: int, filename: str, header: str, rows: list[str], caption: str):
    path = tempfile.NamedTemporaryFile(delete=False, suffix=".txt").name
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            if header: f.write(header.rstrip() + "\n")
            for line in rows: f.write(line.rstrip() + "\n")
        with open(path, "rb") as fh:
            await context.bot.send_document(chat_id=chat_id, document=InputFile(fh, filename=filename), caption=caption)
    finally:
        try: os.remove(path)
        except Exception: pass

def fmt_fecha_show(iso_date: str | None) -> str:
    if not iso_date: return "-"
    try: return datetime.fromisoformat(str(iso_date)).strftime("%d/%m/%Y")
    except Exception: return str(iso_date)

def build_info_text(uid: str, username: str) -> str:
    creditos = get_creditos(uid)
    total = recalc_cuentas_asignadas(uid)
    role = get_role(uid)
    return (
        "ℹ️ <b>INFO</b>\n"
        f"{fmt_kv(Username=username or '-', ID=uid, Rol=role, **{'Cuentas': total, 'Créditos': creditos})}"
    )

def parse_date_str(s: str) -> str | None:
    s = s.strip()
    for sep in ("/", "-", "."):
        p = s.split(sep)
        if len(p) == 3:
            d, m, y = p
            try:
                d = int(re.sub(r"\D", "", d) or "0")
                m = int(re.sub(r"\D", "", m) or "0")
                y = int(re.sub(r"\D", "", y) or "0")
            except Exception:
                return None
            if y < 100: y += 2000
            try:
                return datetime(y, m, d).date().isoformat()
            except Exception:
                return None
    return None

def _date_key(iso_date: str | None):
    try:
        return datetime.fromisoformat(str(iso_date)).date()
    except Exception:
        return datetime.max.date()

async def must_have_correo(update: Update, correo: str) -> bool:
    correo = (correo or "").strip().lower()
    uid = str(update.effective_user.id)
    role = get_role(uid)

    if role in ("admin", "owner"):
        return True

    if correo_asignado_a_usuario(uid, correo):
        return True

    await say_err(update, "Ese correo no está asignado a tu usuario.")
    return False


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    username = update.effective_user.username or f"user_{uid}"
    ensure_owner_and_seed_admins()
    try:
        r = supabase.table("usuarios").select("telegram_id").eq("telegram_id", uid).execute()
        if not r.data:
            upsert_usuario(uid, username, rol=None)
            await say_ok(update, "Registro completado.")
    except Exception as e:
        log.warning(f"/start upsert error: {e}")
    await update.message.reply_text(build_info_text(uid, username), parse_mode=ParseMode.HTML)

async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    username = update.effective_user.username or f"user_{uid}"
    await update.message.reply_text(build_info_text(uid, username), parse_mode=ParseMode.HTML)

async def cmd_comandos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    role = get_role(uid)
    user_txt = (
        "📋 <b>Comandos de usuario</b>\n"
        f"{pill('/start')} – registrarte / ver info\n"
        f"{pill('/info')} – tu info\n"
        f"{pill('/cuentas')} – ver tus cuentas (si >10, en .txt)\n"
        f"{pill('/code')} {pill('<correo>')}\n"
        f"{pill('/link')} {pill('<correo>')}\n"
        f"{pill('/activarTV')} {pill('<correo>')}\n"
        f"{pill('/hogar')} {pill('<correo>')}\n"
        f"{pill('/estoydeviaje')} {pill('<correo>')} (se envía como /code)\n"
        f"{pill('/comprar')} 1\n"
        f"{pill('/renovar')} {pill('<correo>')}\n"
        f"{pill('/reemplazar')} {pill('<correo>')} {pill('<motivo>')}\n"
    )
    admin_txt = (
        "\n👑 <b>Comandos admin/owner</b>\n"
        f"{pill('/miusuario')} {pill('<ID>')}  (alias {pill('/misuario')})\n"
        f"{pill('/registraradmin')} {pill('<ID>')}  (solo owner)\n"
        f"{pill('/registrarcorreos')}  (texto o .txt:  correo;dd/mm/aaaa)\n"
        f"{pill('/asignar')} {pill('<correo> <dd/mm/aaaa> <ID>')}  (o .txt con caption '/asignar <ID>')\n"
        f"{pill('/remover')} {pill('<correo> <ID>')}               (o .txt con caption '/remover <ID>')\n"
        f"{pill('/asignarcreditos')} {pill('<cantidad> <ID>')}\n"
        f"{pill('/comprar')} N  (admin/owner)\n"
        f"{pill('/reemplazarvip')} {pill('<correo> <motivo>')}\n"
        f"{pill('/reemplazos')}  (ver pendientes)\n"
    )
    await update.message.reply_text(user_txt + (admin_txt if role in ("admin","owner") else ""), parse_mode=ParseMode.HTML)

async def cmd_cuentas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    role = get_role(uid)

    if role == "owner":
        filas = listar_todas_asignaciones_activas()
        if not filas:
            await say_warn(update, "No hay asignaciones activas.")
            return
        filas.sort(key=lambda r: _date_key(r.get("fecha_venc")))
        rows = [f"{r.get('usuario_id','')} | {r.get('correo','')} | {fmt_fecha_show(r.get('fecha_venc'))}" for r in filas]
        await send_txt_document(
            context, update.effective_chat.id,
            "todas_asignaciones_activas.txt",
            "usuario_id | correo | fecha_venc (dd/mm/aaaa)",
            rows,
            "📄 Todas las cuentas activas (ordenadas por vencimiento)"
        )
        return

    if role == "admin":
        ids = admin_client_ids(uid) | {uid}
        rows_all = []
        for cid in ids:
            asign = listar_asignaciones_usuario(cid)
            asign.sort(key=lambda r: _date_key(r.get("fecha_venc")))
            for r in asign:
                rows_all.append(f"{cid} | {r['correo']} | {fmt_fecha_show(r.get('fecha_venc'))}")
        if not rows_all:
            await say_warn(update, "No hay cuentas para ti o tus clientes.")
            return
        await send_txt_document(
            context, update.effective_chat.id,
            "cuentas_admin_y_clientes.txt",
            "usuario_id | correo | fecha_venc (dd/mm/aaaa)",
            rows_all,
            "📄 Cuentas (tú y tus clientes) — ordenadas por vencimiento"
        )
        return

    filas = listar_asignaciones_usuario(uid)
    if not filas:
        await say_warn(update, "No tienes cuentas asignadas.")
        return
    filas.sort(key=lambda r: _date_key(r.get("fecha_venc")))
    if len(filas) > 10:
        rows = [f"{r['correo']} | {fmt_fecha_show(r.get('fecha_venc'))}" for r in filas]
        await send_txt_document(
            context, update.effective_chat.id,
            "tus_cuentas.txt",
            "correo | fecha_venc (dd/mm/aaaa)",
            rows,
            "📄 Tus cuentas (ordenadas por vencimiento)"
        )
    else:
        lines = "\n".join(f"• {esc(r['correo'])} — vence {esc(fmt_fecha_show(r.get('fecha_venc')))}" for r in filas)
        await update.message.reply_text("🗂️ <b>Tus cuentas</b> (ordenadas por vencimiento):\n" + lines, parse_mode=ParseMode.HTML)



async def forward_simple(update: Update, context: ContextTypes.DEFAULT_TYPE, target_bot: str, cmd_name: str):
    if not enforce_user_cooldown(update): return
    uid = str(update.effective_user.id)

    if not is_admin_or_owner(uid) and user_has_blocking_action(uid):
        await say_err(update, "Tienes una acción pendiente. Espera a que finalice.")
        return

    args = context.args
    if not args:
        await say_warn(update, f"Uso: {pill(cmd_name)} {pill('<correo>')}")
        return
    correo = args[0].strip().lower()
    if not await must_have_correo(update, correo):
        return

    send_text = f"{cmd_name} {correo}"
    if cmd_name == "/estoydeviaje":
        send_text = f"/code {correo}"

    op = start_operation(uid, "reenvio", correo, send_text)
    await update.message.reply_text("📨 <i>Enviando…</i>", parse_mode=ParseMode.HTML)

    reply = await send_and_wait_reply(target_bot, send_text, timeout_sec=WAIT_TIMEOUT)
    if reply is None:
        finish_operation(op["id"], "fallido", raw_resp="timeout")
        await say_warn(update, "El bot externo no respondió a tiempo (5 min).")
        return

    finish_operation(op["id"], "completado", raw_resp=reply)
    await update.message.reply_text(f"📬 <b>Respuesta</b>:\n{esc(reply)}", parse_mode=ParseMode.HTML)

async def cmd_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await forward_simple(update, context, SERVICIO_VIP, "/code")

async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await forward_simple(update, context, SERVICIO_VIP, "/link")

async def cmd_activar_tv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await forward_simple(update, context, SERVICIO_VIP, "/activarTV")

async def cmd_hogar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await forward_simple(update, context, CODIGOS_NETFLIX, "/hogar")

async def cmd_estoydeviaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await forward_simple(update, context, CODIGOS_NETFLIX, "/estoydeviaje")

async def cmd_comprar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not enforce_user_cooldown(update): return
    uid = str(update.effective_user.id)
    role = get_role(uid)

    if not context.args:
        await say_warn(update, "Uso (usuario): /comprar 1\nUso (admin/owner): /comprar N")
        return

    cantidad = 1
    if role in ("admin", "owner"):
        if not context.args[0].isdigit() or int(context.args[0]) < 1:
            await say_warn(update, "Uso (admin/owner): /comprar N  (N>=1)")
            return
        cantidad = int(context.args[0])
    else:
        if context.args[0] != "1":
            await say_warn(update, "Uso: /comprar 1")
            return

    if not is_admin_or_owner(uid) and user_has_blocking_action(uid):
        await say_err(update, "Tienes una acción pendiente. Espera a que finalice.")
        return

    creditos = get_creditos(uid)
    if creditos < cantidad:
        await say_err(update, f"Créditos insuficientes. Tienes {creditos}, necesitas {cantidad}.")
        return

    op = start_operation(uid, "compra" if cantidad == 1 else "compra_lote", None, f"/comprar {cantidad}")
    await update.message.reply_text(f"🛒 <b>Procesando</b> {cantidad} compra(s)… (máx 5 min c/u)", parse_mode=ParseMode.HTML)

    exitos, fallos = [], []

    for i in range(cantidad):
        reply_text = await send_and_wait_reply(SERVICIO_VIP, "/comprar 1", timeout_sec=WAIT_TIMEOUT)
        if reply_text is None:
            fallos.append(f"#{i+1}: sin respuesta"); continue
        m = re.search(r"Cuenta:\s*([^\s:@]+@[^\s:]+)", reply_text, flags=re.IGNORECASE)
        if not m:
            fallos.append(f"#{i+1}: respuesta inválida"); continue
        correo = m.group(1).lower().strip()

        dueno = buscar_duenho_por_correo_activo(correo)
        if dueno and dueno["usuario_id"] != uid:
            fallos.append(f"#{i+1}: correo ya asignado a otro"); continue

        try:
            venc = (datetime.utcnow().date() + timedelta(days=30)).isoformat()
            supabase.table("correos").upsert({"correo": correo, "vencimiento": venc}, on_conflict="correo").execute()
            if obtener_asignacion_activa(uid, correo):
                supabase.table("asignaciones").update({"fecha_venc": venc}).eq("usuario_id", uid).eq("correo", correo).eq("activo", True).execute()
            else:
                supabase.table("asignaciones").insert({"correo": correo, "usuario_id": uid, "fecha_venc": venc, "asignado_por": "servicio_vip", "activo": True}).execute()
            set_creditos(uid, get_creditos(uid) - 1)
            try:
                supabase.table("creditos_historial").insert({"usuario_id": uid, "delta": -1, "motivo": "compra", "hecho_por": "servicio_vip"}).execute()
            except Exception: pass
            recalc_cuentas_asignadas(uid)
            exitos.append(f"{correo} (vence {fmt_fecha_show(venc)})")
        except Exception as e:
            fallos.append(f"#{i+1}: error DB {e}")

    finish_operation(op["id"], "completado" if exitos else "fallido", raw_resp=f"exitos={len(exitos)}, fallos={len(fallos)}")
    parts = []
    if exitos:
        parts.append("🟢 <b>Exitosas</b>:\n" + "\n".join(f"• {esc(x)}" for x in exitos[:20]) + ("…" if len(exitos) > 20 else ""))
    if fallos:
        parts.append("🟠 <b>Fallos</b>:\n" + "\n".join(f"• {esc(x)}" for x in fallos[:20]) + ("…" if len(fallos) > 20 else ""))
    await update.message.reply_text("\n\n".join(parts) if parts else "No se concretó ninguna compra.", parse_mode=ParseMode.HTML)

async def cmd_renovar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not enforce_user_cooldown(update): return
    uid = str(update.effective_user.id)

    if not context.args:
        await say_warn(update, f"Uso: {pill('/renovar')} {pill('<correo>')}")
        return

    correo = context.args[0].strip().lower()
    if not is_admin_or_owner(uid) and user_has_blocking_action(uid):
        await say_err(update, "Tienes una acción pendiente. Espera a que finalice.")
        return
    if not await must_have_correo(update, correo):
        return

    if get_creditos(uid) < 1:
        await say_err(update, "No tienes créditos suficientes.")
        return

    op = start_operation(uid, "renovar", correo, f"/renovar {correo}")
    await update.message.reply_text("🔄 <i>Solicitando renovación…</i>", parse_mode=ParseMode.HTML)

    reply = await send_and_wait_reply(SERVICIO_VIP, f"/renovar {correo}", timeout_sec=WAIT_TIMEOUT)
    if reply is None:
        finish_operation(op["id"], "fallido", raw_resp="timeout")
        await say_warn(update, "El bot externo no respondió a tiempo (5 min).")
        return

    if correo not in reply:
        finish_operation(op["id"], "fallido", raw_resp="correo no coincide")
        await say_err(update, "La respuesta no coincide con el correo solicitado.")
        return

    nueva = (datetime.utcnow().date() + timedelta(days=30)).isoformat()
    try:
        if not obtener_asignacion_activa(uid, correo):
            supabase.table("correos").upsert({"correo": correo, "vencimiento": nueva}, on_conflict="correo").execute()
            supabase.table("asignaciones").insert({"correo": correo, "usuario_id": uid, "fecha_venc": nueva, "asignado_por": "renovacion", "activo": True}).execute()
        else:
            supabase.table("asignaciones").update({"fecha_venc": nueva}).eq("usuario_id", uid).eq("correo", correo).eq("activo", True).execute()

        set_creditos(uid, get_creditos(uid) - 1)
        try:
            supabase.table("creditos_historial").insert({"usuario_id": uid, "delta": -1, "motivo": "renovacion", "hecho_por": "servicio_vip"}).execute()
        except Exception: pass
        recalc_cuentas_asignadas(uid)
        finish_operation(op["id"], "completado", raw_resp=reply)
        await say_ok(update, f"Account Update [{esc(correo)}]: {esc(fmt_fecha_show(nueva))}")
    except Exception as e:
        finish_operation(op["id"], "fallido", raw_resp=str(e))
        await say_err(update, f"Error al actualizar: {esc(e)}")


async def cmd_miusuario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if get_role(uid) != "admin":
        await say_err(update, "Solo admins pueden usar /miusuario.")
        return

    if len(context.args) != 1:
        await say_warn(update, f"Uso: {pill('/miusuario')} {pill('<ID_TELEGRAM_CLIENTE>')}  (alias: {pill('/misuario')})")
        return

    cliente = context.args[0].strip()
    if not cliente.isdigit():
        await say_err(update, "ID inválido. Debe ser numérico.")
        return
    if cliente == uid:
        await say_err(update, "No puedes asignarte a ti mismo.")
        return

    try:
        upsert_usuario(cliente, f"user_{cliente}")
        col_used = try_upsert_admin_cliente(uid, cliente)
        await update.effective_message.reply_text(
            "✅ Cliente registrado correctamente.\n"
            f"• Admin: {uid}\n"
            f"• Cliente: {cliente}\n"
            f"• Columna usada: {col_used}"
        )
    except Exception as e:
        err = str(e)
        ayuda = ""
        if "row level security" in err.lower() or "pgrst" in err.lower():
            ayuda = "\n🔐 Crea columna texto 'cliente_id' en 'admin_clientes' o ajusta RLS."
        await say_err(update, f"No se pudo registrar el cliente.\nDetalle: {esc(err)}{ayuda}")

# alias /misuario -> /miusuario
async def cmd_misuario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_miusuario(update, context)

# --- Owner: elevar a admin
async def cmd_registraradmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if get_role(uid) != "owner":
        await say_err(update, "Solo el owner puede usar /registraradmin.")
        return

    if len(context.args) != 1:
        await say_warn(update, f"Uso: {pill('/registraradmin')} {pill('<ID>')}")
        return

    target = context.args[0].strip()
    if not target.isdigit():
        await say_err(update, "ID inválido. Debe ser numérico.")
        return

    upsert_usuario(target, f"admin_{target}", rol="admin")
    await say_ok(update, f"{esc(target)} ahora es admin.")



async def cmd_asignar_creditos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    role = get_role(uid)
    if role not in ("admin", "owner"):
        await say_err(update, "No autorizado.")
        return

    if len(context.args) != 2 or not context.args[0].isdigit():
        await say_warn(update, f"Uso: {pill('/asignarcreditos')} {pill('<cantidad> <ID>')}")
        return

    cantidad = int(context.args[0])
    if cantidad <= 0:
        await say_warn(update, "La cantidad debe ser positiva.")
        return

    target = context.args[1].strip()
    if not target.isdigit():
        await say_warn(update, "ID destino inválido.")
        return

    try:
        if role == "admin":
            saldo_admin = get_creditos(uid)
            if saldo_admin < cantidad:
                await say_err(update, f"No tienes suficientes créditos. Tienes {saldo_admin}.")
                return

        r = supabase.table("usuarios").select("creditos, username").eq("telegram_id", target).execute()
        if not r.data:
            upsert_usuario(target, f"user_{target}")
            actuales = 0
            username_dest = f"user_{target}"
        else:
            actuales = int(r.data[0].get("creditos", 0) or 0)
            username_dest = r.data[0].get("username") or f"user_{target}"

        if role == "admin":
            set_creditos(uid, get_creditos(uid) - cantidad)
            try:
                supabase.table("creditos_historial").insert({
                    "usuario_id": uid, "delta": -cantidad, "motivo": "transferencia", "hecho_por": uid
                }).execute()
            except Exception: pass

        supabase.table("usuarios").update({"creditos": actuales + cantidad}).eq("telegram_id", target).execute()
        try:
            supabase.table("creditos_historial").insert({
                "usuario_id": target, "delta": cantidad,
                "motivo": "asignacion_admin" if role == "admin" else "asignacion_owner",
                "hecho_por": uid
            }).execute()
        except Exception: pass

        await update.message.reply_text(
            "🎁 <b>Créditos asignados</b>\n" +
            fmt_kv(Destino=f"{username_dest} ({target})", Antes=actuales, Cambio=f"+{cantidad}", Ahora=actuales+cantidad),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        err = str(e)
        ayuda = ""
        if "row level security" in err.lower() or "pgrst" in err.lower():
            ayuda = "\n🔐 Revisa RLS en tablas <code>usuarios</code> y <code>creditos_historial</code>."
        await say_err(update, f"Error al asignar créditos.<br>Detalle: {esc(err)}{ayuda}")



async def notify_admins(text: str, keyboard: InlineKeyboardMarkup | None = None):
    global _admins_cache_ts, _admins_cache_ids, app
    now = time.time()
    if now - _admins_cache_ts > 60 or not _admins_cache_ids:
        ids = set()
        r = supabase.table("usuarios").select("telegram_id").in_("rol", ["owner", "admin"]).execute()
        for row in r.data or []:
            ids.add(str(row["telegram_id"]))
        _admins_cache_ids = ids
        _admins_cache_ts = now

    for aid in _admins_cache_ids:
        try:
            await app.bot.send_message(chat_id=int(aid), text=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        except Exception:
            pass

async def cmd_reemplazar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not enforce_user_cooldown(update): return
    uid = str(update.effective_user.id)
    if len(context.args) < 2:
        await say_warn(update, f"Uso: {pill('/reemplazar')} {pill('<correo> <motivo>')}")
        return

    correo = context.args[0].strip().lower()
    motivo = " ".join(context.args[1:]).strip()

    if not is_admin_or_owner(uid) and user_has_blocking_action(uid):
        await say_err(update, "Tienes una acción pendiente. Espera a que finalice.")
        return
    if not await must_have_correo(update, correo):
        return

    ins = supabase.table("reemplazos_solicitudes").insert({
        "usuario_id": uid, "correo": correo, "motivo": motivo, "estado": "pendiente"
    }).execute()
    req_id = ins.data[0]["id"]

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Aceptar", callback_data=f"reemp_ok:{req_id}"),
                                InlineKeyboardButton("🛑 Rechazar", callback_data=f"reemp_no:{req_id}")]])
    text = (
        "🆘 <b>Solicitud de reemplazo</b>\n" +
        fmt_kv(Correo=correo, Usuario=f"{update.effective_user.username or '-'} (ID {uid})", Motivo=motivo)
    )
    await notify_admins(text, keyboard=kb)
    await say_ok(update, "Tu solicitud fue enviada a los administradores.")

async def on_reemp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid_click = str(query.from_user.id)
    if not is_admin_or_owner(uid_click):
        await query.answer("No autorizado.", show_alert=True); return
    await query.answer()

    data = query.data or ""
    if not data.startswith("reemp_"): return
    action, req_id_s = data.split(":")
    req_id = int(req_id_s)

    sel = supabase.table("reemplazos_solicitudes").select("*").eq("id", req_id).execute()
    if not sel.data:
        await query.edit_message_text("Solicitud no encontrada (ya gestionada)."); return
    req = sel.data[0]
    if req["estado"] != "pendiente":
        await query.edit_message_text("Solicitud ya gestionada."); return

    if action == "reemp_no":
        supabase.table("reemplazos_solicitudes").update({"estado": "rechazado", "aprobado_por": uid_click}).eq("id", req_id).execute()
        await query.edit_message_text("❌ Rechazada.")
        try: await context.bot.send_message(chat_id=int(req["usuario_id"]), text=f"❌ Tu reemplazo para {req['correo']} fue rechazado.")
        except Exception: pass
        return

    supabase.table("reemplazos_solicitudes").update({"estado": "aceptado", "aprobado_por": uid_click}).eq("id", req_id).execute()
    correo = req["correo"]; user_id = req["usuario_id"]
    try: await context.bot.send_message(chat_id=int(user_id), text="✅ Solicitud aceptada. Buscando…")
    except Exception: pass

    try:
        await client.send_message(await get_entity_cached(VIP_REEMPLAZARBOT), f"/reemplazar {correo} {req.get('motivo') or ''}".strip())
    except Exception as e:
        log.warning(f"Error enviando al VIP: {e}")
    await query.edit_message_text("🟢 Aceptada y enviada al VIP.")

async def cmd_reemplazarvip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not is_admin_or_owner(uid):
        await say_err(update, "No autorizado."); return
    if len(context.args) < 2:
        await say_warn(update, f"Uso: {pill('/reemplazarvip')} {pill('<correo> <motivo>')}")
        return
    correo = context.args[0].strip().lower()
    motivo = " ".join(context.args[1:]).strip()
    supabase.table("reemplazos_solicitudes").insert({"usuario_id": uid, "correo": correo, "motivo": motivo, "estado": "aceptado", "aprobado_por": uid}).execute()
    await say_ok(update, "Reemplazo enviado al VIP.")
    try:
        await client.send_message(await get_entity_cached(VIP_REEMPLAZARBOT), f"/reemplazar {correo} {motivo}".strip())
    except Exception as e:
        log.warning(f"Error enviando al VIP: {e}")

@client.on(events.NewMessage(chats=None))
async def on_any_message(event):
    try:
        sender = await event.get_sender()
        uname = f"@{sender.username}" if getattr(sender, 'username', None) else ""
        if uname != VIP_REEMPLAZARBOT:
            return

        text = (event.message.message or "").strip()

        # Rechazo del VIP
        if re.search(r"(?i)cuenta\s+no\s+v[áa]lida", text):
            rq = supabase.table("reemplazos_solicitudes") \
                .select("id, usuario_id, correo, estado") \
                .in_("estado", ["aceptado", "pendiente"]) \
                .order("id", desc=True).limit(1).execute()

            if rq.data:
                req = rq.data[0]
                uid = req["usuario_id"]
                correo = req.get("correo")
                try:
                    supabase.table("reemplazos_solicitudes").update({"estado": "rechazado"}).eq("id", req["id"]).execute()
                except Exception:
                    pass
                try:
                    await app.bot.send_message(
                        chat_id=int(uid),
                        text=f"❌ No se pudo procesar tu reemplazo para <b>{esc(correo)}</b>.\n\n<code>{esc(text)}</code>",
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass
                await notify_admins(
                    "❌ Reemplazo rechazado por el VIP\n" +
                    fmt_kv(Correo=correo or "-", User_ID=uid, Motivo=text),
                    keyboard=None
                )
            return

        # Éxito del VIP
        if "Cuenta reemplazada" not in text:
            return

        m = re.search(r"\[\s*([^\]]+)\s*\]\s*→\s*([^\s:]+)", text)
        if not m:
            return

        viejo = m.group(1).strip().lower()
        nuevo = m.group(2).strip().lower()

        rq = supabase.table("reemplazos_solicitudes") \
            .select("id, usuario_id, correo, estado") \
            .eq("correo", viejo) \
            .in_("estado", ["aceptado", "pendiente"]) \
            .order("id", desc=True).limit(1).execute()

        if not rq.data:
            await notify_admins(
                "ℹ️ VIP reemplazó (sin solicitud asociada):\n" +
                fmt_kv(Viejo=viejo, Nuevo=nuevo),
                keyboard=None
            )
            return

        req = rq.data[0]
        uid = req["usuario_id"]

        old_asig = obtener_asignacion_activa(uid, viejo)
        fecha_venc = old_asig["fecha_venc"] if old_asig else (datetime.utcnow().date() + timedelta(days=30)).isoformat()

        supabase.table("asignaciones").update({"activo": False}).eq("usuario_id", uid).eq("correo", viejo).eq("activo", True).execute()
        supabase.table("correos").upsert({"correo": nuevo, "vencimiento": fecha_venc}, on_conflict="correo").execute()
        supabase.table("asignaciones").insert({
            "correo": nuevo, "usuario_id": uid, "fecha_venc": fecha_venc,
            "asignado_por": "reemplazo", "activo": True
        }).execute()
        recalc_cuentas_asignadas(uid)
        supabase.table("reemplazos_solicitudes").update({"estado": "aceptado"}).eq("id", req["id"]).execute()

        try:
            await app.bot.send_message(
                chat_id=int(uid),
                text=("✅ <b>Reemplazo completado</b>\n" +
                      fmt_kv(Antes=viejo, Ahora=nuevo, Vence=fmt_fecha_show(fecha_venc))),
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

        await notify_admins(
            "🟢 Reemplazo aplicado\n" +
            fmt_kv(Viejo=viejo, Nuevo=nuevo, User_ID=uid),
            keyboard=None
        )

    except Exception as e:
        log.warning(f"on_any_message error: {e}")
async def cmd_registrarcorreos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    role = get_role(uid)
    if role not in ("admin","owner"):
        await say_err(update, "No autorizado.")
        return

    full_text = update.message.text or ""
    lines_text = full_text.partition("\n")[2]
    if not lines_text.strip() and context.args:
        lines_text = " ".join(context.args)
    if not lines_text.strip():
        await say_warn(update, "Envía el texto debajo del comando o adjunta un .txt con caption /registrarcorreos.\nFormato por línea:  correo;dd/mm/aaaa")
        return

    lines = [ln.strip() for ln in lines_text.splitlines() if ln.strip()]
    ok, bad = 0, []
    for ln in lines:
        parts = re.split(r"[;, \t]+", ln)
        if len(parts) < 2: bad.append(ln); continue
        correo = parts[0].lower()
        fecha_iso = parse_date_str(parts[1])
        if not fecha_iso: bad.append(ln); continue
        try:
            supabase.table("correos").upsert({"correo": correo, "vencimiento": fecha_iso}, on_conflict="correo").execute()
            ok += 1
        except Exception as e:
            bad.append(f"{ln} ({e})")

    msg = f"✅ Registrados: {ok}"
    if bad: msg += f"\n❌ Errores ({len(bad)}):\n" + "\n".join(f"- {b}" for b in bad[:20])
    await update.message.reply_text(msg)

async def _assign_one(correo: str, fecha_iso: str, target: str) -> tuple[bool, str]:
    correo = correo.lower().strip()
    if not correo or not fecha_iso or not target: return False, "datos inválidos"
    owner = buscar_duenho_por_correo_activo(correo)
    if owner and str(owner["usuario_id"]) != str(target):
        return False, "correo asignado a otro usuario"
    try:
        supabase.table("correos").upsert({"correo": correo, "vencimiento": fecha_iso}, on_conflict="correo").execute()
        if obtener_asignacion_activa(target, correo):
            supabase.table("asignaciones").update({"fecha_venc": fecha_iso}).eq("usuario_id", target).eq("correo", correo).eq("activo", True).execute()
        else:
            supabase.table("asignaciones").insert({"correo": correo, "usuario_id": target, "fecha_venc": fecha_iso, "asignado_por": "admin", "activo": True}).execute()
        recalc_cuentas_asignadas(target)
        return True, ""
    except Exception as e:
        return False, str(e)

async def cmd_asignar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    role = get_role(uid)
    if role not in ("admin","owner"):
        await say_err(update, "No autorizado."); return

    args = context.args
    if len(args) < 3:
        await say_warn(update, f"Uso: {pill('/asignar')} {pill('<correo> <dd/mm/aaaa> <ID>')}  (o)  {pill('/asignar')} {pill('<ID> <correo> <dd/mm/aaaa>')}")
        return

    if args[0].isdigit() and "@" not in args[0]:
        target = args[0].strip()
        correo = args[1].strip().lower()
        fecha_iso = parse_date_str(args[2])
    else:
        correo = args[0].strip().lower()
        fecha_iso = parse_date_str(args[1])
        target = args[2].strip()

    if not target.isdigit() or not fecha_iso:
        await say_err(update, "Fecha inválida (usa dd/mm/aaaa) o ID inválido.")
        return

    ok, err = await _assign_one(correo, fecha_iso, target)
    if ok:
        await say_ok(update, f"Asignado {pill(correo)} a {pill(target)} (vence {pill(fmt_fecha_show(fecha_iso))})")
    else:
        await say_err(update, f"No se pudo asignar {pill(correo)}: {esc(err)}")

async def cmd_remover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    role = get_role(uid)
    if role not in ("admin","owner"):
        await say_err(update, "No autorizado."); return

    if len(context.args) < 2:
        await say_warn(update, f"Uso: {pill('/remover')} {pill('<correo> <ID>')}")
        return
    correo = context.args[0].strip().lower()
    target = context.args[1].strip()
    if not target.isdigit():
        await say_err(update, "ID inválido.")
        return

    try:
        supabase.table("asignaciones").update({"activo": False}).eq("usuario_id", target).eq("correo", correo).eq("activo", True).execute()
        recalc_cuentas_asignadas(target)
        await say_ok(update, f"Removido {pill(correo)} de {pill(target)}.")
    except Exception as e:
        await say_err(update, f"Error: {esc(e)}")

# Documentos .txt
async def doc_registrarcorreos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if get_role(uid) not in ("admin","owner"):
        await say_err(update, "No autorizado."); return
    doc = update.message.document
    if not doc or (doc.mime_type or "") != "text/plain":
        await say_warn(update, "Adjunta un .txt de texto plano."); return
    file = await doc.get_file()
    data = await file.download_as_bytearray()
    text = data.decode("utf-8", errors="ignore")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    ok, bad = 0, []
    for ln in lines:
        parts = re.split(r"[;, \t]+", ln)
        if len(parts) < 2: bad.append(ln); continue
        correo = parts[0].lower()
        fecha_iso = parse_date_str(parts[1])
        if not fecha_iso: bad.append(ln); continue
        try:
            supabase.table("correos").upsert({"correo": correo, "vencimiento": fecha_iso}, on_conflict="correo").execute()
            ok += 1
        except Exception as e:
            bad.append(f"{ln} ({e})")
    msg = f"✅ Registrados: {ok}"
    if bad: msg += f"\n❌ Errores ({len(bad)}):\n" + "\n".join(f"- {b}" for b in bad[:20])
    await update.message.reply_text(msg)

async def doc_asignar_remover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    role = get_role(uid)
    if role not in ("admin","owner"):
        await say_err(update, "No autorizado."); return
    caption = (update.message.caption or "").strip()
    if not caption:
        await say_warn(update, "Usa caption '/asignar <ID>' o '/remover <ID>' en el archivo."); return
    m_asig = re.match(r"^/asignar\s+(\d+)$", caption)
    m_rem = re.match(r"^/remover\s+(\d+)$", caption)
    if not (m_asig or m_rem):
        await say_warn(update, "Caption inválido. Ej: /asignar 123456 o /remover 123456"); return
    target = (m_asig or m_rem).group(1)

    doc = update.message.document
    if not doc or (doc.mime_type or "") != "text/plain":
        await say_warn(update, "Adjunta un .txt de texto plano."); return
    file = await doc.get_file()
    data = await file.download_as_bytearray()
    text = data.decode("utf-8", errors="ignore")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    ok, bad = 0, []
    if m_asig:
        for ln in lines:
            parts = re.split(r"[;, \t]+", ln)
            if len(parts) < 2: bad.append(ln); continue
            correo = parts[0].lower()
            fecha_iso = parse_date_str(parts[1])
            if not fecha_iso: bad.append(ln); continue
            ok1, err = await _assign_one(correo, fecha_iso, target)
            if ok1: ok += 1
            else: bad.append(f"{ln} ({err})")
        await update.message.reply_text(f"📎 Asignados: {ok}\n❌ Errores: {len(bad)}" + (("\n" + "\n".join(bad[:20])) if bad else ""))
    else:
        for ln in lines:
            correo = ln.split()[0].lower()
            try:
                supabase.table("asignaciones").update({"activo": False}).eq("usuario_id", target).eq("correo", correo).eq("activo", True).execute()
                ok += 1
            except Exception as e:
                bad.append(f"{ln} ({e})")
        recalc_cuentas_asignadas(target)
        await update.message.reply_text(f"🗑️ Removidos: {ok}\n❌ Errores: {len(bad)}" + (("\n" + "\n".join(bad[:20])) if bad else ""))



async def main():
    await client.start()
    log.info("Sesión de Telethon iniciada")

    global app
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Base
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("comandos", cmd_comandos))
    app.add_handler(CommandHandler("cuentas", cmd_cuentas))

    # Reenvíos
    app.add_handler(CommandHandler("code", cmd_code))
    app.add_handler(CommandHandler("link", cmd_link))
    app.add_handler(CommandHandler("activarTV", cmd_activar_tv))
    app.add_handler(CommandHandler("hogar", cmd_hogar))
    app.add_handler(CommandHandler("estoydeviaje", cmd_estoydeviaje))

    # Comprar / Renovar
    app.add_handler(CommandHandler("comprar", cmd_comprar))
    app.add_handler(CommandHandler("renovar", cmd_renovar))

    # Créditos / Clientes
    app.add_handler(CommandHandler("asignarcreditos", cmd_asignar_creditos))
    app.add_handler(CommandHandler("miusuario", cmd_miusuario))
    app.add_handler(CommandHandler("misuario", cmd_misuario))
    app.add_handler(CommandHandler("registraradmin", cmd_registraradmin))

    # Reemplazos
    app.add_handler(CommandHandler("reemplazar", cmd_reemplazar))
    app.add_handler(CommandHandler("reemplazarvip", cmd_reemplazarvip))
    app.add_handler(CallbackQueryHandler(on_reemp_callback, pattern=r"^reemp_"))

    # Masivos
    app.add_handler(CommandHandler("registrarcorreos", cmd_registrarcorreos))
    app.add_handler(CommandHandler("asignar", cmd_asignar))
    app.add_handler(CommandHandler("remover", cmd_remover))

    # Documentos .txt
    app.add_handler(MessageHandler(filters.Document.MimeType("text/plain") & filters.CaptionRegex(r"^/registrarcorreos\b"), doc_registrarcorreos))
    app.add_handler(MessageHandler(filters.Document.MimeType("text/plain") & (filters.CaptionRegex(r"^/asignar\s+\d+$") | filters.CaptionRegex(r"^/remover\s+\d+$")), doc_asignar_remover))

  if __name__ == "__main__":
    PORT = int(os.getenv("PORT", 8080))
    URL = os.getenv("RAILWAY_STATIC_URL", "https://telegram-bot-production-56c0.up.railway.app")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    log.info("🤖 Bot listo con Webhook. Escuchando en Railway…")

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"{URL}/{BOT_TOKEN}"
    )
