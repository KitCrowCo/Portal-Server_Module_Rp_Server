# modules/rp_server/rp_ai.py
"""
RP AI extension - drop into modules/rp_server/ to activate.
Character mode: AI embodies a specific persona using that persona's description.
DM mode: AI narrates scenarios, events, and NPCs as Dungeon Master.
Settings stored per-room in room.info["ai"]. History summary stored in room.info["ai"]["history_summary"] to compress context.
Model calls go through tools/ai_manager's connections.py (stream_llm) - same provider-agnostic path Athena/Tessa/Kimi use, not a direct Ollama call.
"""
import json, uuid, asyncio, random, pathlib
from datetime import datetime
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm.attributes import flag_modified
from .models import Room, Persona, Message, UserSession, SessionLocal

ENV = {}
IM = None
AIM = None
_AI_CONFIG_PATH = pathlib.Path("./data/rp_server/ai_server_config.json")
LORE_ROOT = pathlib.Path("./data/_common")
_AI_USER = "system_ai"
_DEFAULT = {"mode": "off", "persona": "", "dm_persona": "DM", "conn_id": "", "model": "", "trigger": "every_n", "trigger_value": 3, "trigger_count": 0, "scenario": "", "history_summary": "", "history_depth": 20}

_DM_PROMPTS = {"general": "You are the Narrator for this collaborative story. Create engaging scene developments: describe environments, introduce events, and voice minor NPCs briefly. Do not play named characters already present - you are facilitating a story, not directly playing the established characters. Be concise: one or two paragraphs, then stop and leave room for the players to respond.",
               "otome": "You are the Atmospheric Narrator facilitating a slice-of-life romance scene. Manage the environment, sensory details, and minor background characters. Never write dialogue or internal thought for the player characters. Keep descriptions brief; set the immediate scene and step back.",
               "ttrpg": "You are the Dungeon Master for this tabletop-style story. Describe environments, adjudicate the outcomes of stated actions, control NPCs and monsters, and drive the plot forward when appropriate, including initiating combat, presenting choices, and introducing complications. Do not narrate actions or dialogue for the player characters themselves. Be concise but give players clear information to act on."}

router = APIRouter()

def init_module(env: dict):
    global ENV, WS, IM, AIM
    ENV.update(env)
    WS = env["ws"]
    IM = env.get("IM")
    AIM = env["tools"]["ai_manager"]
    IM.scripts["rp_ai_settings_save"] = [_h_ai_settings_save]
    IM.scripts["rp_ai_conn_change"] = [_h_ai_conn_change]

def ai_server_enabled() -> bool:
    try: return json.loads(_AI_CONFIG_PATH.read_text()).get("enabled", True) if _AI_CONFIG_PATH.exists() else True
    except Exception: return True

def set_ai_server_enabled(val: bool):
    _AI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _AI_CONFIG_PATH.write_text(json.dumps({"enabled": bool(val)}))

def _load_lore_text(folders: list, max_chars: int = 4000) -> str:
    """Concatenates markdown files from the listed wiki-relative folders, truncated to a budget - a simple organizer, not a retrieval system."""
    if not folders: return ""
    chunks = []
    for folder in folders:
        base = (LORE_ROOT / folder.strip()).resolve()
        if not str(base).startswith(str(LORE_ROOT.resolve())) or not base.is_dir(): continue
        for f in sorted(base.glob("*.md")): chunks.append(f"### {f.stem}\n{f.read_text(encoding='utf-8', errors='ignore')}")
    return "\n\n".join(chunks)[:max_chars]

# --- Helpers ---

def _esc(s): return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")
def _cfg(room: Room) -> dict: return {**_DEFAULT, **(room.info or {}).get("ai", {})}

def _save_cfg(room: Room, cfg: dict, db):
    info = dict(room.info or {}); info["ai"] = cfg
    room.info = info; flag_modified(room, "info"); db.commit()

# --- Context Building ---

def _recent(room_id: str, n: int, db) -> list: return db.query(Message).filter(Message.room_id == room_id, Message.deleted == False, Message.user_name != _AI_USER).order_by(Message.created_at.asc()).all()[-n:]

