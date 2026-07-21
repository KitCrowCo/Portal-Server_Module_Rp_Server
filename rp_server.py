"""
RP Server - collaborative roleplay/chat rooms.
Shared modular components: InterfaceManager intents (IM.scripts, dispatched through /im/in) instead of one route per action; 
                           FileManager for uploads, rooted at data/rp_server/static/;
                           SettingsGroup for the two settings surfaces; and the shared theme cascade for per-user appearance instead of ad hoc color storage.

AI (rp_ai.py) is a fully optional layer: if the file isn't present, or a server-wide toggle is off, or a room's owner hasn't enabled it, none of its UI renders anywhere - not hidden with CSS, simply never generated.
"""
import uuid, json, pathlib, datetime, re, asyncio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from modules.rp_server.models import *
from modules.rp_server.rp_tools import router as _tools_router, init_module as _tools_init_module

router = APIRouter()
router.include_router(_tools_router, prefix="/tools")

try:
    from modules.rp_server.rp_ai import router as _ai_router, on_message_sent as _ai_on_message, init_module as _ai_init_module, ai_server_enabled, set_ai_server_enabled
    router.include_router(_ai_router, prefix="/ai")
    _AI_INSTALLED = True
except ImportError:
    _AI_INSTALLED = False
    async def _ai_on_message(*a, **kw): pass
    def ai_server_enabled() -> bool: return False
    def set_ai_server_enabled(v: bool): pass

MODULE_META = {"label": "RP Server", "icon": "&#x1F4AC;", "description": "Collaborative roleplay, TTRPG, and chat rooms", "persistence": "user"}

_P = "/module/rp_server"
RP_ASSET_DIR = pathlib.Path("./data/rp_server/static")
RP_ASSET_URL = "/module/assets/rp_server"

ENV, IM, UI, BI, CM, FM, RoomSettingsGroup = {}, None, None, None, None, None, None

def init_module(environment: dict):
    global ENV, IM, UI, BI, CM, FM, _RoomSettingsGroup
    ENV.update(environment)
    UI = ENV["templates"].env.globals.get("UI")
    BI = ENV["tools"]["built_ins"]
    IM = ENV["InterfaceManager"](nesting_level=1, db_path="rp_server_im.db")
    FM = BI.FileManager(RP_ASSET_DIR)
    CM = BI.ChatManager(namespace="rp_server", base_url=_P, view_style="bubble", allow_edit=False, allow_delete=False, allow_copy=True, show_avatars=True, show_info=False, markdown_mode="extended", pin_enabled=True, input_enabled=False, branch_id=IM.branch_id, nesting_level=1)
    _register_intents()
    if _AI_INSTALLED: _ai_init_module(environment)
    _tools_init_module({**environment, "IM": IM})
    
   
    class RoomSettingsGroup(BI.SettingsGroup):
        """Reuses SettingsGroup's field rendering, but persists to Room.info (DB) instead of a JSON file."""
        def __init__(self, room, db):
            fields = [BI.SettingField("mode", "Mode", "select", "general", options=[("general","General Chat"),("otome","Visual Novel"),("ttrpg","TTRPG / D&D")]),
                      BI.SettingField("display_mode", "Display Names", "select", "persona", options=[("persona","Persona name"),("username","Username"),("both","Both")]),
                      BI.SettingField("show_timestamp", "Show Timestamps", "checkbox", True),
                      BI.SettingField("world", "Setting / World (short name)", "text", ""),
                      BI.SettingField("world_detail", "World Detail", "textarea", ""),
                      BI.SettingField("lore_folders", "Lore Folders (wiki paths, comma-separated)", "text", "", hint="Relative to the wiki root, e.g. 'worlds/faerun, characters'. Stackable - both this panel and AI context (if enabled) pull markdown from every listed folder.")]
            super().__init__("room", "Room Settings", fields, json_path="unused")
            self.room, self.db = room, db
        def load(self): return {f.name: (self.room.info or {}).get(f.name, f.default) for f in self.fields}
        def save(self, form_data):
            data = dict(self.room.info or {})
            for f in self.fields: data[f.name] = bool(form_data.get(f.name)) if f.type == "checkbox" else (form_data.get(f.name) if form_data.get(f.name) is not None else f.default)
            self.room.info = data; flag_modified(self.room, "info"); self.db.commit()
    _RoomSettingsGroup = RoomSettingsGroup

    print("RP Server: environment loaded.")

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

# --- DB helpers ---

def get_user_session(db, username):
    sess = db.get(UserSession, username)
    if sess is None:
        sess = UserSession(username=username, current_room="Public", current_persona=username)
        db.add(sess); db.commit(); db.refresh(sess)
    return sess

def can_access_room(db, room, username, role):
    if room.room_type == "public" or role == "admin" or room.owner == username: return True
    return db.query(RoomMembership).filter_by(room_id=room.id, username=username).first() is not None

def ensure_public_room(db):
    if not db.get(Room, "Public"): db.add(Room(id="Public", owner="admin", room_type="public", title="Public")); db.commit()

def room_label(room): return room.title or room.id

def bubble_html(m, my_persona, username="", room_owner="", sprites=None, display_mode="persona", show_timestamp=True):
    sprites = sprites or {}
    shown = m.persona_name if display_mode == "persona" else m.user_name if display_mode == "username" else f"{m.persona_name} ({m.user_name})"
    t = getattr(m, "story_time", None) or m.created_at
    msg_dict = {"id": str(m.id), "role": "user", "content": m.content or "", "user_name": shown, "timestamp": t.isoformat() if (t and show_timestamp) else ""}
    is_me = (m.persona_name == my_persona)
    can_del = (m.user_name == username or room_owner == username)
    actions = (f'<button class="cm-act" hx-get="{_P}/messages/edit_modal?msg_id={m.id}" hx-target="#rp-modal" hx-swap="innerHTML">&#x270E;</button>' if m.user_name == username else "") + (f"""<button class="cm-act" hx-post="/im/in" hx-vals='{{"type":"rp_msg_delete","msg_id":"{m.id}","branch":"{IM.branch_id}","lvl":1}}' hx-swap="none">&#x2715;</button>""" if can_del else "")
    rendered = CM.render_message(msg_dict, is_me=is_me, can_edit=False, can_delete=False, sprites=sprites)
    if actions and rendered.endswith("</div>"): rendered = rendered[:-6] + f'<div class="cm-acts-side">{actions}</div>' + "</div>"
    return rendered

def _rooms_html(db, username, role) -> str:
    active = get_user_session(db, username).current_room
    html = ""
    for r in db.query(Room).all():
        if not can_access_room(db, r, username, role): continue
        badge = f'<small style="opacity:0.5;font-size:0.7rem;">({UI.escape(r.room_type)})</small>'
        html += f"""<div class="rp-room-item {"active" if r.id == active else ""}" hx-post="/im/in" hx-vals='{{"type":"rp_room_join","room_id":"{UI.escape(r.id)}","branch":"{IM.branch_id}","lvl":1}}' hx-swap="none">
                        <span>{UI.escape(room_label(r))} {badge}</span>
                        <small style="opacity:0.5;">{UI.escape(r.owner or "")}</small>
                    </div>"""
    return html

def _stage_html(room) -> str:
    bg = room.background if room else ""
    return f"""<div style="height:100%;display:flex;align-items:center;justify-content:center;padding:1rem;">{f'<img src="{UI.escape(bg)}" style="max-width:100%;max-height:100%;object-fit:contain;">' if bg else ''}</div>"""

def _messages_html(db, room_id, username) -> str:
    room = db.get(Room, room_id)
    persona = get_user_session(db, username).current_persona or username
    messages = db.query(Message).filter(Message.room_id == room_id, Message.deleted == False).order_by(Message.created_at.asc()).limit(120).all()
    info = room.info or {} if room else {}
    display_mode, show_ts = info.get("display_mode", "persona"), info.get("show_timestamp", True)
    names = {m.persona_name for m in messages if m.persona_name}
    sprites = {p.name: p.sprite for p in db.query(Persona).filter(Persona.name.in_(names)).all() if p.sprite} if names else {}
    html = "".join(bubble_html(m, persona, username, room.owner if room else "", sprites, display_mode, show_ts) for m in messages)
    return html or '<div class="cm-empty">No messages yet.</div>'

def _room_bundle(request, db, room_id, username) -> dict:
    role = request.state.user.role
    room = db.get(Room, room_id)
    return {"rooms": _rooms_html(db, username, role), "stage": _stage_html(room), "messages": _messages_html(db, room_id, username),
            "label": UI.escape(room_label(room)) if room else UI.escape(room_id),
            "bridge": f'<input type="hidden" name="room_id" id="rp-room-bridge" value="{UI.escape(room_id)}">'}

def _personas_html(username, active, db) -> str:
    personas = db.query(Persona).filter_by(owner_username=username).all()
    sprites = {p.name: p.sprite for p in personas if p.sprite}
    items = [{"id": "__username__", "name": username, "pid": None, "is_active": (active == username or not active)}]
    for p in personas: items.append({"id": p.id, "name": p.name, "pid": p.id, "is_active": (active == p.name)})
    html = ""
    for item in items:
        act = " active" if item["is_active"] else ""
        avatar = (f'<img src="{UI.escape(sprites.get(item["name"],""))}" style="width:1.4rem;height:1.4rem;border-radius:50%;object-fit:cover;flex-shrink:0;">' if sprites.get(item["name"]) else f'<div style="width:1.4rem;height:1.4rem;border-radius:50%;background:var(--accent_dim);flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:.6rem;color:var(--accent);">{UI.escape(item["name"][0].upper())}</div>')
        edit_btn = (f'<button class="btn-icon" style="flex-shrink:0;font-size:.7rem;" hx-get="{_P}/personas/select_edit/{item["id"]}" hx-target="#rp-modal" hx-swap="innerHTML" onclick="event.stopPropagation()">&#x270E;</button>') if item["pid"] else ""
        html += f"""<div class="rp-persona-item{act}"><div class="rp-persona-select" style="display:flex;align-items:center;gap:.4rem;flex:1;min-width:0;" hx-post="/im/in" hx-vals='{{"type":"rp_persona_select","persona_id":"{item["id"]}","branch":"{IM.branch_id}","lvl":1}}' hx-swap="none">{avatar}<span style="font-weight:{"600" if item["is_active"] else "400"};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{UI.escape(item["name"])}</span></div>{edit_btn}</div>"""
    return html

def _members_html(room_id, members) -> str:
    if not members: return '<div style="opacity:0.5;font-size:0.8rem;">No members yet.</div>'
    return "".join(f"""<div style="display:flex;justify-content:space-between;padding:0.2rem 0;"><span>{UI.escape(m.username)}</span><button class="ui-btn" style="padding:0.1rem 0.4rem;font-size:0.75rem;" hx-post="/im/in" hx-vals='{{"type":"rp_room_member_remove","room_id":"{room_id}","username":"{UI.escape(m.username)}","branch":"{IM.branch_id}","lvl":1}}' hx-target="#rp-member-list" hx-swap="innerHTML">&#x2715;</button></div>""" for m in members)