def _compress(room: Room, cfg: dict, db) -> str:
    all_msgs = db.query(Message).filter(Message.room_id == room.id, Message.deleted == False, Message.user_name != _AI_USER).order_by(Message.created_at.asc()).all()
    depth = int(cfg.get("history_depth", 20))
    if len(all_msgs) <= depth: return cfg.get("history_summary", "")
    old = all_msgs[:-depth]
    lines = [f"[{m.persona_name}]: {m.content[:100]}" for m in old[-30:]]
    ex = cfg.get("history_summary", "")
    return (ex + " | " if ex else "") + " | ".join(lines)

def _build_messages(room: Room, cfg: dict, db) -> list:
    mode = cfg.get("mode","off")
    recent = _recent(room.id, int(cfg.get("history_depth",20)), db)
    world = (room.info or {}).get("world_detail","") or (room.info or {}).get("world","")
    scenario = cfg.get("scenario",""); summary = cfg.get("history_summary","")
    lore_raw = (room.info or {}).get("lore_folders", "")
    lore_folders = [x for x in lore_raw.split(",") if x.strip()] if isinstance(lore_raw, str) else (lore_raw or [])
    lore_text = _load_lore_text(lore_folders)
    history_text = "\n".join(f"{m.persona_name}: {m.content}" for m in recent)
    if mode == "character":
        pname = cfg.get("persona", "AI")
        persona_obj = db.query(Persona).filter(Persona.name == pname).first()
        char_desc = (persona_obj.description or "") if persona_obj else ""
        instructions = (persona_obj.ai_instructions or "") if persona_obj else ""
        system = f"You are {pname} in this collaborative story. Stay fully in character. Do not acknowledge being an AI. Respond as {pname} in first person, one or two paragraphs at most."
        if char_desc: system += f"\n\nCharacter description:\n{char_desc}"
        if instructions: system += f"\n\nAdditional instructions:\n{instructions}"
    else:
        room_mode = (room.info or {}).get("mode", "general")
        system = _DM_PROMPTS.get(room_mode, _DM_PROMPTS["general"])
    msgs = [{"role": "system", "content": system}]
    ctx_parts = []
    if world: ctx_parts.append(f"[WORLD]\n{world}")
    if lore_text: ctx_parts.append(f"[LORE]\n{lore_text}")
    if scenario: ctx_parts.append(f"[SCENARIO]\n{scenario}")
    if summary: ctx_parts.append(f"[PRIOR EVENTS]\n{summary}")
    if ctx_parts: msgs += [{"role":"user","content":"\n\n".join(ctx_parts)+"\n\n[BEGIN]"}, {"role":"assistant","content":"Understood, I am ready."}]
    msgs.append({"role":"user","content":(f"[RECENT]\n{history_text}\n\nContinue." if history_text else "Begin the scene.")})
    return msgs

# --- Generation (via ai_manager, provider-agnostic) ---

async def _generate(cfg: dict, messages: list) -> str:
    conn = AIM.connections.get_conn(cfg.get("conn_id",""))
    model = cfg.get("model","")
    if not conn: print("[rp_ai] no connection configured for this room"); return ""
    if not model: print("[rp_ai] no model configured for this room"); return ""
    full = ""
    try:
        async for text, _thinking in AIM.connections.stream_llm(conn, messages, model, temperature=0.85, num_predict=256): full += text
    except Exception as e:
        print(f"[rp_ai] generation error: {e}")
    return full.strip()

# --- Trigger Logic ---

def _should_trigger(cfg: dict, room_id: str, sender: str, db) -> bool:
    if sender == _AI_USER: return False
    trigger = cfg.get("trigger","every_n"); val = cfg.get("trigger_value",3)
    if trigger == "every_n":
        count = int(cfg.get("trigger_count",0)) + 1
        cfg["trigger_count"] = count
        return count >= int(val)
    if trigger == "probability": return random.random() < float(val)
    if trigger == "keyword":
        last = db.query(Message).filter(Message.room_id == room_id, Message.user_name != _AI_USER).order_by(Message.created_at.desc()).first()
        if not last: return False
        keywords = [k.strip().lower() for k in str(val).split(",") if k.strip()]
        return any(kw in last.content.lower() for kw in keywords)
    if trigger == "manual":
        last = db.query(Message).filter(Message.room_id == room_id).order_by(Message.created_at.desc()).first()
        return last and any(tag in last.content.lower() for tag in ["@ai","@dm","@gm"])
    return False