def _persona_edit_modal(p: Persona) -> str:
    sprite_preview = (f'<img src="{UI.escape(p.sprite)}" style="width:4rem;height:4rem;border-radius:50%;object-fit:cover;border:2px solid var(--accent);">' if p.sprite else f'<div style="width:4rem;height:4rem;border-radius:50%;background:var(--accent_dim);border:2px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:1.5rem;color:var(--accent);">{UI.escape(p.name[0].upper())}</div>')
    ai_block = ""
    if _AI_INSTALLED and ai_server_enabled():
        ai_block = f"""<label style="display:flex;align-items:center;gap:.4rem;font-size:.8rem;margin-top:.5rem;"><input type="checkbox" name="ai_enabled" value="1" {"checked" if p.ai_enabled else ""}> Available for AI character-mode</label>
       				   {UI.field("AI Instructions (added to this persona's AI prompt)", UI.textarea("ai_instructions", value=p.ai_instructions or "", rows=3))}"""
    body = f"""<div style="display:flex;align-items:center;gap:1rem;margin-bottom:1rem;">
                   {sprite_preview}
                   <form hx-post="/im/in" hx-encoding="multipart/form-data" hx-target="#rp-modal" hx-swap="innerHTML" style="display:flex;flex-direction:column;gap:0.3rem;">
                       <input type="hidden" name="type" value="rp_persona_sprite"><input type="hidden" name="branch" value="{IM.branch_id}"><input type="hidden" name="lvl" value="1">
                       <input type="hidden" name="persona_id" value="{p.id}">
                       <label style="font-size:0.75rem;opacity:0.7;">Avatar image</label>
                       <input type="file" name="sprite" accept="image/*" style="font-size:0.75rem;">
                       <button class="ui-btn" style="font-size:0.8rem;">Upload</button>
                   </form>
               </div>
               <form hx-post="/im/in" hx-swap="none" style="display:flex;flex-direction:column;gap:0.6rem;">
                   <input type="hidden" name="type" value="rp_persona_update"><input type="hidden" name="branch" value="{IM.branch_id}"><input type="hidden" name="lvl" value="1">
                   <input type="hidden" name="persona_id" value="{p.id}">
                   {UI.field("Name", UI.input("name", value=p.name))}
                   {UI.field("Description", UI.textarea("description", value=p.description or "", rows=4))}
                   {ai_block}
                   <button class="ui-btn" style="color:var(--accent);">Save</button>
               </form>
               <div style="margin-top:1rem;padding-top:.8rem;border-top:1px solid var(--border);">
                   <button class="ui-btn" style="color:#ff6b6b;border-color:#ff6b6b;font-size:.8rem;width:100%;" hx-post="/im/in" hx-vals='{{"type":"rp_persona_delete","persona_id":"{p.id}","branch":"{IM.branch_id}","lvl":1}}' hx-confirm="Delete this persona?" hx-swap="none">Delete Persona</button>
               </div>"""
    return UI.modal("rp-persona-edit", f"Persona: {UI.escape(p.name)}", body)

def _user_options_group(username) -> "BI.SettingsGroup": return BI.SettingsGroup("options", "Options", [BI.SettingField("auto_scroll", "Auto-scroll on new message", "checkbox", False)], json_path=f"./data/rp_server/user_options/{username}.json")

# --- IM Intents (all mutations - dispatched via POST /im/in) ---

def _register_intents():
    IM.scripts.update({"rp_room_create": [_h_room_create], "rp_room_join": [_h_room_join],
                       "rp_room_settings_save": [_h_room_settings_save],
                       "rp_room_member_add": [_h_room_member_add], "rp_room_member_remove": [_h_room_member_remove],
                       "rp_room_bg_upload": [_h_room_bg_upload], "rp_room_bg_clear": [_h_room_bg_clear],
                       "rp_persona_create": [_h_persona_create], "rp_persona_select": [_h_persona_select],
                       "rp_persona_update": [_h_persona_update], "rp_persona_delete": [_h_persona_delete], "rp_persona_sprite": [_h_persona_sprite],
                       "rp_msg_send": [_h_msg_send], "rp_msg_delete": [_h_msg_delete], "rp_msg_edit": [_h_msg_edit],
                       "rp_options_save": [_h_options_save]})

async def _h_room_create(request, payload, imr):
    db = SessionLocal()
    try:
        username = request.state.user.username
        title = (payload.get("title") or "").strip()[:120]
        if not title: return imr.status("Title required.", "error")
        room_id = re.sub(r"[^a-z0-9-]", "", title.lower().replace(" ", "-"))[:64] or uuid.uuid4().hex[:12]
        base_id, counter = room_id, 1
        while db.get(Room, room_id): room_id = f"{base_id}-{counter}"; counter += 1
        db.add(Room(id=room_id, owner=username, room_type="private", title=title)); db.commit()
        sess = get_user_session(db, username); sess.current_room = room_id; db.commit()
        b = _room_bundle(request, db, room_id, username)
        imr.oob(b["rooms"], "rp-rooms"); imr.oob(b["stage"], "rp-stage"); imr.oob(b["messages"], "rp-messages")
        imr.oob(b["label"], "rp-room-label"); imr.oob(b["bridge"], "rp-room-bridge")
        if _AI_INSTALLED and ai_server_enabled(): imr.trigger("rpAIUpdate")
        return imr
    finally: db.close()

async def _h_room_join(request, payload, imr):
    db = SessionLocal()
    try:
        username, role = request.state.user.username, request.state.user.role
        room_id = payload.get("room_id", "")
        room = db.get(Room, room_id)
        if not room: return imr.status("Room not found.", "error")
        if not can_access_room(db, room, username, role): return imr.status("Access denied.", "error")
        sess = get_user_session(db, username); sess.current_room = room_id; db.commit()
        b = _room_bundle(request, db, room_id, username)
        imr.oob(b["rooms"], "rp-rooms"); imr.oob(b["stage"], "rp-stage"); imr.oob(b["messages"], "rp-messages")
        imr.oob(b["label"], "rp-room-label"); imr.oob(b["bridge"], "rp-room-bridge")
        if _AI_INSTALLED and ai_server_enabled(): imr.trigger("rpAIUpdate")
        return imr
    finally: db.close()

async def _h_room_settings_save(request, payload, imr):
    db = SessionLocal()
    try:
        username, role = request.state.user.username, request.state.user.role
        room = db.get(Room, payload.get("room_id", ""))
        if not room or (room.owner != username and role not in ("admin", "moderator")): return imr.status("Forbidden.", "error")
        room.title = (payload.get("title") or room.title or room.id).strip()[:120]
        _RoomSettingsGroup(room, db).save(payload)
        imr.oob(UI.escape(room_label(room)), "rp-room-label")
        imr.oob(_rooms_html(db, username, role), "rp-rooms")
        return imr.status("Room settings saved.", "ok")
    finally: db.close()

async def _h_room_member_add(request, payload, imr):
    db = SessionLocal()
    try:
        me = request.state.user; room = db.get(Room, payload.get("room_id", ""))
        if not room or room.owner != (me.username if me else ""): return imr.status("Forbidden.", "error")
        uname = (payload.get("username") or "").strip()
        if uname and not db.query(RoomMembership).filter_by(room_id=room.id, username=uname).first():
            db.add(RoomMembership(room_id=room.id, username=uname)); db.commit()
        imr.raw(_members_html(room.id, db.query(RoomMembership).filter_by(room_id=room.id).all()))
        return imr
    finally: db.close()

async def _h_room_member_remove(request, payload, imr):
    db = SessionLocal()
    try:
        me = request.state.user; room = db.get(Room, payload.get("room_id", ""))
        if not room or room.owner != (me.username if me else ""): return imr.status("Forbidden.", "error")
        db.query(RoomMembership).filter_by(room_id=room.id, username=payload.get("username", "")).delete(); db.commit()
        imr.raw(_members_html(room.id, db.query(RoomMembership).filter_by(room_id=room.id).all()))
        return imr
    finally: db.close()

async def _h_room_bg_upload(request, payload, imr):
    db = SessionLocal()
    try:
        username, role = request.state.user.username, request.state.user.role
        room = db.get(Room, payload.get("room_id", ""))
        if not room or (room.owner != username and role not in ("admin", "moderator")): return imr.status("Forbidden.", "error")
        f = payload.get("bgfile")
        if not f or not getattr(f, "filename", ""): return imr.status("No file provided.", "error")
        ext = pathlib.Path(f.filename).suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"): return imr.status("Invalid file type.", "error")
        rel = f"backgrounds/{username}/{uuid.uuid4().hex}{ext}"
        FM.write_bytes(rel, await f.read())
        room.background = f"{RP_ASSET_URL}/{rel}"; db.commit()
        imr.oob(_stage_html(room), "rp-stage")
        return imr.status("Background updated.", "ok")
    finally: db.close()

async def _h_room_bg_clear(request, payload, imr):
    db = SessionLocal()
    try:
        username, role = request.state.user.username, request.state.user.role
        room = db.get(Room, payload.get("room_id", ""))
        if not room or (room.owner != username and role not in ("admin", "moderator")): return imr.status("Forbidden.", "error")
        room.background = ""; db.commit()
        imr.oob(_stage_html(room), "rp-stage")
        return imr.status("Background removed.", "ok")
    finally: db.close()

async def _h_persona_create(request, payload, imr):
    db = SessionLocal()
    try:
        username = request.state.user.username
        name = (payload.get("name") or "").strip()[:80]
        if not name: return imr.status("Name required.", "error")
        if db.query(Persona).filter_by(owner_username=username, name=name).first(): return imr.status(f"Persona '{name}' already exists.", "error")
        db.add(Persona(name=name, owner_username=username)); db.commit()
        sess = get_user_session(db, username)
        imr.oob(_personas_html(username, sess.current_persona, db), "rp-personas")
        return imr
    finally: db.close()

async def _h_persona_select(request, payload, imr):
    db = SessionLocal()
    try:
        username = request.state.user.username
        pid = payload.get("persona_id", "__username__")
        display = username
        if pid != "__username__":
            p = db.get(Persona, int(pid))
            if not p or p.owner_username != username: return imr.status("Not found.", "error")
            display = p.name
        sess = get_user_session(db, username); sess.current_persona = display; db.commit()
        imr.oob(_personas_html(username, display, db), "rp-personas")
        imr.oob(UI.escape(display), "rp-persona-label")
        return imr
    finally: db.close()

async def _h_persona_update(request, payload, imr):
    db = SessionLocal()
    try:
        username = request.state.user.username
        p = db.get(Persona, int(payload.get("persona_id", 0)))
        if not p or p.owner_username != username: return imr.status("Forbidden.", "error")
        p.name = (payload.get("name") or p.name).strip()[:80]
        p.description = (payload.get("description") or "").strip()
        p.ai_enabled = str(payload.get("ai_enabled", "")) == "1"
        p.ai_instructions = (payload.get("ai_instructions") or "").strip()
        db.commit()
        imr.oob(_personas_html(username, get_user_session(db, username).current_persona, db), "rp-personas")
        return imr.status("Saved.", "ok")
    finally: db.close()

async def _h_persona_delete(request, payload, imr):
    db = SessionLocal()
    try:
        username = request.state.user.username
        p = db.get(Persona, int(payload.get("persona_id", 0)))
        if not p or p.owner_username != username: return imr.status("Not found.", "error")
        db.delete(p); db.commit()
        imr.oob(_personas_html(username, get_user_session(db, username).current_persona, db), "rp-personas")
        imr.trigger("closeModal")
        return imr
    finally: db.close()

async def _h_persona_sprite(request, payload, imr):
    db = SessionLocal()
    try:
        username = request.state.user.username
        p = db.get(Persona, int(payload.get("persona_id", 0)))
        if not p or p.owner_username != username: return imr.status("Forbidden.", "error")
        f = payload.get("sprite")
        if not f or not getattr(f, "filename", ""): return imr.status("No file provided.", "error")
        ext = pathlib.Path(f.filename).suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"): return imr.status("Invalid file type.", "error")
        rel = f"sprites/{username}/{uuid.uuid4().hex}{ext}"
        FM.write_bytes(rel, await f.read())
        p.sprite = f"{RP_ASSET_URL}/{rel}"; db.commit()
        imr.raw(_persona_edit_modal(p))
        imr.oob(_personas_html(username, get_user_session(db, username).current_persona, db), "rp-personas")
        return imr
    finally: db.close()