# --- Post AI Message ---

def _ai_bubble(msg: Message, pname: str) -> str:
    content = _esc(msg.content or ""); t = msg.created_at.strftime("%H:%M") if msg.created_at else ""
    return f"""<div class="cm-msg cm-other" id="cm-msg-{msg.id}" data-msg-id="{msg.id}">
                   <div class="cm-avatar" style="background:var(--accent_dim);color:var(--accent);border-color:var(--accent)">{pname[0].upper() if pname else "A"}</div>
                   <div class="cm-bwrap">
                       <div class="cm-bubble">
                           <div class="cm-meta" style="color:var(--accent)">{_esc(pname)} - {t}</div>
                           <div class="cm-content"><p style="margin:.12rem 0">{content}</p></div>
                       </div>
                   </div>
               </div>"""

async def _post_message(room_id: str, pname: str, content: str, db):
    msg = Message(room_id=room_id, user_name=_AI_USER, persona_name=pname, content=content)
    db.add(msg); db.commit(); db.refresh(msg)
    await WS.broadcast(f'<div id="cm-msgs-{room_id}" hx-swap-oob="beforeend">{_ai_bubble(msg, pname)}</div>')

# --- Public Hook ---

async def on_message_sent(room_id: str, msg_id: int, sender_username: str):
    db = SessionLocal()
    try:
        room = db.get(Room, room_id)
        if not room: return
        cfg = _cfg(room)
        if cfg.get("mode","off") == "off": return
        if not _should_trigger(cfg, room_id, sender_username, db): return
        cfg["trigger_count"] = 0
        cfg["history_summary"] = _compress(room, cfg, db)
        _save_cfg(room, cfg, db)
        messages = _build_messages(room, cfg, db)
        await WS.broadcast(f'<div id="rp-ai-working-{room_id}" hx-swap-oob="innerHTML"><div class="glass" style="padding:.3rem .6rem;font-size:.7rem;color:var(--text_muted);display:flex;align-items:center;gap:.4rem"><span class="spin">&#x25CC;</span> AI is thinking&hellip;</div></div>')
        response = await _generate(cfg, messages)
        await WS.broadcast(f'<div id="rp-ai-working-{room_id}" hx-swap-oob="innerHTML"></div>')
        if response:
            pname = cfg.get("persona","AI") if cfg.get("mode") == "character" else cfg.get("dm_persona","DM")
            await _post_message(room_id, pname, response, db)
    except Exception as e:
        print(f"[rp_ai] on_message_sent error: {e}")
        await WS.broadcast(f'<div id="rp-ai-working-{room_id}" hx-swap-oob="innerHTML"></div>')
    finally:
        db.close()

# --- Routes ---

def _get_room_or_403(room_id: str, user, db):
    room = db.get(Room, room_id)
    if not room or (room.owner != user.username and getattr(user,"role","") not in ("admin","moderator")): return None, HTMLResponse("Forbidden", status_code=403)
    return room, None