async def _h_msg_send(request, payload, imr):
    db = SessionLocal()
    try:
        username = request.state.user.username
        room_id = payload.get("room_id", ""); content = (payload.get("content") or "").strip()
        if not content or not room_id: return imr
        room = db.get(Room, room_id)
        if not room: return imr.status("Room not found.", "error")
        sess = get_user_session(db, username); persona = sess.current_persona or username
        msg = Message(room_id=room_id, user_name=username, persona_name=persona, content=content)
        db.add(msg); db.commit(); db.refresh(msg)
        info = room.info or {}; display_mode, show_ts = info.get("display_mode", "persona"), info.get("show_timestamp", True)
        p_obj = db.query(Persona).filter_by(owner_username=username, name=persona).first()
        sprites = {persona: p_obj.sprite} if (p_obj and p_obj.sprite) else {}
        ws = ENV.get("ws")
        if ws:
            other_html = bubble_html(msg, "", "", room.owner, sprites, display_mode, show_ts)
            active = {s.username for s in db.query(UserSession).filter(UserSession.current_room == room_id).all()}
            for uid in (active - {username}): await ws.send_personal_message(f'<div id="rp-messages" hx-swap-oob="beforeend">{other_html}</div>', uid)
        send_push = ENV.get("send_push")
        if send_push:
            for w in db.query(RoomWatch).filter(RoomWatch.room_id == room_id, RoomWatch.notify == True).all():
                if w.username == username: continue
                wsess = db.get(UserSession, w.username)
                if wsess and wsess.current_room == room_id: continue
                await send_push(w.username, f"New in {room_label(room)}", f"{persona}: {content[:80]}")
        if _AI_INSTALLED and ai_server_enabled(): asyncio.create_task(_ai_on_message(room_id, msg.id, username))
        imr.raw(f'<div id="rp-messages" hx-swap-oob="beforeend">{bubble_html(msg, persona, username, room.owner, sprites, display_mode, show_ts)}</div>')
        return imr
    finally: db.close()

async def _h_msg_delete(request, payload, imr):
    db = SessionLocal()
    try:
        username, role = request.state.user.username, request.state.user.role
        msg = db.get(Message, int(payload.get("msg_id", 0)))
        if not msg: return imr
        room = db.get(Room, msg.room_id)
        if not (msg.user_name == username or (room and room.owner == username) or role in ("admin", "moderator")): return imr.status("Forbidden.", "error")
        msg.deleted = True; db.commit()
        imr.oob(_messages_html(db, msg.room_id, username), "rp-messages")
        return imr
    finally: db.close()

async def _h_msg_edit(request, payload, imr):
    db = SessionLocal()
    try:
        username = request.state.user.username
        msg = db.get(Message, int(payload.get("msg_id", 0)))
        if not msg or msg.user_name != username: return imr.status("Forbidden.", "error")
        msg.content = (payload.get("content") or "").strip()
        msg.edited_at = datetime.datetime.utcnow()
        if (payload.get("persona_name") or "").strip(): msg.persona_name = payload["persona_name"].strip()
        if (payload.get("story_time") or "").strip():
            try: msg.story_time = datetime.datetime.fromisoformat(payload["story_time"])
            except ValueError: pass
        db.commit()
        imr.oob(_messages_html(db, msg.room_id, username), "rp-messages")
        imr.trigger("closeModal")
        return imr
    finally: db.close()

async def _h_options_save(request, payload, imr):
    db = SessionLocal()
    try:
        username, role = request.state.user.username, request.state.user.role
        _user_options_group(username).save(payload)
        for r in db.query(Room).all():
            wants = str(payload.get(f"watch_{r.id}", "")) == "1"
            existing = db.query(RoomWatch).filter_by(room_id=r.id, username=username).first()
            if wants and not existing: db.add(RoomWatch(room_id=r.id, username=username, notify=True))
            elif not wants and existing: db.delete(existing)
        db.commit()
        override = await ENV["get_state"](request, scope="user", namespace="_theme_rp_server", key="overrides") or {}
        if payload.get("me_bg"): override["rp-bubble-me-bg"] = payload["me_bg"]
        if payload.get("other_bg"): override["rp-bubble-other-bg"] = payload["other_bg"]
        await ENV["set_state"](request, override, scope="user", namespace="_theme_rp_server", key="overrides")
        if _AI_INSTALLED and role == "admin": set_ai_server_enabled(str(payload.get("ai_server_enabled", "")) == "1")
        imr.trigger("closeModal")
        return imr.status("Settings saved.", "ok")
    finally: db.close()

# --- CSS / JS ---

RP_CSS = """
:root { --rp-item-gap: 0.15rem; --rp-avatar-size: 2rem; --rp-sidebar-padding: 0.15rem; }
.flex-col {display: flex !important; flex-direction: column !important; height: 100% !important;}
.rp-section-label {flex-shrink: 0; padding: 0.5rem 0.2rem 0.2rem 0.2rem; font-size: 0.75rem; font-weight: bold; text-transform: uppercase; opacity: 0.7; letter-spacing:.06rem;color:var(--text_muted);}
.fixed-shrink {flex-shrink: 0 !important;}
.rp-ui-modal {padding: 1.5rem; min-width: 20rem; max-width: 90vw; max-height: 85vh; position: relative; border-radius: var(--radius); overflow-y:auto;}
#rp-msg-input {field-sizing: content; flex:1;background:var(--bg); color:var(--text);border:var(--border-thick) solid var(--border);border-radius:var(--radius);padding:0.4rem;font-family:var(--font-main); font-size:var(--font-size); resize:none; overflow-y:auto; max-height:25vh; line-height:1.4;}
#rp-msg-input:focus{outline:none; border-color:var(--accent);}
.input-row { display: flex; gap: 0.4rem; align-items: center; width: 100%; }
.toolbar-header { padding: 0.4rem 0.6rem; border-bottom: var(--border-thick) solid var(--border); display: flex; justify-content: space-between; align-items: center; gap:.4rem; flex-shrink:0; background:var(--bg_panel);}
.rp-persona-item{display:flex; align-items:center; justify-content:space-between; padding:.2rem .5rem; font-size:.8rem; border-bottom:var(--border-thick) solid var(--border); cursor:pointer;}
.rp-persona-item:hover,.rp-persona-item.active{background:var(--accent_dim);}
#rp-modal { display:none; position:fixed; inset:0; z-index:2000; align-items:center; justify-content:center; background:rgba(0,0,0,.55); }
#rp-modal:not(:empty) { display:flex; }
.rp-menu-btn { display:flex; align-items:center; gap:.4rem; width:100%; text-align:left; padding:.45rem .6rem; font-size:.8rem; justify-content:flex-start; }
.cm-meta{flex-wrap:nowrap;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
#rp-scroll-btn{position:absolute;bottom:3.5rem; right:.7rem; z-index:100; opacity:.7;border-radius:50%; width:2rem;height:2rem;padding:0;display:flex;align-items:center;justify-content:center;}
#rp-scroll-btn:hover{opacity:1;}
details.rp-expand textarea{field-sizing:content;min-height:4rem;}
.rp-room-item { display:flex; justify-content:space-between; align-items:center; padding:.3rem .5rem; font-size:.8rem; border-bottom:var(--border-thick) solid var(--border); cursor:pointer; }
.rp-room-item:hover, .rp-room-item.active { background:var(--accent_dim); }
"""

RP_SCRIPT = """
document.addEventListener('input', function(e) {if (e.target.id !== 'rp-msg-input') return; var el = e.target; el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, window.innerHeight * 0.25) + 'px';});
document.addEventListener('keydown', function(e) {if (!(e.ctrlKey || e.metaKey) || e.key !== 'Enter') return; if (e.target.id !== 'rp-msg-input') return; e.preventDefault(); document.getElementById('rp-msg-form').requestSubmit();});
document.body.addEventListener('htmx:afterRequest', function(e) {if (!e.detail.elt || e.detail.elt.id !== 'rp-msg-form') return; var inp = document.getElementById('rp-msg-input'); if (inp) { inp.value = ''; inp.style.height = 'auto'; }});
(function(){
    var b = document.getElementById('rp-scroll-btn'), m = document.getElementById('rp-messages');
    if (m && b) m.addEventListener('scroll', function(){ b.style.display = (m.scrollHeight - m.scrollTop - m.clientHeight < 80) ? 'none' : 'flex'; });
})();
"""

# --- Main page ---

@router.get("/", response_class=HTMLResponse)
async def ui_index(request: Request, db: Session = Depends(get_db)):
    user = request.state.user
    username, role = user.username, user.role
    ensure_public_room(db)
    sess = get_user_session(db, username)
    room_id = sess.current_room or "Public"
    persona = sess.current_persona or username
    room_obj = db.get(Room, room_id)
    room_display = UI.escape(room_label(room_obj)) if room_obj else UI.escape(room_id)
    lvl = 1 #int(request.headers.get("x-shell-level", 1))
    ai_available = _AI_INSTALLED and ai_server_enabled()

    LeftBarContent = f"""<div style="display:flex; flex-direction:column; height:100%; overflow:hidden;">
                            <div style="height: 60vh; display: flex; flex-direction: column; padding: 0.4rem; box-sizing: border-box; overflow: hidden;">
                                <div class="fixed-shrink">
                                    <div class="rp-section-label">Rooms</div>
                                    <div id="rp-rooms" style="max-height: 25vh; overflow-y: auto; border-bottom:var(--border-thick) solid var(--border);" hx-get="{_P}/rooms" hx-trigger="load"></div>
                                    <div style="padding: 0.5rem 0;">
                                        <form hx-post="/im/in" hx-swap="none" class="input-row">
                                            <input type="hidden" name="type" value="rp_room_create"><input type="hidden" name="branch" value="{IM.branch_id}"><input type="hidden" name="lvl" value="1">
                                            <input name="title" class="module-select" placeholder="Room name..." style="flex:1;">
                                            <button class="ui-btn">+</button>
                                        </form>
                                    </div>
                                    <div class="rp-section-label">Personas</div>
                                </div>
                                <div id="rp-personas" style="flex: 1; overflow-y: auto; min-height: 0;" hx-get="{_P}/personas" hx-trigger="load"></div>
                            </div>
                            <div style="height: 40vh; padding: 0.4rem; border-top: var(--border-thick) solid var(--border); display: flex; flex-direction: column; gap: 0.3rem; justify-content: center;">
                                <button class="ui-btn rp-menu-btn" hx-get="{_P}/options" hx-target="#rp-modal">&#x2699; Options</button>
                                <button class="ui-btn rp-menu-btn" hx-get="{_P}/room/manage" hx-target="#rp-modal">&#x1F3E0; Room Settings</button>
                                <button class="ui-btn rp-menu-btn" hx-get="{_P}/persona/manage" hx-target="#rp-modal">&#x1F464; Persona Settings</button>
                                <button class="ui-btn rp-menu-btn" hx-get="{_P}/tools/menu" hx-target="#rp-modal">&#x1F3B2; RP Tools</button>
                            </div>
                            {f'<div id="rp-ai-toolbar" hx-get="{_P}/ai/toolbar/{room_id}" hx-trigger="load, rpAIUpdate from:body" hx-target="this" hx-swap="innerHTML" style="border-top:var(--border-thick) solid var(--border);padding:.3rem .4rem;"></div>' if ai_available else ""}
                        </div>"""

    RightBarContent = f"""<div style="display:flex;flex-direction:column;height:100%;width:100%;position:relative;">
                            <div class="toolbar-header fixed-shrink">
                                <span id="rp-room-label" style="font-weight:700;color:var(--accent);">{room_display}</span>
                                <span style="font-size:.75rem;opacity:.6;">as <b id="rp-persona-label">{UI.escape(persona)}</b></span>
                            </div>
                            <div id="rp-messages" class="cm-msgs" data-pinned="true" hx-get="{_P}/messages/latest" hx-trigger="load" hx-swap="innerHTML"></div>
                            <button id="rp-scroll-btn" class="btn-icon" style="display:none;" onclick="var m=document.getElementById('rp-messages');m.scrollTo({{top:m.scrollHeight,behavior:'smooth'}})">&#x25BC;</button>
                            <div class="fixed-shrink" style="border-top:var(--border-thick) solid var(--border);padding:.4rem;">
                                <form id="rp-msg-form" style="display:flex;flex-direction:column;gap:.25rem;" hx-post="/im/in" hx-include="this" hx-swap="none">
                                    <input type="hidden" name="type" value="rp_msg_send"><input type="hidden" name="branch" value="{IM.branch_id}"><input type="hidden" name="lvl" value="1">
                                    <div id="rp-bridge-container" hx-get="{_P}/room/bridge" hx-trigger="load" hx-target="this" hx-swap="innerHTML">
                                        <input type="hidden" name="room_id" id="rp-room-bridge" value="{UI.escape(room_id)}">
                                    </div>
                                    <div style="display:flex;gap:.4rem;align-items:flex-end;">
                                        <textarea id="rp-msg-input" name="content" placeholder="Say something\u2026 (Ctrl+Enter to send)" style="flex:1;"></textarea>
                                        <button type="submit" class="cm-send">Send</button>
                                    </div>
                                </form>
                            </div>
                        </div>"""

    content = f"""<div id="rp-stage" hx-get="{_P}/stage" hx-trigger="load" hx-target="this" hx-swap="innerHTML" style="height:100%; width:100%; background-size:cover; background-position:center; display:flex; align-items:center; justify-content:center;"></div>
                  <div id="rp-modal" hx-on:closeModal="this.innerHTML=''"></div>"""
    theme_diff = await ENV["resolve_theme"](request, module_ns="rp_server")
    return ENV["templates"].TemplateResponse(name="base.html", request=request, context={"request": request, "user": user, "nesting_level": lvl,
        "toolbars": {"left": UI.toolbar(side="left", content=LeftBarContent, size="20rem", overlay=True, nesting_level=lvl),
                     "right": UI.toolbar(side="right", content=RightBarContent, size="30rem", overlay=True, start_open=True, resizable=True, nesting_level=lvl)},
        "content": content, "extra_css": RP_CSS + CM.CSS, "extra_script": RP_SCRIPT + CM.SCRIPT, "theme_diff": theme_diff})