@router.get("/settings/{room_id}", response_class=HTMLResponse)
async def ai_settings(room_id: str, request: Request):
    if not ai_server_enabled(): return HTMLResponse("")
    db = SessionLocal()
    try:
        room, err = _get_room_or_403(room_id, request.state.user, db)
        if err: return err
        cfg = _cfg(room)
        personas = db.query(Persona).filter(Persona.owner_username == request.state.user.username).all()
        p_opts = '<option value="">-- select --</option>' + "".join(f'<option value="{_esc(p.name)}" {"selected" if p.name==cfg.get("persona","") else ""}>{_esc(p.name)}</option>' for p in personas)
        mode_opts = "".join(f'<option value="{m}" {"selected" if m==cfg.get("mode","off") else ""}>{l}</option>' for m,l in [("off","Off"),("character","Character - AI plays a persona"),("dm","DM/Narrator - AI drives the story")])
        t_opts = "".join(f'<option value="{t}" {"selected" if t==cfg.get("trigger","every_n") else ""}>{l}</option>' for t,l in [("every_n","Every N messages"),("probability","Probability (0.0-1.0)"),("keyword","Keyword in message"),("manual","Manual only (@AI, @DM, @GM)")])
        conn = AIM.connections.get_conn(cfg.get("conn_id",""))
        models = await AIM.connections.list_models_async(conn) if conn else []
        conn_opts = '<option value="">-- select --</option>' + AIM.connections.conn_opts_html(cfg.get("conn_id",""))
        model_opts = "".join(f'<option value="{_esc(m)}" {"selected" if m==cfg.get("model","") else ""}>{_esc(m)}</option>' for m in models) or '<option value="">-- select a connection first --</option>'
        return HTMLResponse(f"""<div class="glass rp-ui-modal"><button style="position:absolute;top:.5rem;right:.5rem;background:none;border:none;cursor:pointer;font-size:1rem;color:var(--text_muted)" onclick="document.getElementById('rp-modal').innerHTML=''">&#x2715;</button>
<h3 style="margin-top:0;color:var(--accent)">AI Settings - {_esc(room.title or room_id)}</h3>
<form hx-post="/im/in" hx-target="body" hx-swap="none" style="display:flex;flex-direction:column;gap:.6rem">
    <input type="hidden" name="type" value="rp_ai_settings_save">
    <input type="hidden" name="branch" value="{IM.branch_id}">
    <input type="hidden" name="lvl" value="1">
    <input type="hidden" name="room_id" value="{_esc(room_id)}">
    <label style="font-size:.75rem;color:var(--text_muted)">Mode<select name="mode" class="module-select" style="width:100%;margin-top:.2rem">{mode_opts}</select></label>
    <label style="font-size:.75rem;color:var(--text_muted)">Character Persona (character mode)<select name="persona" class="module-select" style="width:100%;margin-top:.2rem">{p_opts}</select></label>
    <label style="font-size:.75rem;color:var(--text_muted)">DM Name (DM mode)<input type="text" name="dm_persona" value="{_esc(cfg.get("dm_persona","DM"))}" class="module-select" style="width:100%;margin-top:.2rem"></label>
    <label style="font-size:.75rem;color:var(--text_muted)">Connection<select name="conn_id" class="module-select" style="width:100%;margin-top:.2rem" hx-post="/im/in" hx-vals='{{"type":"rp_ai_conn_change","branch":"{IM.branch_id}","lvl":1}}' hx-trigger="change" hx-include="this">{conn_opts}</select></label>
    <div style="font-size:.75rem;color:var(--text_muted)">Model</div>
    <div id="rp-ai-model-wrap"><select name="model" class="module-select" style="width:100%">{model_opts}</select></div>
    <label style="font-size:.75rem;color:var(--text_muted)">Trigger<select name="trigger" class="module-select" style="width:100%;margin-top:.2rem">{t_opts}</select></label>
    <label style="font-size:.75rem;color:var(--text_muted)">Trigger value (N, prob 0-1, or comma keywords)<input type="text" name="trigger_value" value="{_esc(str(cfg.get("trigger_value","3")))}" class="module-select" style="width:100%;margin-top:.2rem"></label>
    <label style="font-size:.75rem;color:var(--text_muted)">Scenario Block<textarea name="scenario" class="module-select" rows="3" style="width:100%;margin-top:.2rem;font-size:.8rem;resize:vertical">{_esc(cfg.get("scenario",""))}</textarea></label>
    <div style="display:flex;gap:.5rem">
        <button type="submit" class="button" style="flex:1;margin-top:0">Save</button>
        <button type="button" class="button" style="margin-top:0;background:none;border-color:var(--accent)" hx-post="/module/rp_server/ai/trigger/{_esc(room_id)}" hx-target="#rp-modal" hx-swap="innerHTML">Trigger Now</button>
        <button type="button" class="button" style="margin-top:0;background:none;border-color:#ff9a3c;color:#ff9a3c" hx-post="/module/rp_server/ai/clear_history/{_esc(room_id)}" hx-swap="none" hx-confirm="Clear AI history summary?">Clear History</button>
    </div>
</form></div>""")
    finally: db.close()