@router.get("/rooms", response_class=HTMLResponse)
async def rooms_list(request: Request, db: Session = Depends(get_db)): return HTMLResponse(_rooms_html(db, request.state.user.username, request.state.user.role))

@router.get("/personas", response_class=HTMLResponse)
async def list_personas(request: Request, db: Session = Depends(get_db)):
    username = request.state.user.username
    return HTMLResponse(_personas_html(username, get_user_session(db, username).current_persona, db))

@router.get("/stage", response_class=HTMLResponse)
async def get_stage(request: Request, db: Session = Depends(get_db)):
    sess = get_user_session(db, request.state.user.username)
    return HTMLResponse(_stage_html(db.get(Room, sess.current_room or "Public")))

@router.get("/messages/latest", response_class=HTMLResponse)
async def get_latest_messages(request: Request, db: Session = Depends(get_db)):
    username = request.state.user.username
    room_id = get_user_session(db, username).current_room or "Public"
    return HTMLResponse(_messages_html(db, room_id, username))

@router.get("/room/bridge", response_class=HTMLResponse)
async def room_bridge(request: Request, db: Session = Depends(get_db)):
    sess = get_user_session(db, request.state.user.username)
    return HTMLResponse(f'<input type="hidden" name="room_id" id="rp-room-bridge" value="{UI.escape(sess.current_room or "Public")}">')

@router.get("/personas/select_edit/{persona_id}", response_class=HTMLResponse)
async def persona_select_edit(persona_id: int, request: Request, db: Session = Depends(get_db)):
    username = request.state.user.username
    p = db.get(Persona, persona_id)
    if not p or p.owner_username != username: return HTMLResponse("Not found.", status_code=404)
    sess = get_user_session(db, username); sess.current_persona = p.name; db.commit()
    return HTMLResponse(_persona_edit_modal(p))

@router.get("/persona/manage", response_class=HTMLResponse)
async def persona_manage_shortcut(request: Request, db: Session = Depends(get_db)):
    username = request.state.user.username
    sess = get_user_session(db, username)
    if sess.current_persona and sess.current_persona != username:
        p = db.query(Persona).filter_by(owner_username=username, name=sess.current_persona).first()
        if p: return HTMLResponse(_persona_edit_modal(p))
    return HTMLResponse(UI.modal("rp-persona-edit", "Persona Settings", "<p style='font-size:.85rem;color:var(--text_muted);'>You're currently using your account identity. Create or select a custom persona from the sidebar list, then reopen this panel to edit it.</p>"))

@router.get("/room/manage", response_class=HTMLResponse)
async def manage_room(request: Request, db: Session = Depends(get_db)):
    username, role = request.state.user.username, request.state.user.role
    sess = get_user_session(db, username); room_id = sess.current_room or "Public"
    room = db.get(Room, room_id)
    if not room: return HTMLResponse("Room not found.", status_code=404)
    if room.owner != username and role not in ("admin", "moderator"): return HTMLResponse('<div style="padding:1rem;opacity:0.6;">You do not manage this room.</div>')
    group = _RoomSettingsGroup(room, db)
    member_html = _members_html(room_id, db.query(RoomMembership).filter_by(room_id=room_id).all())
    ai_link = f"""<button class="ui-btn" style="width:100%;margin-top:.8rem;" hx-get="{_P}/ai/room/{room_id}/settings" hx-target="#rp-modal" hx-swap="innerHTML">&#x1F916; AI Settings</button>""" if (_AI_INSTALLED and ai_server_enabled()) else ""
    body = f"""<form hx-post="/im/in" hx-swap="none" style="display:flex;flex-direction:column;gap:.4rem;">
                   <input type="hidden" name="type" value="rp_room_settings_save"><input type="hidden" name="branch" value="{IM.branch_id}"><input type="hidden" name="lvl" value="1">
                   <input type="hidden" name="room_id" value="{room_id}">
                   {UI.field("Title", UI.input("title", value=room.title or room_id))}
                   {group.render(group.load())}
                   <button class="ui-btn" style="color:var(--accent);">Save</button>
               </form>
               <div style="margin-top:1rem;padding-top:.8rem;border-top:1px solid var(--border);">
                   <label style="font-size:.8rem;font-weight:600;">Members</label>
                   <div id="rp-member-list" style="margin-top:.3rem;">{member_html}</div>
                   <form hx-post="/im/in" hx-target="#rp-member-list" hx-swap="innerHTML" style="display:flex;gap:.4rem;margin-top:.5rem;">
                       <input type="hidden" name="type" value="rp_room_member_add"><input type="hidden" name="branch" value="{IM.branch_id}"><input type="hidden" name="lvl" value="1">
                       <input type="hidden" name="room_id" value="{room_id}">
                       <input name="username" placeholder="username to invite" class="module-select" style="flex:1;font-size:.8rem;">
                       <button class="ui-btn">Add</button>
                   </form>
               </div>
               <div style="margin-top:1rem;padding-top:.8rem;border-top:1px solid var(--border);">
                   <label style="font-size:.8rem;font-weight:600;">Background Image</label>
                   <form hx-post="/im/in" hx-encoding="multipart/form-data" hx-swap="none" style="display:flex;gap:.4rem;margin-top:.3rem;">
                       <input type="hidden" name="type" value="rp_room_bg_upload"><input type="hidden" name="branch" value="{IM.branch_id}"><input type="hidden" name="lvl" value="1">
                       <input type="hidden" name="room_id" value="{room_id}">
                       <input type="file" name="bgfile" accept="image/*" style="flex:1;font-size:.75rem;">
                       <button class="ui-btn" style="font-size:.75rem;">Upload</button>
                   </form>
                   {f'''<button class="ui-btn" style="margin-top:.3rem;color:#ff6b6b;" hx-post="/im/in" hx-vals='{{"type":"rp_room_bg_clear","room_id":"{room_id}","branch":"{IM.branch_id}","lvl":1}}' hx-swap="none">Remove Background</button>''' if room.background else ''}
               </div>
			   {ai_link}"""
    return HTMLResponse(UI.modal("rp-room-settings", UI.escape(room_label(room)), body))

@router.get("/options", response_class=HTMLResponse)
async def options_panel(request: Request, db: Session = Depends(get_db)):
    username, role = request.state.user.username, request.state.user.role
    group = _user_options_group(username)
    watches = {w.room_id: w.notify for w in db.query(RoomWatch).filter_by(username=username).all()}
    rooms = [r for r in db.query(Room).all() if can_access_room(db, r, username, role)]
    watch_rows = "".join(f'<div style="display:flex;align-items:center;justify-content:space-between;padding:0.2rem 0;"><span style="font-size:0.85rem;">{UI.escape(room_label(r))}</span><label style="display:flex;align-items:center;gap:0.3rem;cursor:pointer;font-size:0.8rem;"><input type="checkbox" name="watch_{UI.escape(r.id)}" value="1" {"checked" if watches.get(r.id) else ""}> notify</label></div>' for r in rooms)
    theme_override = await ENV["resolve_theme"](request, module_ns="rp_server")
    me_color, other_color = theme_override.get("rp-bubble-me-bg", "#00a0dc"), theme_override.get("rp-bubble-other-bg", "#00b464")
    ai_toggle = ""
    if _AI_INSTALLED and role == "admin": ai_toggle = UI.collapsible("Server Administration", f"""<label style="display:flex;align-items:center;gap:.4rem;font-size:.85rem;"><input type="checkbox" name="ai_server_enabled" value="1" {"checked" if ai_server_enabled() else ""}> Allow AI features module-wide</label><p style="font-size:.72rem;color:var(--text_muted);margin:.3rem 0 0;">If unchecked, no AI options appear anywhere in RP Server for anyone, regardless of individual room settings.</p>""")
    body = f"""<form hx-post="/im/in" hx-swap="none" style="display:flex;flex-direction:column;gap:0.8rem;">
                   <input type="hidden" name="type" value="rp_options_save"><input type="hidden" name="branch" value="{IM.branch_id}"><input type="hidden" name="lvl" value="1">
                   {UI.collapsible("Notifications", f"<p style='font-size:0.75rem;opacity:0.6;margin:0 0 0.5rem;'>Push notifications when someone posts while you're away.</p>{watch_rows}", open_=True)}
                   {UI.collapsible("Appearance (this device, this module)", f'{UI.field("My bubble color", f"<input type=&quot;color&quot; name=&quot;me_bg&quot; value=&quot;{me_color}&quot; style=&quot;width:3rem;height:2rem;border:none;background:none;cursor:pointer;&quot;>")}{UI.field("Others bubble color", f"<input type=&quot;color&quot; name=&quot;other_bg&quot; value=&quot;{other_color}&quot; style=&quot;width:3rem;height:2rem;border:none;background:none;cursor:pointer;&quot;>")}<p style="font-size:.72rem;color:var(--text_muted);">Full theme controls: Control Panel &#x2192; Appearance.</p>')}
                   {group.render(group.load())}
                   {ai_toggle}
                   <div style='display:flex;justify-content:flex-end;'><button class='ui-btn' style='color:var(--accent);'>Save Settings</button></div>
               </form>"""
    return HTMLResponse(UI.modal("rp-options", "Options & Settings", body))

@router.get("/messages/edit_modal", response_class=HTMLResponse)
async def message_edit_modal(request: Request, msg_id: int, db: Session = Depends(get_db)):
    username = request.state.user.username
    msg = db.get(Message, msg_id)
    if not msg or msg.user_name != username: return HTMLResponse("Forbidden.", status_code=403)
    persona_opts = [(username, f"{username} (you)")] + [(p.name, p.name) for p in db.query(Persona).filter_by(owner_username=username).all()]
    t = getattr(msg, "story_time", None) or msg.created_at
    body = f"""<form hx-post="/im/in" hx-swap="none" style="display:flex;flex-direction:column;gap:0.6rem;">
                   <input type="hidden" name="type" value="rp_msg_edit"><input type="hidden" name="branch" value="{IM.branch_id}"><input type="hidden" name="lvl" value="1">
                   <input type="hidden" name="msg_id" value="{msg_id}">
                   {UI.field("Persona", UI.select("persona_name", persona_opts, selected=msg.persona_name or ""))}
                   {UI.field("Story time", UI.input("story_time", type_="datetime-local", value=t.strftime("%Y-%m-%dT%H:%M") if t else ""))}
                   {UI.field("Message", UI.textarea("content", value=msg.content or "", rows=6))}
                   <div style="display:flex;gap:0.5rem;justify-content:flex-end;">
                       <button class="ui-btn" style="color:var(--accent);">Save</button>
                       <button type="button" class="ui-btn" onclick="document.getElementById('rp-modal').innerHTML=''">Cancel</button>
                   </div>
               </form>"""
    return HTMLResponse(UI.modal("rp-msg-edit", "Edit Message", body))

@router.get("/diagnostics")
async def diagnostics(request: Request, db: Session = Depends(get_db)):
    ws = ENV.get("ws")
    return JSONResponse({"users": db.query(UserSession).count(), "rooms": db.query(Room).count(), "personas": db.query(Persona).count(), "messages": db.query(Message).count(), "ws": ws.get_stats() if ws else {"note": "ws not injected"}})