async def _h_ai_conn_change(request, payload, imr):
    conn = AIM.connections.get_conn(payload.get("conn_id",""))
    models = await AIM.connections.list_models_async(conn) if conn else []
    opts = "".join(f'<option value="{_esc(m)}">{_esc(m)}</option>' for m in models) or '<option value="">-- select a connection first --</option>'
    return imr.oob(f'<select name="model" class="module-select" style="width:100%">{opts}</select>', "rp-ai-model-wrap", swap="innerHTML")

async def _h_ai_settings_save(request, payload, imr):
    if not ai_server_enabled(): return imr.status("AI features are disabled server-wide.", "error")
    db = SessionLocal()
    try:
        room_id = payload.get("room_id","")
        room = db.get(Room, room_id)
        user = request.state.user
        if not room or (room.owner != user.username and getattr(user,"role","") not in ("admin","moderator")): return imr.status("Forbidden.", "error")
        cfg = _cfg(room)
        for k in ("mode","persona","dm_persona","conn_id","model","trigger","trigger_value","scenario"):
            cfg[k] = payload.get(k, cfg.get(k,""))
        cfg.setdefault("trigger_count",0); cfg.setdefault("history_summary","")
        _save_cfg(room, cfg, db)
        mode = cfg.get("mode","off")
        label = {"off":"AI: Off","character":f"AI: {cfg.get('persona','?')}","dm":f"AI: {cfg.get('dm_persona','DM')}"}.get(mode, "AI: Off")
        color = "var(--text_muted)" if mode == "off" else "var(--accent)"
        imr.oob(f"""<button class="ui-btn rp-menu-btn" style="justify-content:space-between;color:{color};" hx-get="/module/rp_server/ai/settings/{room_id}" hx-target="#rp-modal" hx-swap="innerHTML"><span>&#x1F916; {label}</span><span style="opacity:.6;font-size:.7rem;">&#x2699;</span></button>""", "rp-ai-toolbar")
        return imr.status("AI settings saved.", "ok")
    finally: db.close()

@router.post("/trigger/{room_id}", response_class=HTMLResponse)
async def ai_trigger_now(room_id: str, request: Request):
    if not ai_server_enabled(): return HTMLResponse("")
    db = SessionLocal()
    try:
        room, err = _get_room_or_403(room_id, request.state.user, db)
        if err: return err
    finally: db.close()
    asyncio.create_task(on_message_sent(room_id, 0, "__manual__"))
    return HTMLResponse('<div class="glass rp-ui-modal" style="padding:1.5rem"><p style="color:var(--accent)">&#x25B6; AI response triggered.</p><button onclick="document.getElementById(\'rp-modal\').innerHTML=\'\'" class="button" style="margin-top:.5rem">Close</button></div>')

@router.post("/clear_history/{room_id}", response_class=HTMLResponse)
async def ai_clear_history(room_id: str, request: Request):
    if not ai_server_enabled(): return HTMLResponse("")
    db = SessionLocal()
    try:
        room, err = _get_room_or_403(room_id, request.state.user, db)
        if err: return err
        cfg = _cfg(room); cfg["history_summary"] = ""; _save_cfg(room, cfg, db)
    finally: db.close()
    return HTMLResponse(" ")

@router.get("/toolbar/{room_id}", response_class=HTMLResponse)
async def ai_toolbar(room_id: str, request: Request):
    if not ai_server_enabled(): return HTMLResponse("")
    db = SessionLocal()
    try:
        room = db.get(Room, room_id)
        if not room: return HTMLResponse("")
        cfg = _cfg(room)
        mode = cfg.get("mode","off")
        label = {"off":"AI: Off","character":f"AI: {cfg.get('persona','?')}","dm":f"AI: {cfg.get('dm_persona','DM')}"}.get(mode, "AI: Off")
        color = "var(--text_muted)" if mode == "off" else "var(--accent)"
        return HTMLResponse(f"""<button class="ui-btn rp-menu-btn" style="justify-content:space-between;color:{color};" hx-get="/module/rp_server/ai/settings/{room_id}" hx-target="#rp-modal" hx-swap="innerHTML"><span>&#x1F916; {label}</span><span style="opacity:.6;font-size:.7rem;">&#x2699;</span></button>""")
    finally: db.close()